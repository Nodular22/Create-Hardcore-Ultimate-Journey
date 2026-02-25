# CHUJ Maintainer Guide

Technical documentation for maintaining and releasing the CHUJ modpack repo.

## Repository structure

- `modpack/pack.toml`: single human-edited source of truth
- `modpack/pack.template.json`: generated pack metadata template
- `modpack/mods.lock.json`: generated resolved mod artifacts
- `modpack/resource-packs.lock.json`: generated resolved resource pack artifacts
- `modpack/shader-packs.lock.json`: generated resolved shader pack artifacts
- `scripts/resolve_manifests.py`: render template + resolve lock files from `pack.toml`
- `scripts/generate_readme.py`: generate user-facing `README.md` from `pack.toml`
- `scripts/build_mrpack.py`: build client/server `.mrpack` from generated JSON artifacts
- `.github/workflows/release.yml`: resolves + builds + uploads both packs on tag push
- `flake.nix`: local Nix development shell

Note: `modpack/*.lock.json` is gitignored; lock files are regenerated from `pack.toml`.

## `pack.toml` format

### Core metadata

- `[pack]`
  - `name`: pack name
  - `summary`: pack summary
  - `debug_version`: default version used for local builds
- `[dependencies]`
  - `minecraft`: Minecraft version
  - `loader`: one of `forge`, `fabric`, `quilt`, `neoforge`
  - `loader_version`: loader version string
- `[build]`
  - `slug`: output filename prefix used by `build_mrpack.py`
  - `default_side`: default side used by `build_mrpack.py` if `--side` is omitted
  - `dist_dir`: output directory used by `build_mrpack.py` if `--dist` is omitted

### Content sections

The resolver reads these tables and flattens them into lock manifests:

- `mods.<category>.mods = [ ... ]`
- `resourcepacks.<category>.packs = [ ... ]`
- `shaderpacks.<category>.packs = [ ... ]`

Each entry must include:

- `url`: Modrinth project URL (`https://modrinth.com/project/<id-or-slug>`)
- `side`: `both`, `client`, or `server`

Optional:

- `name`: readability only
- `version`: pin by exact Modrinth `version_number`

## Resolve from TOML

Render template and all lock files:

```bash
python3 scripts/resolve_manifests.py --target all
```

Resolve only one output:

```bash
python3 scripts/resolve_manifests.py --target template
python3 scripts/resolve_manifests.py --target mods
python3 scripts/resolve_manifests.py --target resourcepacks
python3 scripts/resolve_manifests.py --target shaderpacks
```

Validate without writing files:

```bash
python3 scripts/resolve_manifests.py --target all --check
```

## Generate README

Regenerate the user-facing `README.md` after pack edits:

```bash
python3 scripts/generate_readme.py
```

## Local builds

### Option 1: Nix (recommended)

```bash
nix develop
python3 scripts/resolve_manifests.py --target all
python3 scripts/generate_readme.py
python3 scripts/build_mrpack.py --side both
```

### Option 2: Global tools

Requirements:

- Python 3.11+ (for `tomllib`)

Build both packs:

```bash
python3 scripts/resolve_manifests.py --target all
python3 scripts/generate_readme.py
python3 scripts/build_mrpack.py
```

Build one side:

```bash
python3 scripts/build_mrpack.py --side client
python3 scripts/build_mrpack.py --side server
```

Build with explicit version:

```bash
python3 scripts/build_mrpack.py --side both --version 0.1.1
```

## Release flow

1. Edit `modpack/pack.toml`.
2. Commit changes.
3. Tag and push:

```bash
git tag v0.1.0
git push origin v0.1.0
```

4. GitHub Actions runs:
   1. `scripts/resolve_manifests.py --target all`
   2. `scripts/generate_readme.py`
   3. `scripts/build_mrpack.py --side both --version <tag-version>`
5. Release assets uploaded:

- `chuj-client-<version>.mrpack`
- `chuj-server-<version>.mrpack`

## Notes

- Generated files (`pack.template.json`, `*.lock.json`) are build artifacts derived from `pack.toml`.
- `build_mrpack.py` remains intentionally simple and artifact-focused.
