#!/usr/bin/env python3
"""Prepare local server assets and the CDN cache from the Windows DSO cache.

The official client keeps downloaded CDN content under
%LOCALAPPDATA%\\Temp\\DSOClient. This script extracts the server data files
from that cache and copies only CDN resources into this repository's DSOClient
folder. It does not download, install, or redistribute the game client.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import os
from pathlib import Path
import re
import shutil
import struct
import zlib
import xml.etree.ElementTree as ET


_NZ_FILE = re.compile(r"^(?P<key>.+)\.[0-9a-fA-F]{32}\.nz$")
_PARTIAL_FILE = re.compile(r"^(?P<key>.+)\._[0-9a-fA-F]{32}$")
_MAP_KEY = re.compile(r"^export_win32_maps_(?P<name>.+\.(?:db4|map|col))$", re.I)
_LANGUAGE_KEY = re.compile(
    r"^export_win32_language_(?P<locale>[^_]+)_(?P<file>db\..+\.xml)$", re.I
)
_HASHED_CDN_FILE = re.compile(r"\.[0-9a-fA-F]{32}\.nz$|\._[0-9a-fA-F]{32}$")
_BXML_NO_INDEX = 0x7FFFFFFF


@dataclass(frozen=True)
class CacheFile:
    key: str
    path: Path
    kind: str


def say(message: str = "") -> None:
    print(message, flush=True)


def source_directory(override: Path | None) -> Path:
    if override is not None:
        return override.expanduser().resolve()
    local_app_data = os.environ.get("LOCALAPPDATA")
    if not local_app_data:
        raise RuntimeError(
            "LOCALAPPDATA is not set. Run this script on Windows, or pass "
            "--source with the official client's DSOClient cache folder."
        )
    return Path(local_app_data) / "Temp" / "DSOClient"


def _cache_file(path: Path) -> CacheFile | None:
    name = path.name
    if name.endswith(".unc"):
        return CacheFile(name[:-4], path, "unc")
    match = _NZ_FILE.fullmatch(name)
    if match:
        return CacheFile(match.group("key"), path, "nz")
    match = _PARTIAL_FILE.fullmatch(name)
    if match:
        return CacheFile(match.group("key"), path, "partial")
    return None


def _destination_for_key(key: str, data_root: Path) -> Path | None:
    if key == "export_win32_db_static.db4":
        return data_root / "db" / "static.db4"

    map_match = _MAP_KEY.fullmatch(key)
    if map_match:
        return data_root / "maps" / map_match.group("name")

    if key == "export_win32_tables_navshapes.bin":
        return data_root / "navigation" / "navshapes.bin"

    if key == "export_win32_quests_quests.pbxml":
        return data_root / "quests.pbxml"

    language_match = _LANGUAGE_KEY.fullmatch(key)
    if language_match:
        return (
            data_root
            / "language"
            / language_match.group("locale").lower()
            / language_match.group("file")
        )

    nav_prefix = "export_win32_navigation_"
    if key.startswith(nav_prefix) and key.lower().endswith(".tok"):
        repeated_stem = key[len(nav_prefix):-4]
        for index, char in enumerate(repeated_stem):
            if char == "_" and repeated_stem[:index] == repeated_stem[index + 1:]:
                map_name = repeated_stem[:index]
                return data_root / "navigation" / map_name / f"{map_name}.tok"

    return None


def collect_assets(source: Path, data_root: Path) -> dict[Path, CacheFile]:
    # Prefer already-decompressed client files, then complete CDN payloads,
    # and use partial-cache names only when no better copy exists.
    priority = {"unc": 3, "nz": 2, "partial": 1}
    selected: dict[Path, CacheFile] = {}
    for path in source.iterdir():
        if not path.is_file():
            continue
        cached = _cache_file(path)
        if cached is None:
            continue
        destination = _destination_for_key(cached.key, data_root)
        if destination is None:
            continue
        previous = selected.get(destination)
        if previous is None or (
            priority[cached.kind], path.stat().st_mtime_ns
        ) > (
            priority[previous.kind], previous.path.stat().st_mtime_ns
        ):
            selected[destination] = cached
    return selected


def _read_cached_payload(cached: CacheFile) -> bytes:
    payload = cached.path.read_bytes()
    if cached.kind == "unc":
        return payload

    if len(payload) < 8 or payload[:4] != b"__ZN":
        raise ValueError(f"Unrecognized CDN compression header: {cached.path.name}")
    expected_size = struct.unpack_from("<I", payload, 4)[0]
    decoded = zlib.decompress(payload[8:])
    if len(decoded) != expected_size:
        raise ValueError(
            f"Decoded size mismatch for {cached.path.name}: "
            f"expected {expected_size:,} bytes, got {len(decoded):,}"
        )
    return decoded


def _write_asset(destination: Path, data: bytes) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".setup-tmp")
    try:
        temporary.write_bytes(data)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _read_cstring_table(data: bytes, offset: int, count: int) -> tuple[list[str], int]:
    strings: list[str] = []
    for index in range(count):
        end = data.find(b"\x00", offset)
        if end < 0:
            raise ValueError(f"BXML string {index} is not NUL-terminated.")
        try:
            strings.append(data[offset:end].decode("utf-8"))
        except UnicodeDecodeError as exc:
            raise ValueError(f"BXML string {index} is not valid UTF-8.") from exc
        offset = end + 1
    return strings, offset


def _decode_bxml_document(data: bytes) -> ET.Element:
    """Decode one Nebula3 BXML document stored with DSO's little-endian layout."""
    if len(data) < 16 or data[:4] != b"LMXB":
        raise ValueError("BXML document has an invalid LMXB header.")

    attribute_count, node_count, string_count = struct.unpack_from("<III", data, 4)
    if not node_count or not string_count:
        raise ValueError("BXML document has no nodes or strings.")

    offset = 16
    attribute_bytes = attribute_count * 8
    node_bytes = node_count * 24
    table_end = offset + attribute_bytes + node_bytes
    if table_end > len(data):
        raise ValueError("BXML node or attribute tables exceed the document size.")

    attributes = [
        struct.unpack_from("<II", data, offset + index * 8)
        for index in range(attribute_count)
    ]
    nodes_offset = offset + attribute_bytes
    nodes = [
        struct.unpack_from("<6I", data, nodes_offset + index * 24)
        for index in range(node_count)
    ]
    strings, _ = _read_cstring_table(data, table_end, string_count)

    elements: list[ET.Element] = []
    for node_index, node in enumerate(nodes):
        name_index, _, _, _, first_attribute, num_attributes = node
        if name_index >= len(strings) or not strings[name_index]:
            raise ValueError(f"BXML node {node_index} has an invalid name index.")
        element = ET.Element(strings[name_index])
        if num_attributes:
            if first_attribute == _BXML_NO_INDEX or first_attribute + num_attributes > len(attributes):
                raise ValueError(f"BXML node {node_index} has an invalid attribute range.")
            for attribute_index in range(first_attribute, first_attribute + num_attributes):
                key_index, value_index = attributes[attribute_index]
                if key_index >= len(strings) or value_index >= len(strings):
                    raise ValueError(f"BXML node {node_index} has an invalid attribute string index.")
                element.set(strings[key_index], strings[value_index])
        elements.append(element)

    root_index = 0
    attached: set[int] = {root_index}
    for parent_index, node in enumerate(nodes):
        child_index = node[1]
        previous_child = _BXML_NO_INDEX
        while child_index != _BXML_NO_INDEX:
            if child_index >= len(nodes) or child_index in attached:
                raise ValueError(f"BXML node {parent_index} has an invalid or cyclic child link.")
            child = nodes[child_index]
            if child[3] != parent_index:
                raise ValueError(f"BXML child {child_index} has an inconsistent parent link.")
            attached.add(child_index)
            elements[parent_index].append(elements[child_index])
            previous_child = child_index
            child_index = child[2]
        if previous_child == parent_index:
            raise ValueError(f"BXML node {parent_index} is its own child.")

    if len(attached) != len(nodes):
        raise ValueError("BXML document contains nodes that are unreachable from its root.")
    return elements[root_index]


def decode_quests_pack(data: bytes) -> tuple[ET.Element, int]:
    """Decode the KCAP quest pack and merge its one-quest BXML documents."""
    if len(data) < 8 or data[:4] != b"KCAP":
        raise ValueError("Quest pack has an invalid KCAP header.")
    version = struct.unpack_from("<I", data, 4)[0]
    if version != 1:
        raise ValueError(f"Unsupported KCAP quest pack version: {version}.")

    quests_root = ET.Element("Quests")
    offset = 8
    quest_count = 0
    while offset < len(data):
        if offset + 2 > len(data):
            raise ValueError("Quest pack ends inside an entry name length.")
        name_length = struct.unpack_from("<H", data, offset)[0]
        offset += 2
        if not name_length or offset + name_length + 4 > len(data):
            raise ValueError("Quest pack contains an invalid entry name length.")
        try:
            entry_name = data[offset:offset + name_length].decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("Quest pack contains a non-UTF-8 entry name.") from exc
        offset += name_length
        document_length = struct.unpack_from("<I", data, offset)[0]
        offset += 4
        document_end = offset + document_length
        if document_end > len(data):
            raise ValueError(f"Quest pack entry {entry_name!r} exceeds the pack size.")

        document = _decode_bxml_document(data[offset:document_end])
        if document.tag != "Quests" or len(document) != 1 or document[0].tag != "Quest":
            raise ValueError(f"Quest pack entry {entry_name!r} does not contain exactly one Quest.")
        quest = document[0]
        if quest.get("id") != entry_name:
            quest.set("packEntry", entry_name)
        quests_root.append(quest)
        quest_count += 1
        offset = document_end

    if not quest_count:
        raise ValueError("Quest pack contains no quest documents.")
    return quests_root, quest_count


def convert_quests_pbxml(source: Path, destination: Path) -> int:
    root, quest_count = decode_quests_pack(source.read_bytes())
    ET.indent(root, space="  ")
    xml_data = ET.tostring(root, encoding="utf-8", xml_declaration=True) + b"\n"
    _write_asset(destination, xml_data)
    return quest_count


def _is_cdn_resource(path: Path) -> bool:
    name = path.name
    return (
        name == "__root.txt"
        or name.startswith("__toc")
        or name.endswith(".nbi")
        or bool(_HASHED_CDN_FILE.search(name))
    )


def _cdn_source_files(source: Path) -> list[tuple[Path, Path]]:
    files: list[tuple[Path, Path]] = []
    for path in source.iterdir():
        if path.is_file() and _is_cdn_resource(path):
            files.append((path, Path(path.name)))

    bundles = source / "bundles"
    if bundles.is_dir():
        for path in bundles.rglob("*"):
            if path.is_file() and _is_cdn_resource(path):
                files.append((path, Path("bundles") / path.relative_to(bundles)))

    # These are CDN data only. Do not copy the client executable, DLLs, logs,
    # local-storage/session files, or decompressed .unc duplicates.
    event_mapping = source / "dlcache" / "eventbankmapping.txt"
    if event_mapping.is_file():
        files.append((event_mapping, Path("dlcache") / event_mapping.name))
    dlcache_assets = source / "dlcache" / "export_win32"
    if dlcache_assets.is_dir():
        for path in dlcache_assets.rglob("*"):
            if path.is_file():
                files.append((path, Path("dlcache") / "export_win32" / path.relative_to(dlcache_assets)))

    return files


def _human_size(value: int) -> str:
    size = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TiB"


def copy_cdn_cache(files: list[tuple[Path, Path]], destination: Path) -> int:
    total_bytes = sum(path.stat().st_size for path, _ in files)
    say(f"Copying {len(files):,} CDN files ({_human_size(total_bytes)}) to {destination}.")
    copied_bytes = 0
    for index, (source, relative) in enumerate(files, 1):
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        copied_bytes += source.stat().st_size
        if index == len(files) or index % 5000 == 0:
            say(
                f"  CDN cache: {index:,}/{len(files):,} files "
                f"({_human_size(copied_bytes)} copied)"
            )
    return copied_bytes


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Extract DSO server assets and prepare its local CDN cache."
    )
    parser.add_argument(
        "--source",
        type=Path,
        help="Override the official client cache folder (default: %%LOCALAPPDATA%%\\Temp\\DSOClient).",
    )
    parser.add_argument(
        "--server-dir",
        type=Path,
        default=Path(__file__).resolve().parent,
        help="Server repository directory (default: this script's directory).",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip the confirmation before copying the CDN cache.",
    )
    parser.add_argument(
        "--overwrite-quests-xml",
        action="store_true",
        help="Replace an existing data/quests.xml with the decoded client quest pack.",
    )
    args = parser.parse_args()

    server_dir = args.server_dir.expanduser().resolve()
    data_root = server_dir / "data"
    cdn_destination = server_dir / "DSOClient"
    try:
        source = source_directory(args.source)
    except RuntimeError as exc:
        say(f"ERROR: {exc}")
        return 2

    say("DSO SERVER CLIENT ASSET SETUP")
    say("This script reads the official client's local cache; it does not download game files.")
    say(f"Client cache: {source}")
    say(f"Server data:  {data_root}")
    say(f"CDN cache:    {cdn_destination}")

    root_file = source / "__root.txt"
    toc_files = list(source.glob("__toc*"))
    if not source.is_dir() or not root_file.is_file() or not toc_files:
        say()
        say("The official DSO client cache was not found or is incomplete.")
        say("Please install the official Drakensang Online client and let it finish downloading/updating completely.")
        say("Launch the official client once, wait for it to finish loading, close it, then run this script again.")
        say("No game files are downloaded by this script.")
        return 2

    assets = collect_assets(source, data_root)
    selected_destinations = set(assets)
    static_db = data_root / "db" / "static.db4"
    navshapes = data_root / "navigation" / "navshapes.bin"
    quests_pbxml = data_root / "quests.pbxml"
    map_databases = [path for path in selected_destinations if path.parent == data_root / "maps" and path.suffix.lower() == ".db4"]
    navigation_meshes = [path for path in selected_destinations if path.suffix.lower() == ".tok"]
    language_files = [path for path in selected_destinations if data_root / "language" in path.parents]
    if not static_db in selected_destinations or not map_databases or not navigation_meshes or not navshapes in selected_destinations:
        say()
        say("The client cache does not contain the minimum server assets yet.")
        say(f"Found: static database={static_db in selected_destinations}, map databases={len(map_databases)}, navigation meshes={len(navigation_meshes)}, navshapes={navshapes in selected_destinations}.")
        say("Let the official client finish downloading its content, then run this script again.")
        return 2

    if not root_file.read_text(encoding="utf-8", errors="replace").strip():
        say("ERROR: The client's __root.txt is empty; the CDN cache is incomplete.")
        return 2

    cdn_files = _cdn_source_files(source)
    if not cdn_files:
        say("ERROR: No CDN cache files were found in the official client folder.")
        return 2
    total_cdn_bytes = sum(path.stat().st_size for path, _ in cdn_files)
    quests_xml = data_root / "quests.xml"

    say()
    say("The following operations are ready:")
    say(f"  - Extract {len(assets):,} cached data files, including {len(map_databases):,} map databases, {len(navigation_meshes):,} navigation meshes, and {len(language_files):,} language files.")
    say("  - Save the client's binary quests.pbxml as data/quests.pbxml and decode its quest definitions into data/quests.xml.")
    if quests_xml.is_file() and not args.overwrite_quests_xml:
        say("    Existing data/quests.xml will be preserved; use --overwrite-quests-xml to replace it.")
    say(f"  - Copy {len(cdn_files):,} CDN resources ({_human_size(total_cdn_bytes)}) into DSOClient for the server's CDN service.")
    say("  - Exclude client executables, DLLs, logs, local-storage/session files, and decompressed cache duplicates from the CDN copy.")
    if quests_pbxml not in selected_destinations:
        say("  NOTE: No quests.pbxml was found in the client cache; quest XML conversion will be skipped.")

    if not args.yes:
        try:
            answer = input("Continue with setup? [y/N] ").strip().lower()
        except EOFError:
            answer = ""
        if answer not in ("y", "yes"):
            say("Setup cancelled. No files were changed.")
            return 1

    say()
    say("Extracting server data files...")
    for index, (destination, cached) in enumerate(sorted(assets.items(), key=lambda item: str(item[0])), 1):
        try:
            payload = _read_cached_payload(cached)
            if destination == static_db and not payload.startswith(b"SQLite format 3\x00"):
                raise ValueError("The extracted static.db4 does not have a SQLite header.")
            if destination.suffix.lower() == ".db4" and not payload.startswith(b"SQLite format 3\x00"):
                raise ValueError(f"The extracted database does not have a SQLite header: {cached.key}")
            _write_asset(destination, payload)
        except (OSError, ValueError, zlib.error) as exc:
            say(f"ERROR: Could not extract {cached.path.name}: {exc}")
            return 2
        if index == len(assets) or index % 250 == 0:
            say(f"  Data files: {index:,}/{len(assets):,}")

    decoded_quest_count: int | None = None
    if quests_pbxml.is_file():
        if quests_xml.is_file() and not args.overwrite_quests_xml:
            say(f"Preserving existing quest XML: {quests_xml}")
        else:
            say("Decoding the client's quest pack into XML...")
            try:
                decoded_quest_count = convert_quests_pbxml(quests_pbxml, quests_xml)
            except (OSError, ValueError, struct.error) as exc:
                say(f"ERROR: Could not convert quests.pbxml: {exc}")
                return 2
            say(f"  Quest XML: {decoded_quest_count:,} quests written to {quests_xml}")

    cdn_destination.mkdir(parents=True, exist_ok=True)
    copied_bytes = copy_cdn_cache(cdn_files, cdn_destination)

    say()
    say("Setup finished.")
    say(f"  Static database: {static_db}")
    say(f"  Map databases:  {len(map_databases):,}")
    say(f"  Navigation:     {len(navigation_meshes):,} .tok files and navshapes.bin")
    say(f"  Language files: {len(language_files):,}")
    say(f"  CDN resources:  {_human_size(copied_bytes)} copied to {cdn_destination}")
    if quests_pbxml in selected_destinations:
        say(f"  Quest source:   {quests_pbxml}")
    if quests_xml.is_file():
        say(f"  Quest XML:      {quests_xml}" + (f" ({decoded_quest_count:,} quests)" if decoded_quest_count is not None else " (existing file preserved)"))
    else:
        say("  Quest XML:      not present; quest features are disabled until a quest pack is available.")
    say("The extracted game assets are local-only and should not be committed to Git.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
