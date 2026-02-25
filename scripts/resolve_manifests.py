#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import json
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

USER_AGENT = "CHUJ-Pack-Resolver"
VALID_SIDES = {"both", "client", "server"}
SUPPORTED_LOADERS = ("fabric", "forge", "quilt", "neoforge")
MODRINTH_FORMAT_VERSION = 1
MODRINTH_GAME = "minecraft"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Render template and resolve lock manifests from modpack/pack.toml"
    )
    parser.add_argument("--pack", default="modpack/pack.toml")
    parser.add_argument("--template-out", default="modpack/pack.template.json")
    parser.add_argument("--mods-lock", default="modpack/mods.lock.json")
    parser.add_argument(
        "--resource-packs-lock", default="modpack/resource-packs.lock.json"
    )
    parser.add_argument("--shader-packs-lock", default="modpack/shader-packs.lock.json")
    parser.add_argument(
        "--target",
        default="all",
        choices=["all", "template", "mods", "resourcepacks", "shaderpacks"],
        help="Render a specific output or all",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Validate and resolve without writing files",
    )
    return parser.parse_args()


def read_toml(path: Path) -> dict:
    with path.open("rb") as f:
        data = tomllib.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a TOML object")
    return data


def write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")


def as_table(raw: object, name: str) -> dict:
    if not isinstance(raw, dict):
        raise ValueError(f"{name} must be a TOML table")
    return raw


def as_nonempty_str(raw: object, field_name: str) -> str:
    if not isinstance(raw, str) or not raw:
        raise ValueError(f"{field_name} must be a non-empty string")
    return raw


def parse_template(data: dict) -> tuple[dict, str, str]:
    pack = as_table(data.get("pack"), "pack")
    deps = as_table(data.get("dependencies"), "dependencies")
    name = as_nonempty_str(pack.get("name"), "pack.name")
    summary = as_nonempty_str(pack.get("summary"), "pack.summary")
    version_id = as_nonempty_str(pack.get("debug_version"), "pack.debug_version")

    minecraft = as_nonempty_str(deps.get("minecraft"), "dependencies.minecraft")
    loader = as_nonempty_str(deps.get("loader"), "dependencies.loader")
    loader_version = as_nonempty_str(
        deps.get("loader_version"), "dependencies.loader_version"
    )

    if loader not in SUPPORTED_LOADERS:
        raise ValueError(
            f"dependencies.loader must be one of {SUPPORTED_LOADERS}, got '{loader}'"
        )

    template = {
        "formatVersion": MODRINTH_FORMAT_VERSION,
        "game": MODRINTH_GAME,
        "name": name,
        "summary": summary,
        "versionId": version_id,
        "dependencies": {
            "minecraft": minecraft,
            loader: loader_version,
        },
    }
    return template, minecraft, loader


def normalize_project_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError(f"Invalid project URL: {url}")
    if parsed.netloc not in {"modrinth.com", "www.modrinth.com"}:
        raise ValueError(f"Unsupported host in URL: {url}")

    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) != 2 or parts[0] != "project":
        raise ValueError(f"Invalid Modrinth project URL path: {url}")

    return f"https://modrinth.com/project/{parts[1]}"


def collect_entries(data: dict, top_key: str, item_key: str) -> list[dict]:
    top = data.get(top_key)
    if top is None:
        return []

    top_table = as_table(top, top_key)
    flattened: list[dict] = []
    for category, raw_category in top_table.items():
        category_table = as_table(raw_category, f"{top_key}.{category}")
        items = category_table.get(item_key)
        if not isinstance(items, list):
            raise ValueError(f"{top_key}.{category}.{item_key} must be an array")

        for i, raw_item in enumerate(items):
            if not isinstance(raw_item, dict):
                raise ValueError(
                    f"{top_key}.{category}.{item_key}[{i}] must be an object"
                )

            url = as_nonempty_str(
                raw_item.get("url"), f"{top_key}.{category}.{item_key}[{i}].url"
            )
            side = as_nonempty_str(
                raw_item.get("side"), f"{top_key}.{category}.{item_key}[{i}].side"
            )
            if side not in VALID_SIDES:
                raise ValueError(
                    f"{top_key}.{category}.{item_key}[{i}].side must be one of {sorted(VALID_SIDES)}"
                )

            version = raw_item.get("version")
            if version is not None and (not isinstance(version, str) or not version):
                raise ValueError(
                    f"{top_key}.{category}.{item_key}[{i}].version must be a non-empty string when provided"
                )

            entry: dict[str, str] = {
                "url": normalize_project_url(url),
                "side": side,
            }
            if isinstance(raw_item.get("name"), str) and raw_item.get("name"):
                entry["name"] = raw_item["name"]
            if isinstance(version, str):
                entry["version"] = version

            flattened.append(entry)

    return flattened


def parse_project_id(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) != 2 or parts[0] != "project":
        raise ValueError(f"Invalid Modrinth project URL path: {url}")
    return parts[1]


def request_json(url: str) -> object:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Modrinth API returned {exc.code} for {url}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Network error while requesting {url}: {exc}") from exc


def fetch_versions(project_id: str) -> list[dict]:
    url = f"https://api.modrinth.com/v2/project/{project_id}/version"
    data = request_json(url)
    if not isinstance(data, list):
        raise RuntimeError(f"Unexpected versions response for project '{project_id}'")

    return [item for item in data if isinstance(item, dict)]


def filter_versions(
    versions: list[dict], *, minecraft: str, loader: str | None
) -> list[dict]:
    filtered: list[dict] = []
    for version in versions:
        game_versions = version.get("game_versions")
        if isinstance(game_versions, list) and game_versions and minecraft not in game_versions:
            continue

        if loader is not None:
            loaders = version.get("loaders")
            if not isinstance(loaders, list) or loader not in loaders:
                continue

        filtered.append(version)
    return filtered


def parse_timestamp(ts: str) -> dt.datetime:
    return dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))


def version_sort_key(version: dict) -> dt.datetime:
    return parse_timestamp(str(version.get("date_published", "1970-01-01T00:00:00Z")))


def select_version(versions: list[dict], version_number: str | None) -> dict:
    if not versions:
        raise ValueError("No compatible versions found")

    if version_number:
        matched = [v for v in versions if v.get("version_number") == version_number]
        if not matched:
            raise ValueError(f"No compatible version_number='{version_number}' found")
        if len(matched) > 1:
            matched.sort(key=version_sort_key, reverse=True)
        return matched[0]

    versions_sorted = sorted(versions, key=version_sort_key, reverse=True)
    return versions_sorted[0]


def select_file(version_data: dict) -> dict:
    files = version_data.get("files")
    if not isinstance(files, list) or not files:
        raise ValueError("Selected Modrinth version has no files")

    primary = [f for f in files if isinstance(f, dict) and f.get("primary") is True]
    if primary:
        return primary[0]

    first = files[0]
    if not isinstance(first, dict):
        raise ValueError("Invalid file object in version response")
    return first


def build_lock_entry(file_obj: dict, side: str) -> dict:
    filename = file_obj.get("filename")
    url = file_obj.get("url")
    size = file_obj.get("size")
    hashes = file_obj.get("hashes")

    if not isinstance(filename, str) or not filename:
        raise ValueError("Missing filename in Modrinth response")
    if not isinstance(url, str) or not url:
        raise ValueError("Missing download URL in Modrinth response")
    if not isinstance(size, int) or size <= 0:
        raise ValueError("Missing/invalid file size in Modrinth response")
    if not isinstance(hashes, dict):
        raise ValueError("Missing hashes in Modrinth response")

    out_hashes: dict[str, str] = {}
    sha1 = hashes.get("sha1")
    sha512 = hashes.get("sha512")
    if isinstance(sha1, str) and sha1:
        out_hashes["sha1"] = sha1
    if isinstance(sha512, str) and sha512:
        out_hashes["sha512"] = sha512

    if not out_hashes:
        raise ValueError("No sha1/sha512 hash provided by Modrinth")

    return {
        "filename": filename,
        "side": side,
        "downloads": [url],
        "hashes": out_hashes,
        "fileSize": size,
    }


def resolve_entries(
    entries: list[dict], minecraft: str, loader: str, label: str, require_loader: bool
) -> list[dict]:
    resolved: list[dict] = []
    seen_filenames: set[str] = set()

    for i, entry in enumerate(entries):
        project_id = parse_project_id(entry["url"])
        versions = fetch_versions(project_id)
        filtered_versions = filter_versions(
            versions,
            minecraft=minecraft,
            loader=loader if require_loader else None,
        )
        selected_version = select_version(filtered_versions, entry.get("version"))
        selected_file = select_file(selected_version)
        lock_entry = build_lock_entry(selected_file, entry["side"])

        filename = lock_entry["filename"]
        if filename in seen_filenames:
            raise ValueError(
                f"Duplicate resolved filename '{filename}' in {label}; this is ambiguous"
            )
        seen_filenames.add(filename)
        resolved.append(lock_entry)

        print(f"Resolved {label}[{i}]: {entry['url']} -> {filename}")

    resolved.sort(key=lambda x: x["filename"].lower())
    return resolved


def main() -> int:
    args = parse_args()

    pack_path = Path(args.pack)
    if not pack_path.exists():
        raise FileNotFoundError(f"Pack config not found: {pack_path}")

    data = read_toml(pack_path)
    template, minecraft, loader = parse_template(data)

    mods_entries = collect_entries(data, "mods", "mods")
    resource_pack_entries = collect_entries(data, "resourcepacks", "packs")
    shader_pack_entries = collect_entries(data, "shaderpacks", "packs")

    locks = {
        "mods": (mods_entries, Path(args.mods_lock)),
        "resourcepacks": (resource_pack_entries, Path(args.resource_packs_lock)),
        "shaderpacks": (shader_pack_entries, Path(args.shader_packs_lock)),
    }

    targets = (
        ["template", "mods", "resourcepacks", "shaderpacks"]
        if args.target == "all"
        else [args.target]
    )

    if "template" in targets:
        if args.check:
            print(f"Validated template render -> {args.template_out}")
        else:
            write_json(Path(args.template_out), template)
            print(f"Rendered template -> {args.template_out}")

    for target in ("mods", "resourcepacks", "shaderpacks"):
        if target not in targets:
            continue

        entries, lock_path = locks[target]
        resolved = resolve_entries(
            entries,
            minecraft,
            loader,
            target,
            require_loader=(target == "mods"),
        )
        if args.check:
            print(f"Validated {target} -> {lock_path} ({len(resolved)} entries)")
        else:
            write_json(lock_path, resolved)
            print(f"Wrote {target} lock -> {lock_path} ({len(resolved)} entries)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
