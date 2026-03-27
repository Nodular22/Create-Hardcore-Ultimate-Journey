#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
from typing import NamedTuple
from zipfile import ZIP_DEFLATED, ZipFile

from common import parse_project_id, read_json, read_toml, require_file
from logging_utils import configure_logging

REQUIRED_TEMPLATE_KEYS = [
    "formatVersion",
    "game",
    "name",
    "summary",
    "versionId",
    "dependencies",
]

VALID_SIDES = {"both", "client", "server"}
SIDE_LABELS = {"client": "Client", "server": "Server"}
LOGGER = logging.getLogger("build")


class OverrideFile(NamedTuple):
    source_path: Path
    archive_path: str
    install_path: str


OPTIONS_RESOURCE_PACKS_PREFIX = "resourcePacks:"
OPTIONS_INCOMPATIBLE_RESOURCE_PACKS_PREFIX = "incompatibleResourcePacks:"


def parse_options_string_list(raw_value: str, *, path: Path, label: str) -> list[str]:
    loaded = json.loads(raw_value)
    if not isinstance(loaded, list) or not all(isinstance(item, str) for item in loaded):
        raise ValueError(f"{path} {label} must be a JSON string array")
    return loaded


def format_options_string_list(prefix: str, values: list[str]) -> str:
    return f"{prefix}{json.dumps(values)}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build Prism-importable .mrpack files for client/server from lock manifests"
    )
    parser.add_argument("--pack", default="modpack/pack.toml")
    parser.add_argument("--version", default="", help="Override versionId")
    parser.add_argument("--template", default="modpack/pack.template.json")
    parser.add_argument("--mods", default="modpack/mods.lock.json")
    parser.add_argument("--resource-packs", default="modpack/resource-packs.lock.json")
    parser.add_argument("--shader-packs", default="modpack/shader-packs.lock.json")
    parser.add_argument("--dist", default="")
    parser.add_argument("--slug", default="")
    parser.add_argument("--side", default="")
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args()


def read_pack_build_defaults(data: dict) -> dict[str, str]:
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


def read_pack_overrides(data: dict) -> dict[str, list[str]]:
    if not isinstance(data, dict):
        return {"include": [], "client_include": [], "server_include": []}

    overrides = data.get("overrides")
    if not isinstance(overrides, dict):
        return {"include": [], "client_include": [], "server_include": []}

    parsed: dict[str, list[str]] = {}
    for key in ("include", "client_include", "server_include"):
        raw_value = overrides.get(key, [])
        if not isinstance(raw_value, list):
            raise ValueError(f"overrides.{key} must be an array of paths")

        values: list[str] = []
        for i, item in enumerate(raw_value):
            if not isinstance(item, str) or not item:
                raise ValueError(f"overrides.{key}[{i}] must be a non-empty string")
            values.append(item)
        parsed[key] = values

    return parsed


def read_default_enabled_resource_packs(data: dict) -> list[dict[str, str]]:
    if not isinstance(data, dict):
        return []

    resourcepacks = data.get("resourcepacks")
    if not isinstance(resourcepacks, dict):
        return []

    enabled_entries: list[dict[str, str]] = []
    for category, raw_category in resourcepacks.items():
        if category == "from_overrides":
            continue
        if not isinstance(category, str):
            raise ValueError("resourcepacks category names must be strings")
        if not isinstance(raw_category, dict):
            raise ValueError(f"resourcepacks.{category} must be a table")

        packs = raw_category.get("packs")
        if packs is None:
            continue
        if not isinstance(packs, list):
            raise ValueError(f"resourcepacks.{category}.packs must be an array")

        for i, raw_pack in enumerate(packs):
            if not isinstance(raw_pack, dict):
                raise ValueError(f"resourcepacks.{category}.packs[{i}] must be an object")

            is_enabled = raw_pack.get("enabled", True)
            if not isinstance(is_enabled, bool):
                raise ValueError(
                    f"resourcepacks.{category}.packs[{i}].enabled must be a boolean"
                )
            if not is_enabled:
                continue

            url = raw_pack.get("url")
            side = raw_pack.get("side")
            if not isinstance(url, str) or not url:
                raise ValueError(f"resourcepacks.{category}.packs[{i}].url must be a non-empty string")
            if side not in VALID_SIDES:
                raise ValueError(
                    f"resourcepacks.{category}.packs[{i}].side must be one of {sorted(VALID_SIDES)}"
                )

            enabled_entries.append({"project_id": parse_project_id(url), "side": side})

    return enabled_entries


def read_enabled_override_resource_pack_entries(data: dict) -> list[str]:
    if not isinstance(data, dict):
        return []

    resourcepacks = data.get("resourcepacks")
    if not isinstance(resourcepacks, dict):
        return []

    from_overrides = resourcepacks.get("from_overrides")
    if from_overrides is None:
        return []
    if not isinstance(from_overrides, dict):
        raise ValueError("resourcepacks.from_overrides must be a table")

    raw_entries = from_overrides.get("enabled", [])
    if not isinstance(raw_entries, list):
        raise ValueError("resourcepacks.from_overrides.enabled must be an array")

    entries: list[str] = []
    for i, raw_entry in enumerate(raw_entries):
        if not isinstance(raw_entry, str) or not raw_entry:
            raise ValueError(
                f"resourcepacks.from_overrides.enabled[{i}] must be a non-empty string"
            )
        entries.append(f"file/{raw_entry}")
    return entries


def read_options_resource_packs(path: Path) -> list[str]:
    if not path.exists():
        return []

    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith(OPTIONS_RESOURCE_PACKS_PREFIX):
            raw_value = line[len(OPTIONS_RESOURCE_PACKS_PREFIX) :]
            return parse_options_string_list(raw_value, path=path, label="resourcePacks")
    return []


def build_default_resource_pack_entries(
    *,
    pack_data: dict,
    options_path: Path,
    resource_packs: list[dict],
) -> list[str]:
    enabled_configs = read_default_enabled_resource_packs(pack_data)
    override_enabled_entries = read_enabled_override_resource_pack_entries(pack_data)

    lock_by_project_id: dict[str, dict] = {}
    lock_filenames: set[str] = set()
    for entry in resource_packs:
        filename = entry.get("filename")
        project_id = entry.get("projectId")
        if isinstance(filename, str) and filename:
            lock_filenames.add(filename)
        if isinstance(project_id, str) and project_id:
            lock_by_project_id[project_id] = entry

    resolved_entries = ["vanilla"]
    for entry in enabled_configs:
        if entry["side"] not in {"both", "client"}:
            continue
        lock_entry = lock_by_project_id.get(entry["project_id"])
        if lock_entry is None:
            raise ValueError(
                "Missing resolved resource pack in lockfile for enabled resource pack project "
                f"'{entry['project_id']}'. Re-run scripts/resolve_manifests.py --target resourcepacks"
            )

        filename = lock_entry.get("filename")
        if not isinstance(filename, str) or not filename:
            raise ValueError(
                f"Resolved resource pack for project '{entry['project_id']}' is missing filename"
            )
        resolved_entries.append(f"file/{filename}")

    resolved_entries.extend(override_enabled_entries)

    existing_entries = read_options_resource_packs(options_path)
    for existing in existing_entries:
        if existing == "vanilla" or not existing.startswith("file/"):
            continue
        filename = existing.removeprefix("file/")
        if filename not in lock_filenames:
            resolved_entries.append(existing)

    deduped_entries: list[str] = []
    seen_entries: set[str] = set()
    for entry in resolved_entries:
        if entry in seen_entries:
            continue
        seen_entries.add(entry)
        deduped_entries.append(entry)

    return deduped_entries


def render_options_with_resource_packs(path: Path, resource_packs: list[str]) -> str:
    content = path.read_text(encoding="utf-8") if path.exists() else ""
    lines = content.splitlines()
    rendered_resource_packs = format_options_string_list(
        OPTIONS_RESOURCE_PACKS_PREFIX,
        resource_packs,
    )
    rendered_incompatible = format_options_string_list(
        OPTIONS_INCOMPATIBLE_RESOURCE_PACKS_PREFIX,
        [],
    )

    rendered_lines: list[str] = []
    saw_resource_packs = False
    saw_incompatible = False

    for line in lines:
        if line.startswith(OPTIONS_RESOURCE_PACKS_PREFIX):
            rendered_lines.append(rendered_resource_packs)
            saw_resource_packs = True
            continue
        if line.startswith(OPTIONS_INCOMPATIBLE_RESOURCE_PACKS_PREFIX):
            rendered_lines.append(rendered_incompatible)
            saw_incompatible = True
            continue
        rendered_lines.append(line)

    if not saw_resource_packs:
        rendered_lines.append(rendered_resource_packs)
    if not saw_incompatible:
        rendered_lines.append(rendered_incompatible)

    return "\n".join(rendered_lines) + "\n"


def maybe_generate_options_override(
    *,
    pack_data: dict,
    override_files: list[OverrideFile],
    resource_packs: list[dict],
) -> list[OverrideFile]:
    options_override = next(
        (override_file for override_file in override_files if override_file.install_path == "options.txt"),
        None,
    )
    if options_override is None:
        return override_files

    default_entries = build_default_resource_pack_entries(
        pack_data=pack_data,
        options_path=options_override.source_path,
        resource_packs=resource_packs,
    )
    rendered_options = render_options_with_resource_packs(
        options_override.source_path,
        default_entries,
    )
    options_override.source_path.write_text(rendered_options, encoding="utf-8")
    return override_files


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


def override_prefix_for_side(side: str) -> str:
    if side == "client":
        return "client-overrides"
    if side == "server":
        return "server-overrides"
    return "overrides"


def strip_override_source_prefix(path: Path, category: str) -> Path:
    expected_prefixes = {
        "include": Path("overrides/shared"),
        "client_include": Path("overrides/client"),
        "server_include": Path("overrides/server"),
    }
    prefix = expected_prefixes[category]
    try:
        return path.relative_to(prefix)
    except ValueError:
        return Path(path.name)


def collect_override_files(paths: list[str], category: str) -> list[OverrideFile]:
    archive_root = override_prefix_for_side(
        "client" if category == "client_include" else "server" if category == "server_include" else "both"
    )
    collected: list[OverrideFile] = []
    seen_install_paths: set[str] = set()

    for raw_path in paths:
        source_path = Path(raw_path)
        require_file(source_path, f"Override path '{raw_path}'")

        if source_path.is_dir():
            base_install_path = strip_override_source_prefix(source_path, category)
            file_paths = sorted(path for path in source_path.rglob("*") if path.is_file())
            for file_path in file_paths:
                relative_path = file_path.relative_to(source_path)
                install_path = (base_install_path / relative_path).as_posix()
                if install_path in seen_install_paths:
                    raise ValueError(f"Duplicate override install path: {install_path}")
                seen_install_paths.add(install_path)
                collected.append(
                    OverrideFile(
                        source_path=file_path,
                        archive_path=f"{archive_root}/{install_path}",
                        install_path=install_path,
                    )
                )
            continue

        install_path = strip_override_source_prefix(source_path, category).as_posix()
        if install_path in seen_install_paths:
            raise ValueError(f"Duplicate override install path: {install_path}")
        seen_install_paths.add(install_path)
        collected.append(
            OverrideFile(
                source_path=source_path,
                archive_path=f"{archive_root}/{install_path}",
                install_path=install_path,
            )
        )

    return collected


def override_category_for_build_side(build_side: str) -> str:
    return "client_include" if build_side == "client" else "server_include"


def write_zip(output_path: Path, index_data: dict, override_files: list[OverrideFile]) -> None:
    with ZipFile(output_path, "w", compression=ZIP_DEFLATED) as zf:
        zf.writestr("modrinth.index.json", json.dumps(index_data, indent=2) + "\n")
        for override_file in override_files:
            zf.write(override_file.source_path, arcname=override_file.archive_path)


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
    override_files: list[OverrideFile],
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

    pack_file_paths = {file_entry["path"] for file_entry in files}
    seen_override_paths: set[str] = set()
    for override_file in override_files:
        if override_file.install_path in seen_override_paths:
            raise ValueError(
                f"Duplicate override install path in build inputs: {override_file.install_path}"
            )
        seen_override_paths.add(override_file.install_path)
        if override_file.install_path in pack_file_paths:
            raise ValueError(
                f"Override path conflicts with downloaded file path: {override_file.install_path}"
            )

    pack_version = index_data["versionId"]
    dist_dir = Path(dist)
    dist_dir.mkdir(parents=True, exist_ok=True)
    out = dist_dir / f"{slug}-{side}-{pack_version}.mrpack"

    write_zip(out, index_data, override_files)

    LOGGER.info(
        "%s pack: built %s (mods: %d, resource packs: %d, shader packs: %d, overrides: %d)",
        SIDE_LABELS[side],
        out,
        len(mod_files),
        len(resource_pack_files),
        len(shader_pack_files),
        len(override_files),
    )


def sides_to_build(side: str) -> list[str]:
    if side == "both":
        return ["client", "server"]
    return [side]


def main() -> int:
    args = parse_args()
    configure_logging(verbose=args.verbose)
    try:
        pack_path = Path(args.pack)
        pack_data = read_toml(pack_path)
        build_defaults = read_pack_build_defaults(pack_data)

        side = args.side or build_defaults.get("default_side", "both")
        if side not in VALID_SIDES:
            raise ValueError(f"--side must be one of {sorted(VALID_SIDES)}")

        slug = args.slug or build_defaults.get("slug", "chuj")
        dist = args.dist or build_defaults.get("dist_dir", "dist")
        override_config = read_pack_overrides(pack_data)

        template_path = Path(args.template)
        mods_path = Path(args.mods)
        resource_packs_path = Path(args.resource_packs)
        shader_packs_path = Path(args.shader_packs)

        require_file(template_path, "Template")
        require_file(mods_path, "Mods file")
        require_file(resource_packs_path, "Resource packs file")
        require_file(shader_packs_path, "Shader packs file")

        template = read_json(template_path)
        if not isinstance(template, dict):
            raise ValueError("Template must be a JSON object")
        validate_template(template)

        mods = validate_entries(read_json(mods_path), str(mods_path))
        resource_packs = validate_entries(read_json(resource_packs_path), str(resource_packs_path))
        shader_packs = validate_entries(read_json(shader_packs_path), str(shader_packs_path))

        for build_side in sides_to_build(side):
            side_override_category = override_category_for_build_side(build_side)
            selected_override_files = [
                *collect_override_files(override_config["include"], "include"),
                *collect_override_files(override_config[side_override_category], side_override_category),
            ]
            if build_side == "client":
                selected_override_files = maybe_generate_options_override(
                    pack_data=pack_data,
                    override_files=selected_override_files,
                    resource_packs=resource_packs,
                )
            build_pack(
                template=template,
                mods=mods,
                resource_packs=resource_packs,
                shader_packs=shader_packs,
                side=build_side,
                label=SIDE_LABELS[build_side],
                args=args,
                slug=slug,
                dist=dist,
                override_files=selected_override_files,
            )

        return 0
    except Exception as exc:
        if args.verbose:
            raise
        LOGGER.error("Error: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
