# CHUJ - Create: Hardcore Ultimate Journey

A Forge 1.20.1 modpack focused on Create, survival progression, and co-op play.

## Download and play

1. Go to the GitHub Releases page for this repo.
2. Download the latest `chuj-client-<version>.mrpack`.
3. Open Prism Launcher.
4. Click `Add Instance` -> `Import`.
5. Select the downloaded `.mrpack`.

Use `chuj-server-<version>.mrpack` for dedicated server setup.

## Pack info

- Minecraft: `1.20.1`
- Loader: `Forge 47.4.10`

## Source of truth

This repo is TOML-first:

- Edit `modpack/pack.toml`
- Generate artifacts with `scripts/resolve_manifests.py`
- Build `.mrpack` files with `scripts/build_mrpack.py`

Generated files:

- `modpack/modrinth.template.json`
- `modpack/mods.lock.json`
- `modpack/resource-packs.lock.json`
- `modpack/shader-packs.lock.json`

These generated lock files are ignored via `.gitignore` and are rebuilt from `pack.toml`.

## Local build

```bash
python3 scripts/resolve_manifests.py --target all
python3 scripts/build_mrpack.py --side both
```

## Project docs

- Maintainer guide: `README.maintainers.md`
- Pack config (source of truth): `modpack/pack.toml`
