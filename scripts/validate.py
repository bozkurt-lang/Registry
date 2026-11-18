#!/usr/bin/env python3
"""Validate Bozkurt package registry index files."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tarfile
import urllib.error
import urllib.request
from pathlib import Path

NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9_-]{0,62}[a-z0-9])?$")
SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
GITHUB_REPO_RE = re.compile(r"^https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?$")
RESERVED_NAMES = frozenset({
    "std",
    "core",
    "alloc",
    "compiler",
    "builtin",
    "bozkurt",
    "test",
    "main",
})
ALLOWED_COLLABORATOR_PERMS = frozenset({"admin", "maintain", "write"})


def expected_shard_path(name: str) -> Path:
    if len(name) == 1:
        return Path("index") / "1" / f"{name}.json"
    if len(name) == 2:
        return Path("index") / "2" / f"{name}.json"
    if len(name) == 3:
        return Path("index") / "3" / name[0] / f"{name}.json"
    return Path("index") / name[:2] / name[2:4] / f"{name}.json"


def parse_github_repo(repo_url: str) -> tuple[str, str] | None:
    match = GITHUB_REPO_RE.match(repo_url.strip())
    if not match:
        return None
    return match.group(1), match.group(2)


def github_headers() -> dict[str, str]:
    headers = {
        "User-Agent": "bozkurt-registry-validator/1.0",
        "Accept": "application/vnd.github+json",
    }
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def load_base_index(rel: Path, repo_root: Path, base_ref: str) -> tuple[dict | None, str | None]:
    ref_check = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", base_ref],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    if ref_check.returncode != 0:
        return None, None

    obj_spec = f"{base_ref}:{rel.as_posix()}"
    exists = subprocess.run(
        ["git", "cat-file", "-e", obj_spec],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    if exists.returncode != 0:
        return None, None

    show = subprocess.run(
        ["git", "show", obj_spec],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    if show.returncode != 0:
        return None, f"{rel}: failed to read {obj_spec}: {show.stderr.strip()}"

    try:
        parsed = json.loads(show.stdout)
    except json.JSONDecodeError as exc:
        return None, f"{rel}: invalid JSON in {obj_spec}: {exc}"

    if not isinstance(parsed, dict):
        return None, f"{rel}: {obj_spec} top-level value is not an object"
    return parsed, None


def verify_repo_ownership(owner: str, repo_name: str, actor: str, rel: Path) -> list[str]:
    url = f"https://api.github.com/repos/{owner}/{repo_name}/collaborators/{actor}/permission"
    req = urllib.request.Request(url, headers=github_headers())
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            if resp.status != 200:
                return [f"{rel}: GitHub ownership check returned HTTP {resp.status} for {actor!r}"]
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return [f"{rel}: user {actor!r} is not authorized for {owner}/{repo_name} (HTTP {exc.code})"]
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        return [f"{rel}: failed to verify ownership for {actor!r} on {owner}/{repo_name}: {exc}"]

    perm = body.get("permission")
    if perm not in ALLOWED_COLLABORATOR_PERMS:
        return [f"{rel}: user {actor!r} lacks write/maintain/admin permission on {owner}/{repo_name} (got {perm!r})"]
    return []


def validate_immutability(old_data: dict, new_data: dict, rel: Path) -> list[str]:
    errors: list[str] = []

    old_repo = old_data.get("repo")
    new_repo = new_data.get("repo")
    if old_repo != new_repo:
        errors.append(f"{rel}: existing package cannot change 'repo' ({old_repo!r} -> {new_repo!r})")

    old_versions: dict[str, dict] = {}
    for entry in old_data.get("versions", []):
        if isinstance(entry, dict) and isinstance(entry.get("vers"), str):
            old_versions[entry["vers"]] = entry

    new_versions: dict[str, dict] = {}
    for entry in new_data.get("versions", []):
        if isinstance(entry, dict) and isinstance(entry.get("vers"), str):
            new_versions[entry["vers"]] = entry

    for vers, old_v in old_versions.items():
        if vers not in new_versions:
            errors.append(f"{rel}: existing version {vers!r} cannot be deleted")
            continue

        new_v = new_versions[vers]
        for field in ("sha256", "tag", "tarball"):
            if old_v.get(field) != new_v.get(field):
                errors.append(f"{rel}: existing version {vers!r} cannot modify '{field}'")

        if old_v.get("deps", []) != new_v.get("deps", []):
            errors.append(f"{rel}: existing version {vers!r} cannot modify 'deps'")

        old_yanked = bool(old_v.get("yanked", False))
        new_yanked = bool(new_v.get("yanked", False))
        if old_yanked and not new_yanked:
            errors.append(f"{rel}: existing version {vers!r} cannot be un-yanked")

    return errors


def fetch_and_verify_tarball(url: str, expected_sha256: str, pkg_name: str, vers: str) -> list[str]:
    errors: list[str] = []
    req = urllib.request.Request(url, headers={"User-Agent": "bozkurt-registry-validator/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            if resp.status != 200:
                return [f"HTTP {resp.status} fetching {url}"]
            data = resp.read()
    except (urllib.error.URLError, TimeoutError) as exc:
        return [f"failed to fetch {url}: {exc}"]

    actual_sha256 = hashlib.sha256(data).hexdigest()
    if actual_sha256 != expected_sha256:
        errors.append(
            f"sha256 mismatch for {pkg_name}@{vers}: expected {expected_sha256}, got {actual_sha256}"
        )
        return errors

    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
            manifest_member = None
            for member in tf.getmembers():
                parts = Path(member.name).parts
                if len(parts) == 2 and parts[1] in ("Bozkurt.toml", "bozkurt.toml"):
                    manifest_member = member
                    break
            if manifest_member is None:
                errors.append(f"{pkg_name}@{vers}: archive missing root Bozkurt.toml")
            else:
                raw_manifest = tf.extractfile(manifest_member)
                if raw_manifest is None:
                    errors.append(f"{pkg_name}@{vers}: unreadable Bozkurt.toml")
                else:
                    text = raw_manifest.read().decode("utf-8", errors="replace")
                    if f'"{pkg_name}"' not in text and f"'{pkg_name}'" not in text:
                        errors.append(f"{pkg_name}@{vers}: Bozkurt.toml does not declare name '{pkg_name}'")
    except (tarfile.TarError, OSError) as exc:
        errors.append(f"{pkg_name}@{vers}: invalid tar.gz archive: {exc}")

    return errors


def validate_file(
    path: Path,
    repo_root: Path,
    verify_network: bool,
    base_ref: str = "origin/main",
    pr_author: str | None = None,
) -> list[str]:
    errors: list[str] = []
    try:
        rel = path.resolve().relative_to(repo_root.resolve())
    except ValueError:
        rel = path

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [f"{rel}: invalid JSON: {exc}"]

    if not isinstance(data, dict):
        return [f"{rel}: top-level value must be a JSON object"]

    name = data.get("name")
    if not isinstance(name, str) or not NAME_RE.match(name):
        errors.append(f"{rel}: invalid package name {name!r}")
    elif name in RESERVED_NAMES:
        errors.append(f"{rel}: package name {name!r} is reserved")
    else:
        exp_rel = expected_shard_path(name)
        if Path(rel.as_posix()) != Path(exp_rel.as_posix()):
            errors.append(f"{rel}: expected shard path {exp_rel.as_posix()}")

    repo = data.get("repo")
    parsed_repo = parse_github_repo(repo) if isinstance(repo, str) else None
    if parsed_repo is None:
        errors.append(f"{rel}: 'repo' must be an https://github.com/<owner>/<repo> URL")
        allowed_tarball_prefixes: tuple[str, ...] = ()
    else:
        owner_l, repo_l = parsed_repo[0].lower(), parsed_repo[1].lower()
        allowed_tarball_prefixes = (
            f"https://codeload.github.com/{owner_l}/{repo_l}/",
            f"https://github.com/{owner_l}/{repo_l}/",
        )

    owners = data.get("owners")
    if owners is not None:
        if not isinstance(owners, list) or not all(isinstance(o, str) and o.strip() for o in owners):
            errors.append(f"{rel}: 'owners' must be an array of non-empty strings")

    desc = data.get("description")
    if not isinstance(desc, str) or not desc.strip() or "\t" in desc or "\n" in desc:
        errors.append(f"{rel}: 'description' must be a non-empty single-line string without tabs")

    versions = data.get("versions")
    if not isinstance(versions, list) or len(versions) == 0:
        errors.append(f"{rel}: 'versions' must be a non-empty array")
        return errors

    old_data, base_err = load_base_index(rel, repo_root, base_ref)
    if base_err is not None:
        errors.append(base_err)
    elif old_data is not None:
        errors.extend(validate_immutability(old_data, data, rel))
        if verify_network and old_data != data:
            actor = (pr_author or os.environ.get("GITHUB_ACTOR", "")).strip()
            if not actor:
                errors.append(f"{rel}: cannot verify ownership for modified package without --pr-author or GITHUB_ACTOR")
            else:
                old_owners = old_data.get("owners")
                if isinstance(old_owners, list) and actor in old_owners:
                    pass
                else:
                    old_repo = old_data.get("repo")
                    old_parsed = parse_github_repo(old_repo) if isinstance(old_repo, str) else None
                    if old_parsed is None:
                        errors.append(f"{rel}: existing package has invalid 'repo' {old_repo!r}")
                    else:
                        errors.extend(verify_repo_ownership(old_parsed[0], old_parsed[1], actor, rel))

    seen_vers: set[str] = set()
    for idx, entry in enumerate(versions):
        prefix = f"{rel} versions[{idx}]"
        if not isinstance(entry, dict):
            errors.append(f"{prefix}: must be an object")
            continue

        vers = entry.get("vers")
        if not isinstance(vers, str) or not SEMVER_RE.match(vers):
            errors.append(f"{prefix}: invalid semver {vers!r}")
        elif vers in seen_vers:
            errors.append(f"{prefix}: duplicate version {vers!r}")
        else:
            seen_vers.add(vers)

        tag = entry.get("tag")
        if not isinstance(tag, str) or not tag.strip():
            errors.append(f"{prefix}: 'tag' must be a non-empty string")

        tarball = entry.get("tarball")
        if not isinstance(tarball, str):
            errors.append(f"{prefix}: 'tarball' must be a string")
        elif allowed_tarball_prefixes and not tarball.lower().startswith(allowed_tarball_prefixes):
            errors.append(f"{prefix}: 'tarball' URL must match declared 'repo' ({repo})")

        sha256 = entry.get("sha256")
        if not isinstance(sha256, str) or not SHA256_RE.match(sha256):
            errors.append(f"{prefix}: 'sha256' must be 64 lowercase hex characters")

        yanked = entry.get("yanked", False)
        if not isinstance(yanked, bool):
            errors.append(f"{prefix}: 'yanked' must be a boolean")

        deps = entry.get("deps", [])
        if not isinstance(deps, list):
            errors.append(f"{prefix}: 'deps' must be an array")
        else:
            for d_idx, dep in enumerate(deps):
                if not isinstance(dep, dict):
                    errors.append(f"{prefix}.deps[{d_idx}]: must be an object")
                    continue
                d_name = dep.get("name")
                d_req = dep.get("req")
                if not isinstance(d_name, str) or not NAME_RE.match(d_name):
                    errors.append(f"{prefix}.deps[{d_idx}]: invalid dependency name {d_name!r}")
                if not isinstance(d_req, str) or not d_req.strip():
                    errors.append(f"{prefix}.deps[{d_idx}]: invalid version requirement {d_req!r}")

        if verify_network and not yanked and isinstance(tarball, str) and isinstance(sha256, str) and isinstance(name, str) and isinstance(vers, str):
            errors.extend(fetch_and_verify_tarball(tarball, sha256, name, vers))

    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate Bozkurt registry index files.")
    parser.add_argument("files", nargs="*", help="JSON files to validate (defaults to all under index/)")
    parser.add_argument("--verify-network", action="store_true", help="Download tarballs and verify SHA-256, Bozkurt.toml, and ownership")
    parser.add_argument("--base-ref", default="origin/main", help="Git ref to compare against for immutability and ownership")
    parser.add_argument("--pr-author", default=None, help="GitHub username of the PR author (defaults to GITHUB_ACTOR)")
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parent.parent
    targets = [Path(f) for f in args.files] if args.files else sorted((repo_root / "index").rglob("*.json"))

    if not targets:
        print("No index files to validate.")
        return 0

    all_errors: list[str] = []
    for target in targets:
        all_errors.extend(validate_file(target, repo_root, args.verify_network, args.base_ref, args.pr_author))

    if all_errors:
        for err in all_errors:
            print(f"ERROR: {err}", file=sys.stderr)
        return 1

    print(f"Validated {len(targets)} index file(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
