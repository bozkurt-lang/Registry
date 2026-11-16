#!/usr/bin/env python3
"""Validate Bozkurt package registry index files."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import sys
import tarfile
import urllib.error
import urllib.request
from pathlib import Path

NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9_-]{0,62}[a-z0-9])?$")
SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
ALLOWED_TARBALL_PREFIXES = (
    "https://codeload.github.com/",
    "https://github.com/",
)


def expected_shard_path(name: str) -> Path:
    if len(name) == 1:
        return Path("index") / "1" / f"{name}.json"
    if len(name) == 2:
        return Path("index") / "2" / f"{name}.json"
    if len(name) == 3:
        return Path("index") / "3" / name[0] / f"{name}.json"
    return Path("index") / name[:2] / name[2:4] / f"{name}.json"


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


def validate_file(path: Path, repo_root: Path, verify_network: bool) -> list[str]:
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
    else:
        exp_rel = expected_shard_path(name)
        if Path(rel.as_posix()) != Path(exp_rel.as_posix()):
            errors.append(f"{rel}: expected shard path {exp_rel.as_posix()}")

    repo = data.get("repo")
    if not isinstance(repo, str) or not repo.startswith("https://github.com/"):
        errors.append(f"{rel}: 'repo' must be an https://github.com/... URL")

    desc = data.get("description")
    if not isinstance(desc, str) or not desc.strip() or "\t" in desc or "\n" in desc:
        errors.append(f"{rel}: 'description' must be a non-empty single-line string without tabs")

    versions = data.get("versions")
    if not isinstance(versions, list) or len(versions) == 0:
        errors.append(f"{rel}: 'versions' must be a non-empty array")
        return errors

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
        if not isinstance(tarball, str) or not tarball.startswith(ALLOWED_TARBALL_PREFIXES):
            errors.append(f"{prefix}: 'tarball' must be a GitHub archive URL")

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
    parser.add_argument("--verify-network", action="store_true", help="Download tarballs and verify SHA-256 and Bozkurt.toml")
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parent.parent
    targets = [Path(f) for f in args.files] if args.files else sorted((repo_root / "index").rglob("*.json"))

    if not targets:
        print("No index files to validate.")
        return 0

    all_errors: list[str] = []
    for target in targets:
        all_errors.extend(validate_file(target, repo_root, args.verify_network))

    if all_errors:
        for err in all_errors:
            print(f"ERROR: {err}", file=sys.stderr)
        return 1

    print(f"Validated {len(targets)} index file(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
