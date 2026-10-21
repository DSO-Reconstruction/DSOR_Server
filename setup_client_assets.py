#!/usr/bin/env python3
"""Prepare local server assets from the Windows DSO cache.

The official client keeps downloaded content under
%LOCALAPPDATA%\\Temp\\DSOClient. This script extracts the server data files
from that cache. It does not download, install, or redistribute the game
client.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import os
from pathlib import Path
import re
import struct
import subprocess
import sys
import zlib
import xml.etree.ElementTree as ET


_NZ_FILE = re.compile(r"^(?P<key>.+)\.[0-9a-fA-F]{32}\.nz$")
_PARTIAL_FILE = re.compile(r"^(?P<key>.+)\._[0-9a-fA-F]{32}$")
_MAP_KEY = re.compile(r"^export_win32_maps_(?P<name>.+\.(?:db4|map|col))$", re.I)
_LANGUAGE_KEY = re.compile(
    r"^export_win32_language_(?P<locale>[^_]+)_(?P<file>db\..+\.xml)$", re.I
)
_BXML_NO_INDEX = 0x7FFFFFFF
_BUNDLE_INPUT = re.compile(
    r"^bundles_(?:optional|required)_bundle\d+\.nb"
    r"(?:\._[0-9a-f]{32}|\.[0-9a-f]{32}\.nz)$",
    re.IGNORECASE,
)


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

    depth = 0
    while payload.startswith(b"__ZN"):
        if len(payload) < 8:
            raise ValueError(f"Truncated CDN compression header: {cached.path.name}")
        expected_size = struct.unpack_from("<I", payload, 4)[0]
        decoded = zlib.decompress(payload[8:])
        if len(decoded) != expected_size:
            raise ValueError(
                f"Decoded size mismatch for {cached.path.name}: "
                f"expected {expected_size:,} bytes, got {len(decoded):,}"
            )
        payload = decoded
        depth += 1
        if depth > 8:
            raise ValueError(f"Too many nested CDN compression layers: {cached.path.name}")
    if depth == 0:
        raise ValueError(f"Unrecognized CDN compression header: {cached.path.name}")
    return payload


def _minimum_assets_present(assets: dict[Path, CacheFile], data_root: Path) -> bool:
    destinations = set(assets)
    static_db = data_root / "db" / "static.db4"
    navshapes = data_root / "navigation" / "navshapes.bin"
    maps_dir = data_root / "maps"
    return (
        static_db in destinations
        and navshapes in destinations
        and any(path.parent == maps_dir and path.suffix.lower() == ".db4" for path in destinations)
        and any(path.suffix.lower() == ".tok" for path in destinations)
    )


def _client_bundles_present(source: Path) -> bool:
    return source.is_dir() and any(
        path.is_file() and _BUNDLE_INPUT.fullmatch(path.name)
        for path in source.rglob("*")
    )


def _unpack_client_bundles(source: Path) -> int:
    unpacker = Path(__file__).resolve().parent / "tools" / "unpack_dso_bundles.py"
    if not unpacker.is_file():
        say(f"ERROR: Bundle extractor not found: {unpacker}")
        return 2
    if not _client_bundles_present(source):
        say("No bundle files were found to unpack.")
        return 2
    say("Required local assets are missing; unpacking the downloaded client bundles first...")
    say(f"Bundle source and flat game cache output: {source}")
    say(f"Hierarchical client cache output: {source / 'dlcache'}")
    try:
        result = subprocess.run(
            [
                sys.executable,
                str(unpacker),
                str(source),
                "-o",
                str(source),
                "--runtime-cache",
                str(source / "dlcache"),
            ],
            check=False,
        )
    except OSError as exc:
        say(f"ERROR: Could not start the bundle extractor: {exc}")
        return 2
    return result.returncode


def _write_asset(destination: Path, data: bytes) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".setup-tmp")
    try:
        temporary.write_bytes(data)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _sync_launcher_rootkey(source: Path) -> str | None:
    """Keep the launcher's fallback rootkey aligned with the extracted client cache."""
    root_candidates = (source / "dlcache" / "__root.txt", source / "__root.txt")
    root_file = next((path for path in root_candidates if path.is_file()), None)
    if root_file is None:
        say("WARNING: No generated __root.txt found; launcher rootkey was not updated.")
        return None

    rootkey = root_file.read_text(encoding="ascii").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{32}", rootkey):
        say(f"WARNING: Invalid rootkey in {root_file}; launcher rootkey was not updated.")
        return None

    launcher_path = Path(__file__).resolve().parent / "launcher_gui.py"
    try:
        launcher_source = launcher_path.read_text(encoding="utf-8")
    except OSError as exc:
        say(f"WARNING: Could not read launcher GUI to sync rootkey: {exc}")
        return None

    pattern = re.compile(r'(?m)^(\s*rootkey\s*=\s*)"[0-9a-fA-F]{32}"(\s*)$')
    updated, replacements = pattern.subn(
        lambda match: f'{match.group(1)}"{rootkey}"{match.group(2)}',
        launcher_source,
        count=1,
    )
    if replacements != 1:
        say("WARNING: Could not find the launcher's default rootkey assignment to update.")
        return None
    if updated != launcher_source:
        temporary = launcher_path.with_name(launcher_path.name + ".setup-tmp")
        try:
            temporary.write_text(updated, encoding="utf-8", newline="")
            os.replace(temporary, launcher_path)
        finally:
            temporary.unlink(missing_ok=True)
    say(f"  Launcher rootkey: {rootkey} (from {root_file})")
    return rootkey


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


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Extract DSO server assets from the official client cache."
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
        help="Skip the confirmation before extracting the server assets.",
    )
    parser.add_argument(
        "--overwrite-quests-xml",
        action="store_true",
        help="Replace an existing data/quests.xml with the decoded client quest pack.",
    )
    args = parser.parse_args()

    server_dir = args.server_dir.expanduser().resolve()
    data_root = server_dir / "data"
    try:
        source = source_directory(args.source)
    except RuntimeError as exc:
        say(f"ERROR: {exc}")
        return 2

    say("DSO SERVER CLIENT ASSET SETUP")
    say("This script reads the official client's local cache; it does not download game files.")
    say(f"Client cache: {source}")
    say(f"Server data:  {data_root}")

    if not source.is_dir():
        say()
        say("The DSOClient folder was not found.")
        say("Install or download the official client files into the folder above, then run setup again.")
        return 2

    assets = collect_assets(source, data_root)
    runtime_cache = source / "dlcache"
    runtime_static_db = runtime_cache / "export_win32" / "db" / "static.db4"
    bundle_input_exists = _client_bundles_present(source)
    minimum_assets_present = _minimum_assets_present(assets, data_root)
    if bundle_input_exists and (
        not minimum_assets_present or not runtime_static_db.is_file()
    ):
        say()
        say("The DSOClient cache is missing required local assets; unpacking its downloaded bundles first.")
        unpack_result = _unpack_client_bundles(source)
        if unpack_result != 0:
            say("Bundle extraction failed; server asset setup was not started.")
            return unpack_result
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
        if bundle_input_exists:
            say("Bundle extraction completed, but the downloaded client files do not include all required assets.")
        else:
            say("No bundle files were available for extraction.")
        say("Finish downloading the client bundles, then run setup again.")
        return 2

    quests_xml = data_root / "quests.xml"

    say()
    say("The following operations are ready:")
    say(f"  - Extract {len(assets):,} cached data files, including {len(map_databases):,} map databases, {len(navigation_meshes):,} navigation meshes, and {len(language_files):,} language files.")
    say("  - Save the client's binary quests.pbxml as data/quests.pbxml and decode its quest definitions into data/quests.xml.")
    if quests_xml.is_file() and not args.overwrite_quests_xml:
        say("    Existing data/quests.xml will be preserved; use --overwrite-quests-xml to replace it.")
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

    say()
    say("Setup finished.")
    _sync_launcher_rootkey(source)
    say(f"  Static database: {static_db}")
    say(f"  Map databases:  {len(map_databases):,}")
    say(f"  Navigation:     {len(navigation_meshes):,} .tok files and navshapes.bin")
    say(f"  Language files: {len(language_files):,}")
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
