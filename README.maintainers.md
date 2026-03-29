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

Note: generated artifacts such as `modpack/pack.template.json` and the lock files are committed in this repo and should be regenerated when `pack.toml` changes.

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
- `[overrides]`
  - `include`: paths shipped in both packs as Modrinth `overrides/`
  - `client_include`: paths shipped only in the client pack as `client-overrides/`
  - `server_include`: paths shipped only in the server pack as `server-overrides/`

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
- `additional_dependencies`: extra Modrinth project URLs to force as required dependencies for this mod
- `enabled`: for `resourcepacks.*.packs` only, controls whether the resolved zip is included in client
  `options.txt`; defaults to `true`
- `incompatible`: for `resourcepacks.*.packs` only, adds the resolved zip to
  `incompatibleResourcePacks` in client `options.txt`; defaults to `false`

Under `[resourcepacks.from_overrides]`, you can also set:

- `enabled`: filenames from `overrides/client/resourcepacks/` to enable by default,
  for example `"My Override Pack.zip"`

For `mods.*.mods` entries, the resolver also auto-includes Modrinth dependencies marked as required.
All other Modrinth dependency types are ignored. Use `additional_dependencies` when a mod
needs another dependency that is not declared upstream.

For `resourcepacks.*.packs`, client-side entries are enabled in `options.txt` by default.
Set `enabled = false` to keep a resource pack included in the pack, but not pre-enabled.
Set `incompatible = true` when a pack works in practice but Minecraft marks it as incompatible,
so the client pack build also rewrites `incompatibleResourcePacks:` with the resolved filename.
The client pack build fully regenerates `overrides/client/options.txt` with only the
`resourcePacks:` and `incompatibleResourcePacks:` lines using the resolved filenames from
`modpack/resource-packs.lock.json`, so you do not need to manually keep download filenames in sync.
Any entries from `resourcepacks.from_overrides.enabled` are converted to `file/...` entries
and appended before de-duplication.

Example:

```toml
{ 
  url = "https://modrinth.com/project/abc123",
  side = "both",
  name = "Some Mod",
  additional_dependencies = [
    "https://modrinth.com/project/lib123"
  ]
}
```

Resource pack example:

```toml
{ 
  url = "https://modrinth.com/project/pack123",
  side = "client",
  name = "Some Resource Pack",
  enabled = false
}
```

## Overrides

The builder can embed local override files and folders into the generated `.mrpack`.

Example:

```toml
[overrides]
include = [
  "overrides/shared/config",
  "overrides/shared/defaultconfigs",
  "overrides/shared/mods"
]
client_include = [
  "overrides/client/options.txt",
  "overrides/client/servers.dat"
]
server_include = [
  "overrides/server/mods"
]
```

Suggested layout:

- `overrides/shared/...`: packed into `overrides/`
- `overrides/client/...`: packed into `client-overrides/`
- `overrides/server/...`: packed into `server-overrides/`

Each configured path may point to either a file or a directory.
Directory contents are added recursively. This can be used for mod configs, `servers.dat`,
`options.txt`, `defaultconfigs`, KubeJS content, and local `.jar` files placed under
override paths such as `overrides/shared/mods`.

Override install paths must not collide with each other or with downloaded manifest files.

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
python3 scripts/build_mrpack.py --side both --version 0.2.0
```

## Release flow

1. Edit `modpack/pack.toml`.
2. Commit changes.
3. Tag and push:

```bash
git tag v0.2.0
git push origin v0.2.0
```

4. GitHub Actions runs:
   1. `scripts/resolve_manifests.py --target all`
   2. `scripts/build_mrpack.py --side both --version <tag-version>`
5. Release assets uploaded:

- `chuj-client-<version>.mrpack`
- `chuj-server-<version>.mrpack`

## Notes

- Regenerate `README.md` manually with `python3 scripts/generate_readme.py` when you want the public README to reflect `pack.toml` changes.
- Generated files (`pack.template.json`, `*.lock.json`) are build artifacts derived from `pack.toml`.
- `build_mrpack.py` remains intentionally simple and artifact-focused.
