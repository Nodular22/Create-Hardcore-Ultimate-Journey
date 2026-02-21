#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import tomllib
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

REQUIRED_TEMPLATE_KEYS = [
    "formatVersion",
    "game",
    "name",
    "summary",
    "versionId",
    "dependencies",
]

VALID_SIDES = {"both", "client", "server"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build Prism-importable .mrpack files for client/server from lock manifests"
    )
    parser.add_argument("--pack", default="modpack/pack.toml")
    parser.add_argument("--version", default="", help="Override versionId")
    parser.add_argument("--template", default="modpack/modrinth.template.json")
    parser.add_argument("--mods", default="modpack/mods.lock.json")
    parser.add_argument("--resource-packs", default="modpack/resource-packs.lock.json")
    parser.add_argument("--shader-packs", default="modpack/shader-packs.lock.json")
    parser.add_argument("--dist", default="")
    parser.add_argument("--slug", default="")
    parser.add_argument("--side", default="")
    return parser.parse_args()


def read_json(path: Path) -> object:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def read_pack_build_defaults(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    with path.open("rb") as f:
        data = tomllib.load(f)
    if not isinstance(data, dict):
        return {}
    build = data.get("build")
    if not isinstance(build, dict):
        return {}

    defaults: dict[str, str] = {}
    for key in ("slug", "dist_dir", "default_side"):
        value = build.get(key)
        if isinstance(value, str) and value:
            defaults[key] = value
    return defaults


def validate_template(template: dict) -> None:
    missing = [k for k in REQUIRED_TEMPLATE_KEYS if k not in template]
    if missing:
        raise ValueError(f"Template missing required keys: {', '.join(missing)}")


def validate_entries(entries: object, manifest_name: str) -> list[dict]:
    if not isinstance(entries, list):
        raise ValueError(f"{manifest_name} must be a JSON array")

    for i, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ValueError(f"{manifest_name}[{i}] must be an object")

        filename = entry.get("filename")
        side = entry.get("side")
        downloads = entry.get("downloads")
        hashes = entry.get("hashes")
        file_size = entry.get("fileSize")

        if not isinstance(filename, str) or not filename:
            raise ValueError(f"{manifest_name}[{i}].filename must be a non-empty string")
        if side not in VALID_SIDES:
            raise ValueError(
                f"{manifest_name}[{i}].side must be one of {sorted(VALID_SIDES)}"
            )
        if not isinstance(downloads, list) or not downloads or not all(
            isinstance(url, str) and url for url in downloads
        ):
            raise ValueError(
                f"{manifest_name}[{i}].downloads must be a non-empty array of URLs"
            )
        if not isinstance(hashes, dict) or not ("sha1" in hashes or "sha512" in hashes):
            raise ValueError(
                f"{manifest_name}[{i}].hashes must include at least sha1 or sha512"
            )
        if not isinstance(file_size, int) or file_size <= 0:
            raise ValueError(f"{manifest_name}[{i}].fileSize must be an integer > 0")

    return entries


def env_for_side(side: str) -> dict[str, str]:
    if side == "client":
        return {"client": "required", "server": "unsupported"}
    if side == "server":
        return {"client": "unsupported", "server": "required"}
    return {"client": "required", "server": "required"}


def build_files(entries: list[dict], side: str, path_prefix: str) -> list[dict]:
    files: list[dict] = []
    for entry in entries:
        if entry["side"] not in ("both", side):
            continue
        files.append(
            {
                "path": f"{path_prefix}/{entry['filename']}",
                "hashes": entry["hashes"],
                "downloads": entry["downloads"],
                "fileSize": entry["fileSize"],
                "env": env_for_side(entry["side"]),
            }
        )
    return files


def write_zip(output_path: Path, index_data: dict) -> None:
    with ZipFile(output_path, "w", compression=ZIP_DEFLATED) as zf:
        zf.writestr("modrinth.index.json", json.dumps(index_data, indent=2) + "\n")


def build_pack(
    *,
    template: dict,
    mods: list[dict],
    resource_packs: list[dict],
    shader_packs: list[dict],
    side: str,
    label: str,
    args: argparse.Namespace,
    slug: str,
    dist: str,
) -> None:
    mod_files = build_files(mods, side, "mods")
    resource_pack_files = build_files(resource_packs, side, "resourcepacks")
    shader_pack_files = build_files(shader_packs, side, "shaderpacks")
    files = [*mod_files, *resource_pack_files, *shader_pack_files]

    index_data = dict(template)
    if args.version:
        index_data["versionId"] = args.version
    index_data["name"] = f"{template['name']} ({label})"
    index_data["summary"] = f"{template['summary']} [{label}]"
    index_data["files"] = files

    pack_version = index_data["versionId"]
    dist_dir = Path(dist)
    dist_dir.mkdir(parents=True, exist_ok=True)
    out = dist_dir / f"{slug}-{side}-{pack_version}.mrpack"

    write_zip(out, index_data)

    print(
        f"Built {out} "
        f"(mods: {len(mod_files)}, resource packs: {len(resource_pack_files)}, shader packs: {len(shader_pack_files)})"
    )


def main() -> int:
    args = parse_args()
    build_defaults = read_pack_build_defaults(Path(args.pack))

    side = args.side or build_defaults.get("default_side", "both")
    if side not in VALID_SIDES:
        raise ValueError(f"--side must be one of {sorted(VALID_SIDES)}")

    slug = args.slug or build_defaults.get("slug", "chuj")
    dist = args.dist or build_defaults.get("dist_dir", "dist")

    template_path = Path(args.template)
    mods_path = Path(args.mods)
    resource_packs_path = Path(args.resource_packs)
    shader_packs_path = Path(args.shader_packs)

    if not template_path.exists():
        raise FileNotFoundError(f"Template not found: {template_path}")
    if not mods_path.exists():
        raise FileNotFoundError(f"Mods file not found: {mods_path}")
    if not resource_packs_path.exists():
        raise FileNotFoundError(f"Resource packs file not found: {resource_packs_path}")
    if not shader_packs_path.exists():
        raise FileNotFoundError(f"Shader packs file not found: {shader_packs_path}")

    template = read_json(template_path)
    if not isinstance(template, dict):
        raise ValueError("Template must be a JSON object")
    validate_template(template)

    mods = validate_entries(read_json(mods_path), str(mods_path))
    resource_packs = validate_entries(read_json(resource_packs_path), str(resource_packs_path))
    shader_packs = validate_entries(read_json(shader_packs_path), str(shader_packs_path))

    if side == "both":
        build_pack(
            template=template,
            mods=mods,
            resource_packs=resource_packs,
            shader_packs=shader_packs,
            side="client",
            label="Client",
            args=args,
            slug=slug,
            dist=dist,
        )
        build_pack(
            template=template,
            mods=mods,
            resource_packs=resource_packs,
            shader_packs=shader_packs,
            side="server",
            label="Server",
            args=args,
            slug=slug,
            dist=dist,
        )
    elif side == "client":
        build_pack(
            template=template,
            mods=mods,
            resource_packs=resource_packs,
            shader_packs=shader_packs,
            side="client",
            label="Client",
            args=args,
            slug=slug,
            dist=dist,
        )
    else:
        build_pack(
            template=template,
            mods=mods,
            resource_packs=resource_packs,
            shader_packs=shader_packs,
            side="server",
            label="Server",
            args=args,
            slug=slug,
            dist=dist,
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
