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


def _minimum_asset_status(
    assets: dict[Path, CacheFile], data_root: Path
) -> tuple[bool, list[Path], list[Path], bool]:
    destinations = set(assets)
    static_db = data_root / "db" / "static.db4"
    navshapes = data_root / "navigation" / "navshapes.bin"
    maps_dir = data_root / "maps"
    map_databases = [path for path in destinations if path.parent == maps_dir and path.suffix.lower() == ".db4"]
    navigation_meshes = [path for path in destinations if path.suffix.lower() == ".tok"]
    return static_db in destinations, map_databases, navigation_meshes, navshapes in destinations


def _client_bundles_present(source: Path, progress_callback=None) -> bool:
    if not source.is_dir():
        return False
    walked_directories = 0
    for current, directories, names in os.walk(source):
        walked_directories += 1
        directories[:] = [name for name in directories if name.lower() != "dlcache"]
        if any(name.startswith("bundles_") and "_bundle" in name and ".nb" in name for name in names):
            return True
        if progress_callback is not None and walked_directories % 100 == 0:
            progress_callback(walked_directories)
    if progress_callback is not None:
        progress_callback(walked_directories)
    return False


def _unpack_client_bundles(source: Path, output: Path) -> int:
    """Unpack the client's bundles into ``output`` (flat cache + dlcache).

    By default ``output`` is the server's data/client_unpacked: written into
    the client folder itself, the unpacked cache grew it from 2.2 GB to 9.6 GB
    and replaced the launcher's own dlcache/__root.txt and TOC files with
    self-generated ones. ``--client-cache`` passes the client folder, for the
    official client to read the files locally without a CDN.
    """
    unpacker = Path(__file__).resolve().parent / "tools" / "unpack_dso_bundles.py"
    frozen = getattr(sys, "frozen", False)
    if not frozen and not unpacker.is_file():
        say(f"ERROR: Bundle extractor not found: {unpacker}")
        return 2
    say("Required local assets are missing; unpacking the downloaded client bundles first...")
    say(f"Bundle source (read only): {source}")
    say(f"Unpacked cache: {output}")
    arguments = [
        str(source), "-o", str(output), "--runtime-cache", str(output / "dlcache"),
    ]
    if frozen:
        say("PROGRESS: 8")
        from contextlib import redirect_stdout
        from tools.unpack_dso_bundles import main as unpack_bundles

        class BundleProgressOutput:
            def __init__(self):
                self.pending = ""
                self.output = sys.stdout

            def write(self, chunk):
                self.pending += chunk
                lines = self.pending.split("\n")
                self.pending = lines.pop()
                for output_line in lines:
                    self._write_line(output_line)
                return len(chunk)

            def flush(self):
                if self.pending:
                    self._write_line(self.pending)
                    self.pending = ""

            def _write_line(self, output_line):
                if output_line.startswith("PROGRESS:"):
                    try:
                        percent = int(output_line.partition(":")[2].strip())
                    except ValueError:
                        return
                    print(f"PROGRESS: {5 + percent * 75 // 100}", file=self.output, flush=True)
                else:
                    print(output_line, file=self.output, flush=True)

        original_argv = sys.argv
        try:
            sys.argv = [str(unpacker), *arguments, "--progress"]
            with redirect_stdout(BundleProgressOutput()):
                return unpack_bundles()
        finally:
            sys.argv = original_argv
    try:
        say("PROGRESS: 8")
        process = subprocess.Popen(
            [
                sys.executable,
                str(unpacker),
                *arguments,
                "--progress",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        assert process.stdout is not None
        for line in process.stdout:
            if line.startswith("PROGRESS:"):
                try:
                    bundle_percent = int(line.partition(":")[2].strip())
                except ValueError:
                    continue
                print(f"PROGRESS: {5 + bundle_percent * 75 // 100}", flush=True)
            else:
                print(line, end="", flush=True)
        return process.wait()
    except OSError as exc:
        say(f"ERROR: Could not start the bundle extractor: {exc}")
        return 2


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


def verify_complete_client_download(source: Path) -> dict[str, int]:
    """Check NBI completeness before setup; bundle parsing happens during unpack."""
    from tools.unpack_dso_bundles import (
        _check_nbi_files,
        _find_bundles,
        _scan_source_files,
    )

    source = source.expanduser().resolve()
    if not source.is_dir():
        raise ValueError(f"Client folder does not exist: {source}")

    source_files = _scan_source_files(source, source, source / "dlcache")
    bundles = _find_bundles(source, source_files)
    if not bundles:
        raise ValueError("No complete client bundle files were found.")

    nbi_total, nbi_matched, nbi_invalid, _entries = _check_nbi_files(
        source_files, bundles,
    )
    if not nbi_total:
        raise ValueError(
            "The client cache has no NBI download metadata, so download completeness cannot be confirmed."
        )
    if nbi_invalid:
        raise ValueError(f"The client cache contains {nbi_invalid} invalid NBI metadata files.")
    if nbi_matched != nbi_total:
        raise ValueError(
            f"Client download is incomplete: {nbi_matched}/{nbi_total} bundle references are present."
        )

    return {"bundles": len(bundles), "nbi_total": nbi_total}


def _remove_stale_client_assets(
    data_root: Path, selected_destinations: set[Path],
) -> int:
    """Remove obsolete extracted assets only from client-owned data folders."""
    managed_roots = (
        data_root / "maps",
        data_root / "navigation",
        data_root / "language",
    )
    keep = {path.resolve() for path in selected_destinations}
    removed = 0
    for root in managed_roots:
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*"), reverse=True):
            if path.is_file() and path.name.lower() != "readme.txt" and path.resolve() not in keep:
                path.unlink()
                removed += 1
            elif path.is_dir():
                try:
                    path.rmdir()
                except OSError:
                    pass
    return removed


def main(argv: list[str] | None = None) -> int:
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
        "--client-cache",
        action="store_true",
        help=("Unpack the bundles into the client folder itself (its flat cache and "
              "dlcache), so the official client reads them locally without a CDN. "
              "Grows it by ~7 GB and replaces dlcache/__root.txt (a copy is kept as "
              "__root.txt.before-unpack). Default: data/client_unpacked, client untouched."),
    )
    parser.add_argument(
        "--unpack-client",
        action="store_true",
        help=("Force unpacking all client bundles directly into the official client's "
              "dlcache folder (%%LOCALAPPDATA%%\\Temp\\DSOClient\\dlcache), ensuring all 3D "
              "models, physics meshes, and textures are available offline even if server assets "
              "are already present. Implies --client-cache and --force-unpack."),
    )
    parser.add_argument(
        "--force-unpack",
        action="store_true",
        help="Force unpacking bundles even if minimum server assets are already present.",
    )
    parser.add_argument(
        "--overwrite-quests-xml",
        action="store_true",
        help="Replace an existing data/quests.xml with the decoded client quest pack.",
    )
    parser.add_argument(
        "--replace-assets",
        action="store_true",
        help=("Remove obsolete maps, navigation, and language assets that are not "
              "present in the selected client cache after successful extraction."),
    )
    args = parser.parse_args(argv)

    if args.unpack_client:
        args.client_cache = True
        args.force_unpack = True

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

    say("PROGRESS: 1")
    say("Scanning local client assets...")
    assets = collect_assets(source, data_root)
    say("PROGRESS: 2")
    # Bundles unpack beside the server's data (git-ignored), so the official
    # client's folder is only ever read -- unless --client-cache asks for the
    # client's own local cache (the official client then needs no CDN).
    unpacked_cache = source if args.client_cache else data_root / "client_unpacked"
    say("SCANNING: bundle cache")
    bundle_input_exists = _client_bundles_present(
        source,
        progress_callback=lambda count: say(f"SCANNING: bundle folders {count}"),
    )
    say("PROGRESS: 4")
    static_db = data_root / "db" / "static.db4"
    navshapes = data_root / "navigation" / "navshapes.bin"
    static_present, map_databases, navigation_meshes, navshapes_present = _minimum_asset_status(assets, data_root)
    minimum_assets_present = bool(
        static_present and map_databases and navigation_meshes and navshapes_present
    )
    unpack_bundles = bundle_input_exists and (not minimum_assets_present or args.force_unpack)

    if not minimum_assets_present and not bundle_input_exists:
        say()
        say("The client cache does not contain the minimum server assets yet.")
        say(f"Found: static database={static_present}, map databases={len(map_databases)}, navigation meshes={len(navigation_meshes)}, navshapes={navshapes_present}.")
        say("No bundle files were available for extraction.")
        say("Finish downloading the client bundles, then run setup again.")
        return 2

    say()
    say("The following operations are ready:")
    if unpack_bundles:
        if args.client_cache:
            say(f"  - Unpack the downloaded bundles INTO THE CLIENT FOLDER {source} and its dlcache (~7 GB more;")
            say("    dlcache/__root.txt is replaced, a copy is kept as __root.txt.before-unpack).")
        else:
            say(f"  - Unpack the downloaded bundles into {unpacked_cache} (the client folder is not changed; ~7 GB).")
        say(f"  - Re-scan that cache, then extract its server assets into {data_root}.")
    else:
        say(f"  - Extract {len(assets):,} cached data files into {data_root}.")
    quests_xml = data_root / "quests.xml"
    if quests_xml.is_file() and not args.overwrite_quests_xml:
        say("  - Preserve the existing data/quests.xml file.")
    else:
        say("  - Decode data/quests.xml from the client's quest pack if one is available.")

    if not args.yes:
        try:
            answer = input("Continue with setup? [y/N] ").strip().lower()
        except EOFError:
            answer = ""
        if answer not in ("y", "yes"):
            say("Setup cancelled. No files were changed.")
            return 1

    if unpack_bundles:
        say()
        say("Unpacking downloaded client bundles...")
        say("PROGRESS: 5")
        if args.client_cache:
            say("PROGRESS: 6")
            for root_file in (source / "__root.txt", source / "dlcache" / "__root.txt"):
                backup = root_file.with_name("__root.txt.before-unpack")
                if root_file.is_file() and not backup.exists():
                    backup.write_bytes(root_file.read_bytes())
            say("PROGRESS: 7")
        unpack_result = _unpack_client_bundles(source, unpacked_cache)
        if unpack_result != 0:
            say(f"Bundle extraction failed; {unpacked_cache} may be incomplete. Server asset extraction was not started.")
            return unpack_result
        say("PROGRESS: 80")
        assets = collect_assets(unpacked_cache, data_root)

    selected_destinations = set(assets)
    quests_pbxml = data_root / "quests.pbxml"
    static_present, map_databases, navigation_meshes, navshapes_present = _minimum_asset_status(assets, data_root)
    language_files = [path for path in selected_destinations if data_root / "language" in path.parents]
    if not (static_present and map_databases and navigation_meshes and navshapes_present):
        say()
        say("The client cache does not contain the minimum server assets yet.")
        say(f"Found: static database={static_present}, map databases={len(map_databases)}, navigation meshes={len(navigation_meshes)}, navshapes={navshapes_present}.")
        if bundle_input_exists:
            say("Bundle extraction completed, but the downloaded client files do not include all required assets.")
        else:
            say("No bundle files were available for extraction.")
        say("Finish downloading the client bundles, then run setup again.")
        return 2

    say()
    say(f"  - Extract {len(assets):,} cached data files, including {len(map_databases):,} map databases, {len(navigation_meshes):,} navigation meshes, and {len(language_files):,} language files.")
    if quests_pbxml not in selected_destinations:
        say("  NOTE: No quests.pbxml was found in the client cache; quest XML conversion will be skipped.")

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
        progress = 80 + index * 19 // max(1, len(assets))
        say(f"PROGRESS: {progress}")

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

    if args.replace_assets:
        removed = _remove_stale_client_assets(data_root, selected_destinations)
        say(f"Removed {removed:,} obsolete client asset files.")

    say()
    say("Setup finished.")
    say("PROGRESS: 100")
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
