#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import NamedTuple

from common import parse_project_id, read_toml, write_json
from logging_utils import configure_logging

USER_AGENT = "CHUJ-Pack-Resolver"
VALID_SIDES = {"both", "client", "server"}
SUPPORTED_LOADERS = ("fabric", "forge", "quilt", "neoforge")
MODRINTH_FORMAT_VERSION = 1
MODRINTH_GAME = "minecraft"
TARGET_LABELS = {
    "mods": "Mods",
    "resourcepacks": "Resource packs",
    "shaderpacks": "Shader packs",
    "template": "Template",
}
LOGGER = logging.getLogger("resolve")


class ResolutionResult(NamedTuple):
    resolved: list[dict]
    root_projects: int


@dataclass
class Requirement:
    project_id: str
    url: str
    side: str
    version: str | None = None
    required_version_ids: set[str] = field(default_factory=set)
    additional_dependencies: set[str] = field(default_factory=set)

    def state(self) -> tuple:
        return (
            self.side,
            self.version,
            tuple(sorted(self.required_version_ids)),
            tuple(sorted(self.additional_dependencies)),
        )


def compact_version(version: dict) -> str:
    version_number = version.get("version_number")
    version_id = version.get("id")
    if not isinstance(version_number, str) or not version_number:
        version_number = "-"
    if not isinstance(version_id, str) or not version_id:
        version_id = "-"
    return f"{version_number} [{version_id}]"


def display_target(label: str) -> str:
    return TARGET_LABELS.get(label, label.replace("_", " ").title())


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
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Show per-project resolution details",
    )
    return parser.parse_args()


def as_table(raw: object, name: str) -> dict:
    if not isinstance(raw, dict):
        raise ValueError(f"{name} must be a TOML table")
    return raw


def as_nonempty_str(raw: object, field_name: str) -> str:
    if not isinstance(raw, str) or not raw:
        raise ValueError(f"{field_name} must be a non-empty string")
    return raw


def as_nonempty_str_list(raw: object, field_name: str) -> list[str]:
    if not isinstance(raw, list) or not raw:
        raise ValueError(f"{field_name} must be a non-empty array of strings")

    values: list[str] = []
    for i, item in enumerate(raw):
        values.append(as_nonempty_str(item, f"{field_name}[{i}]"))
    return values


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


def parse_manifest_entry(raw_item: object, field_prefix: str) -> dict[str, str]:
    if not isinstance(raw_item, dict):
        raise ValueError(f"{field_prefix} must be an object")

    url = as_nonempty_str(raw_item.get("url"), f"{field_prefix}.url")
    side = as_nonempty_str(raw_item.get("side"), f"{field_prefix}.side")
    if side not in VALID_SIDES:
        raise ValueError(f"{field_prefix}.side must be one of {sorted(VALID_SIDES)}")

    version = raw_item.get("version")
    if version is not None and (not isinstance(version, str) or not version):
        raise ValueError(f"{field_prefix}.version must be a non-empty string when provided")

    entry: dict[str, str] = {
        "url": normalize_project_url(url),
        "side": side,
    }
    if isinstance(raw_item.get("name"), str) and raw_item.get("name"):
        entry["name"] = raw_item["name"]
    if isinstance(version, str):
        entry["version"] = version
    return entry


def parse_mod_entry(raw_item: object, field_prefix: str) -> dict[str, str]:
    entry = parse_manifest_entry(raw_item, field_prefix)
    item = as_table(raw_item, field_prefix)
    additional_dependencies = item.get("additional_dependencies")
    if additional_dependencies is not None:
        entry["additional_dependencies"] = [
            normalize_project_url(url)
            for url in as_nonempty_str_list(
                additional_dependencies,
                f"{field_prefix}.additional_dependencies",
            )
        ]
    return entry


def collect_mod_entries(data: dict) -> list[dict]:
    mods = data.get("mods")
    if mods is None:
        return []

    mods_table = as_table(mods, "mods")
    flattened: list[dict] = []
    for category, raw_category in mods_table.items():
        category_table = as_table(raw_category, f"mods.{category}")
        raw_items = category_table.get("mods")
        if not isinstance(raw_items, list):
            raise ValueError(f"mods.{category}.mods must be an array")

        for i, raw_item in enumerate(raw_items):
            field_prefix = f"mods.{category}.mods[{i}]"
            flattened.append(parse_mod_entry(raw_item, field_prefix))

    return flattened


def collect_pack_entries(data: dict, top_key: str) -> list[dict]:
    top = data.get(top_key)
    if top is None:
        return []

    top_table = as_table(top, top_key)
    flattened: list[dict] = []
    for category, raw_category in top_table.items():
        if top_key == "resourcepacks" and category == "from_overrides":
            continue

        category_table = as_table(raw_category, f"{top_key}.{category}")
        raw_items = category_table.get("packs")
        if not isinstance(raw_items, list):
            raise ValueError(f"{top_key}.{category}.packs must be an array")

        for i, raw_item in enumerate(raw_items):
            flattened.append(
                parse_manifest_entry(raw_item, f"{top_key}.{category}.packs[{i}]")
            )

    return flattened


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


def fetch_version(version_id: str) -> dict:
    url = f"https://api.modrinth.com/v2/version/{version_id}"
    data = request_json(url)
    if not isinstance(data, dict):
        raise RuntimeError(f"Unexpected version response for version '{version_id}'")
    return data


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


def build_lock_entry(file_obj: dict, side: str, *, project_id: str | None = None) -> dict:
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

    lock_entry = {
        "filename": filename,
        "side": side,
        "downloads": [url],
        "hashes": out_hashes,
        "fileSize": size,
    }
    if project_id is not None:
        lock_entry["projectId"] = project_id
    return lock_entry


def merge_side(current: str | None, incoming: str) -> str:
    if current is None or current == incoming:
        return incoming
    return "both"


def parse_required_dependencies(version_data: dict) -> list[dict]:
    dependencies = version_data.get("dependencies")
    if not isinstance(dependencies, list):
        return []

    required_dependencies: list[dict] = []
    for dependency in dependencies:
        if not isinstance(dependency, dict):
            continue
        if dependency.get("dependency_type") != "required":
            continue

        project_id = dependency.get("project_id")
        version_id = dependency.get("version_id")
        file_name = dependency.get("file_name")

        if project_id is not None and not isinstance(project_id, str):
            raise ValueError("Required dependency project_id must be a string when provided")
        if version_id is not None and not isinstance(version_id, str):
            raise ValueError("Required dependency version_id must be a string when provided")
        if file_name is not None and not isinstance(file_name, str):
            raise ValueError("Required dependency file_name must be a string when provided")
        if not project_id and not version_id:
            raise ValueError(
                "Required dependency must include at least project_id or version_id"
            )

        dep: dict[str, str] = {}
        if isinstance(project_id, str) and project_id:
            dep["project_id"] = project_id
        if isinstance(version_id, str) and version_id:
            dep["version_id"] = version_id
        if isinstance(file_name, str) and file_name:
            dep["file_name"] = file_name
        required_dependencies.append(dep)

    return required_dependencies


def queue_project(project_id: str, *, queue: deque[str], queued: set[str]) -> None:
    if project_id in queued:
        return
    queued.add(project_id)
    queue.append(project_id)


def get_versions(project_id: str, *, versions_cache: dict[str, list[dict]]) -> list[dict]:
    cached = versions_cache.get(project_id)
    if cached is None:
        cached = fetch_versions(project_id)
        versions_cache[project_id] = cached
    return cached


def get_version(version_id: str, *, version_cache: dict[str, dict]) -> dict:
    cached = version_cache.get(version_id)
    if cached is None:
        cached = fetch_version(version_id)
        version_cache[version_id] = cached
    return cached


def ensure_project_requirement(
    requirements: dict[str, Requirement],
    *,
    queue: deque[str],
    queued: set[str],
    project_id: str,
    side: str,
    url: str | None = None,
    version_number: str | None = None,
    version_id: str | None = None,
    additional_dependencies: list[str] | None = None,
) -> None:
    requirement = requirements.get(project_id)
    changed = False

    if requirement is None:
        requirement = Requirement(
            project_id=project_id,
            url=url or f"https://modrinth.com/project/{project_id}",
            side=side,
            version=version_number,
        )
        requirements[project_id] = requirement
        changed = True
    else:
        merged_side = merge_side(requirement.side, side)
        if merged_side != requirement.side:
            requirement.side = merged_side
            changed = True

        if url is not None and requirement.url != url:
            requirement.url = url
            changed = True

        existing_version = requirement.version
        if version_number is not None:
            if existing_version is not None and existing_version != version_number:
                raise ValueError(
                    f"Conflicting explicit versions for project '{project_id}': "
                    f"'{existing_version}' vs '{version_number}'"
                )
            if existing_version is None:
                requirement.version = version_number
                changed = True

    if version_id is not None and version_id not in requirement.required_version_ids:
        requirement.required_version_ids.add(version_id)
        changed = True

    if additional_dependencies:
        before = len(requirement.additional_dependencies)
        requirement.additional_dependencies.update(additional_dependencies)
        if len(requirement.additional_dependencies) != before:
            changed = True

    if changed:
        queue_project(project_id, queue=queue, queued=queued)


def select_project_version(
    *,
    project_id: str,
    requirement: Requirement,
    root_projects: set[str],
    minecraft: str,
    loader: str,
    require_loader: bool,
    versions_cache: dict[str, list[dict]],
    version_cache: dict[str, dict],
) -> tuple[dict, str, int | None, int | None, list[str]]:
    required_version_ids = sorted(requirement.required_version_ids)
    if len(required_version_ids) > 1:
        raise ValueError(
            f"Conflicting required version_ids for project '{project_id}': {required_version_ids}"
        )

    latest_compatible_version: dict | None = None
    total_versions: int | None = None
    compatible_versions: int | None = None
    selection_source = "compatible"

    if project_id in root_projects or not required_version_ids:
        versions = get_versions(project_id, versions_cache=versions_cache)
        filtered_versions = filter_versions(
            versions,
            minecraft=minecraft,
            loader=loader if require_loader else None,
        )
        total_versions = len(versions)
        compatible_versions = len(filtered_versions)
        if not filtered_versions:
            raise ValueError(
                f"No compatible versions found for project '{project_id}' "
                f"(minecraft={minecraft}, loader={loader if require_loader else 'any'}, "
                f"total_versions={total_versions})"
            )
        latest_compatible_version = select_version(filtered_versions, requirement.version)

    if required_version_ids:
        dependency_selected_version = get_version(
            required_version_ids[0], version_cache=version_cache
        )
        selected_project_id = dependency_selected_version.get("project_id")
        if not isinstance(selected_project_id, str) or not selected_project_id:
            raise ValueError(
                f"Resolved version '{required_version_ids[0]}' is missing project_id"
            )
        if selected_project_id != project_id:
            raise ValueError(
                f"Resolved version '{required_version_ids[0]}' belongs to project "
                f"'{selected_project_id}', expected '{project_id}'"
            )
        if latest_compatible_version is not None and requirement.version is None:
            if version_sort_key(dependency_selected_version) > version_sort_key(
                latest_compatible_version
            ):
                selected_version = dependency_selected_version
                selection_source = f"dependency {required_version_ids[0]}"
            else:
                selected_version = latest_compatible_version
        else:
            selected_version = dependency_selected_version
            selection_source = f"dependency {required_version_ids[0]}"
    else:
        if latest_compatible_version is None:
            raise ValueError(
                f"No compatible version selection could be computed for '{project_id}'"
            )
        selected_version = latest_compatible_version

    explicit_version = requirement.version
    if explicit_version is not None and selected_version.get("version_number") != explicit_version:
        raise ValueError(
            f"Resolved version conflict for project '{project_id}': expected "
            f"version_number='{explicit_version}'"
        )

    return (
        selected_version,
        selection_source,
        total_versions,
        compatible_versions,
        required_version_ids,
    )


def log_selected_version(
    *,
    label: str,
    project_id: str,
    requirement: Requirement,
    selected_version: dict,
    selection_source: str,
    total_versions: int | None,
    compatible_versions: int | None,
    required_version_ids: list[str],
) -> None:
    compatibility_summary = ""
    if total_versions is not None and compatible_versions is not None:
        compatibility_summary = f" compatible={compatible_versions}/{total_versions}"
    explicit_version = requirement.version
    explicit_summary = f" explicit={explicit_version}" if explicit_version is not None else ""
    required_summary = (
        f" required={required_version_ids[0]}" if required_version_ids else ""
    )
    LOGGER.debug(
        "%s: select %s side=%s via=%s%s%s%s -> %s",
        display_target(label),
        project_id,
        requirement.side,
        selection_source,
        compatibility_summary,
        explicit_summary,
        required_summary,
        compact_version(selected_version),
    )


def queue_version_dependencies(
    *,
    label: str,
    project_id: str,
    side: str,
    selected_version: dict,
    requirements: dict[str, Requirement],
    queue: deque[str],
    queued: set[str],
    version_cache: dict[str, dict],
) -> None:
    for dependency in parse_required_dependencies(selected_version):
        dependency_project_id = dependency.get("project_id")
        dependency_version_id = dependency.get("version_id")

        if dependency_project_id is None:
            if dependency_version_id is None:
                raise ValueError(
                    f"Required dependency for project '{project_id}' could not be resolved"
                )
            dependency_version = get_version(dependency_version_id, version_cache=version_cache)
            raw_project_id = dependency_version.get("project_id")
            if not isinstance(raw_project_id, str) or not raw_project_id:
                raise ValueError(
                    f"Required dependency version '{dependency_version_id}' is missing project_id"
                )
            dependency_project_id = raw_project_id

        LOGGER.debug(
            "%s: dependency %s -> %s%s",
            display_target(label),
            project_id,
            dependency_project_id,
            f" version={dependency_version_id}" if dependency_version_id else "",
        )

        ensure_project_requirement(
            requirements,
            queue=queue,
            queued=queued,
            project_id=dependency_project_id,
            side=side,
            version_id=dependency_version_id,
        )


def queue_manual_dependencies(
    *,
    label: str,
    project_id: str,
    side: str,
    requirement: Requirement,
    requirements: dict[str, Requirement],
    queue: deque[str],
    queued: set[str],
) -> None:
    for dependency_url in sorted(requirement.additional_dependencies):
        dependency_project_id = parse_project_id(dependency_url)
        LOGGER.debug(
            "%s: manual dependency %s -> %s",
            display_target(label),
            project_id,
            dependency_project_id,
        )
        ensure_project_requirement(
            requirements,
            queue=queue,
            queued=queued,
            project_id=dependency_project_id,
            side=side,
            url=dependency_url,
        )


def log_root_resolutions(
    entries: list[dict], *, label: str, selected_versions: dict[str, dict]
) -> None:
    for i, entry in enumerate(entries):
        root_project_id = parse_project_id(entry["url"])
        selected_version = selected_versions[root_project_id]
        selected_file = select_file(selected_version)
        filename = selected_file.get("filename")
        if not isinstance(filename, str) or not filename:
            raise ValueError("Missing filename in Modrinth response")
        LOGGER.debug(
            "%s: root[%d] %s -> %s",
            display_target(label),
            i,
            root_project_id,
            filename,
        )


def build_resolved_entries(
    *,
    label: str,
    selected_versions: dict[str, dict],
    requirements: dict[str, Requirement],
) -> list[dict]:
    resolved: list[dict] = []
    seen_filenames: set[str] = set()

    for project_id, selected_version in selected_versions.items():
        requirement = requirements[project_id]
        selected_file = select_file(selected_version)
        lock_entry = build_lock_entry(
            selected_file,
            requirement.side,
            project_id=project_id,
        )

        filename = lock_entry["filename"]
        if filename in seen_filenames:
            raise ValueError(
                f"Duplicate resolved filename '{filename}' in {label}; this is ambiguous"
            )
        seen_filenames.add(filename)
        resolved.append(lock_entry)

    resolved.sort(key=lambda x: x["filename"].lower())
    return resolved


def resolve_entries(
    entries: list[dict],
    minecraft: str,
    loader: str,
    label: str,
    require_loader: bool,
    follow_required_dependencies: bool,
) -> ResolutionResult:
    queued: set[str] = set()
    queue: deque[str] = deque()
    requirements: dict[str, Requirement] = {}
    selected_versions: dict[str, dict] = {}
    processed_state: dict[str, tuple] = {}
    versions_cache: dict[str, list[dict]] = {}
    version_cache: dict[str, dict] = {}
    root_projects = {parse_project_id(entry["url"]) for entry in entries}

    LOGGER.info("%s: resolving %d root entries", display_target(label), len(entries))

    for entry in entries:
        ensure_project_requirement(
            requirements,
            queue=queue,
            queued=queued,
            project_id=parse_project_id(entry["url"]),
            side=entry["side"],
            url=entry["url"],
            version_number=entry.get("version"),
            additional_dependencies=entry.get("additional_dependencies"),
        )

    while queue:
        project_id = queue.popleft()
        queued.remove(project_id)
        requirement = requirements[project_id]

        state = requirement.state()
        if processed_state.get(project_id) == state:
            continue
        processed_state[project_id] = state

        (
            selected_version,
            selection_source,
            total_versions,
            compatible_versions,
            required_version_ids,
        ) = select_project_version(
            project_id=project_id,
            requirement=requirement,
            root_projects=root_projects,
            minecraft=minecraft,
            loader=loader,
            require_loader=require_loader,
            versions_cache=versions_cache,
            version_cache=version_cache,
        )
        log_selected_version(
            label=label,
            project_id=project_id,
            requirement=requirement,
            selected_version=selected_version,
            selection_source=selection_source,
            total_versions=total_versions,
            compatible_versions=compatible_versions,
            required_version_ids=required_version_ids,
        )
        selected_versions[project_id] = selected_version

        if follow_required_dependencies:
            queue_version_dependencies(
                label=label,
                project_id=project_id,
                side=requirement.side,
                selected_version=selected_version,
                requirements=requirements,
                queue=queue,
                queued=queued,
                version_cache=version_cache,
            )
        queue_manual_dependencies(
            label=label,
            project_id=project_id,
            side=requirement.side,
            requirement=requirement,
            requirements=requirements,
            queue=queue,
            queued=queued,
        )

    log_root_resolutions(entries, label=label, selected_versions=selected_versions)
    resolved = build_resolved_entries(
        label=label,
        selected_versions=selected_versions,
        requirements=requirements,
    )
    return ResolutionResult(resolved=resolved, root_projects=len(root_projects))


def main() -> int:
    args = parse_args()
    configure_logging(verbose=args.verbose)

    try:
        pack_path = Path(args.pack)
        if not pack_path.exists():
            raise FileNotFoundError(f"Pack config not found: {pack_path}")

        data = read_toml(pack_path)
        template, minecraft, loader = parse_template(data)

        mods_entries = collect_mod_entries(data)
        resource_pack_entries = collect_pack_entries(data, "resourcepacks")
        shader_pack_entries = collect_pack_entries(data, "shaderpacks")

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
                LOGGER.info("%s: validated %s", display_target("template"), args.template_out)
            else:
                write_json(Path(args.template_out), template)
                LOGGER.info("%s: wrote %s", display_target("template"), args.template_out)

        for target in ("mods", "resourcepacks", "shaderpacks"):
            if target not in targets:
                continue

            entries, lock_path = locks[target]
            result = resolve_entries(
                entries,
                minecraft,
                loader,
                target,
                require_loader=(target == "mods"),
                follow_required_dependencies=(target == "mods"),
            )
            resolved = result.resolved
            dependency_count = max(0, len(resolved) - result.root_projects)
            dependency_suffix = (
                f"; {dependency_count} dependencies added" if dependency_count else ""
            )
            if args.check:
                LOGGER.info(
                    "%s: validated %s (%d entries%s)",
                    display_target(target),
                    lock_path,
                    len(resolved),
                    dependency_suffix,
                )
            else:
                write_json(lock_path, resolved)
                LOGGER.info(
                    "%s: wrote %s (%d entries%s)",
                    display_target(target),
                    lock_path,
                    len(resolved),
                    dependency_suffix,
                )

        return 0
    except Exception as exc:
        if args.verbose:
            raise
        LOGGER.error("Error: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
