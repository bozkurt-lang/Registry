#!/usr/bin/env python3
"""Generate catalog.tsv and docs/catalog.tsv from index/**/*.json."""

from __future__ import annotations

import functools
import json
import sys
from pathlib import Path

HEADER = "# name\tversion\tdescription\trepo\tsha256\n"


def sanitize_field(value: str) -> str:
    return value.replace("\t", " ").replace("\r", " ").replace("\n", " ").strip()


def parse_semver(vers: str) -> tuple[tuple[int, int, int], tuple[str, ...] | None]:
    core_and_pre = vers.split("+", 1)[0]
    if "-" in core_and_pre:
        core_str, pre_str = core_and_pre.split("-", 1)
        pre: tuple[str, ...] | None = tuple(pre_str.split("."))
    else:
        core_str = core_and_pre
        pre = None
    major, minor, patch = (int(part) for part in core_str.split("."))
    return (major, minor, patch), pre


def compare_semver(a: str, b: str) -> int:
    core_a, pre_a = parse_semver(a)
    core_b, pre_b = parse_semver(b)
    if core_a != core_b:
        return -1 if core_a < core_b else 1

    if pre_a is None and pre_b is None:
        return 0
    if pre_a is None:
        return 1
    if pre_b is None:
        return -1

    for id_a, id_b in zip(pre_a, pre_b):
        num_a = id_a.isdigit()
        num_b = id_b.isdigit()
        if num_a and num_b:
            va, vb = int(id_a), int(id_b)
            if va != vb:
                return -1 if va < vb else 1
        elif num_a:
            return -1
        elif num_b:
            return 1
        elif id_a != id_b:
            return -1 if id_a < id_b else 1

    if len(pre_a) != len(pre_b):
        return -1 if len(pre_a) < len(pre_b) else 1
    return 0


SEMVER_KEY = functools.cmp_to_key(compare_semver)


def select_latest_version(versions: list[dict]) -> dict | None:
    if not versions:
        return None
    non_yanked = [v for v in versions if not v.get("yanked", False)]
    candidates = non_yanked if non_yanked else versions
    return max(candidates, key=lambda v: SEMVER_KEY(v["vers"]))


def build_catalog(repo_root: Path) -> str:
    index_dir = repo_root / "index"
    rows: list[tuple[str, str, str, str, str]] = []

    if index_dir.exists():
        for path in sorted(index_dir.rglob("*.json")):
            data = json.loads(path.read_text(encoding="utf-8"))
            name = sanitize_field(data["name"])
            repo = sanitize_field(data["repo"])
            desc = sanitize_field(data["description"])
            latest = select_latest_version(data.get("versions", []))
            if latest is None:
                continue
            rows.append((
                name,
                sanitize_field(latest["vers"]),
                desc,
                repo,
                sanitize_field(latest["sha256"]),
            ))

    rows.sort(key=lambda r: r[0])
    lines = [HEADER]
    for row in rows:
        lines.append("\t".join(row) + "\n")
    return "".join(lines)


def main() -> int:
    repo_root = Path(__file__).resolve().parent.parent
    content = build_catalog(repo_root)

    (repo_root / "catalog.tsv").write_text(content, encoding="utf-8")
    docs_dir = repo_root / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "catalog.tsv").write_text(content, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
