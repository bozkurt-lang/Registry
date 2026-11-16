# Bozkurt Package Registry

Central package index for `bzc pkg` (`bzpm`).

## Layout

```text
index/                  Prefix-sharded package metadata (JSON)
catalog.tsv             Generated tab-separated index of latest versions
docs/                   Static search UI (GitHub Pages)
scripts/                Index validation and catalog generation tools
.github/workflows/      PR validation and catalog/Pages deployment
```

## Sharding

Package names are lowercase ASCII (`[a-z0-9_-]`, 1 to 64 characters, cannot begin or end with `-` or `_`).
Each package has a single metadata file under `index/`:

- 1 character: `index/1/<name>.json`
- 2 characters: `index/2/<name>.json`
- 3 characters: `index/3/<name[0]>/<name>.json`
- 4+ characters: `index/<name[0..2]>/<name[2..4]>/<name>.json`

For example, `hello` lives at `index/he/ll/hello.json`.

## Package Metadata Schema

```json
{
  "name": "hello",
  "repo": "https://github.com/owner/hello",
  "description": "Short summary of the package",
  "license": "MIT",
  "versions": [
    {
      "vers": "0.1.0",
      "tag": "v0.1.0",
      "tarball": "https://codeload.github.com/owner/hello/tar.gz/refs/tags/v0.1.0",
      "sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
      "yanked": false,
      "deps": [
        { "name": "core-util", "req": "0.2.0" }
      ]
    }
  ]
}
```

## Catalog Format (`catalog.tsv`)

`catalog.tsv` is generated from `index/` on every push to `main`. Lines starting with `#` are comments. Columns are tab-separated:

```text
# name	version	description	repo	sha256
```

## Endpoints

- Index file: `https://raw.githubusercontent.com/bozkurt-lang/Registry/main/index/<shard>/<name>.json`
- Catalog: `https://raw.githubusercontent.com/bozkurt-lang/Registry/main/catalog.tsv`
- Web search: `https://bozkurt-lang.github.io/Registry/`

## Submitting a Package

1. Tag a release in your GitHub repository containing a valid `Bozkurt.toml`.
2. Compute the SHA-256 of the GitHub source tarball:
   `https://codeload.github.com/<owner>/<repo>/tar.gz/refs/tags/<tag>`
3. Fork this repository and add or update `index/<shard>/<name>.json`.
4. Run `python3 scripts/validate.py --verify-network index/<shard>/<name>.json` locally.
5. Open a pull request against `main`. CI verifies the schema, tarball availability, SHA-256 digest, and `Bozkurt.toml` manifest inside the archive.
