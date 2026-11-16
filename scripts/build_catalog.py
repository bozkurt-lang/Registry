#!/usr/bin/env python3
"""Generate catalog.tsv and docs/catalog.tsv from index/**/*.json."""

from __future__ import annotations

import json
import sys
from pathlib import Path

HEADER = "# name\tversion\tdescription\trepo\tsha256\n"


def sanitize_field(value: str) -> str:
    return value.replace("\t", " ").replace("\r", " ").replace("\n", " ").strip()


def build_catalog(repo_root: Path) -> str:
    index_dir = repo_root / "index"
    rows: list[tuple[str, str, str, str, str]] = []

    if index_dir.exists():
        for path in sorted(index_dir.rglob("*.json")):
            data = json.loads(path.read_text(encoding="utf-8"))
            name = sanitize_field(data["name"])
            repo = sanitize_field(data["repo"])
            desc = sanitize_field(data["description"])
            versions = [v for v in data.get("versions", []) if not v.get("yanked", False)]
            if not versions:
                continue
            latest = versions[-1]
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
