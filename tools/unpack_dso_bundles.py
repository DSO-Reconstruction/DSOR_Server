#!/usr/bin/env python3
"""Extract DSO B3N bundle resources as CDN-compatible ``.nz`` objects."""

from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import struct
import sys
import tempfile
import zlib


_CDN_MAGIC = b"__ZN"
_BUNDLE_MAGIC = b"_B3NHB3N"
_BUNDLE_INPUT = re.compile(
    r"^bundles_(?:optional|required)_bundle\d+\.nb"
    r"(?:\._[0-9a-f]{32}|\.[0-9a-f]{32}\.nz)$",
    re.IGNORECASE,
)
_CACHE_OBJECT = re.compile(r"^(?P<flat>.+)\._(?P<digest>[0-9a-f]{32})$", re.IGNORECASE)
_CACHE_NZ_OBJECT = re.compile(
    r"^(?P<flat>.+)\.(?P<digest>[0-9a-f]{32})\.nz$", re.IGNORECASE
)
_TOC_OBJECT = re.compile(
    r"^(?P<flat>.+___toc|__toc)\._(?P<digest>[0-9a-f]{32})$",
    re.IGNORECASE,
)
_TOC_NZ_OBJECT = re.compile(
    r"^(?P<flat>.+___toc|__toc)\.(?P<digest>[0-9a-f]{32})\.nz$",
    re.IGNORECASE,
)
_HEADER = struct.Struct("<8sIIII")
_RECORD = struct.Struct("<I32sII")
_CHUNK = 1024 * 1024


def _matches_prefix(path: str, prefixes: list[str] | None) -> bool:
    if not prefixes:
        return True
    normalized_path = path.strip("/").casefold()
    return any(
        normalized_path == prefix.strip("/").casefold()
        or normalized_path.startswith(prefix.strip("/").casefold() + "/")
        for prefix in prefixes
    )


def _read_exact(handle, size: int, label: str) -> bytes:
    data = handle.read(size)
    if len(data) != size:
        raise ValueError(f"Unexpected end of file while reading {label}.")
    return data


def _inflate_cdn(source: Path, target) -> None:
    with source.open("rb") as handle:
        header = _read_exact(handle, 8, "__ZN header")
        if header[:4] != _CDN_MAGIC:
            raise ValueError(f"{source.name} has an invalid __ZN header.")
        expected_size = struct.unpack_from("<I", header, 4)[0]
        decoder = zlib.decompressobj()
        decoded_size = 0
        while chunk := handle.read(_CHUNK):
            pending = chunk
            while pending:
                decoded = decoder.decompress(pending, _CHUNK)
                pending = decoder.unconsumed_tail
                if decoded:
                    target.write(decoded)
                    decoded_size += len(decoded)
                    if decoded_size > expected_size:
                        raise ValueError("CDN data exceeds its declared decoded size.")
        tail = decoder.flush()
        if tail:
            target.write(tail)
            decoded_size += len(tail)
        if not decoder.eof:
            raise ValueError("CDN zlib stream is incomplete.")
        if decoded_size != expected_size:
            raise ValueError(
                f"CDN size mismatch: expected {expected_size:,}, got {decoded_size:,}."
            )


def _seekable_bundle(source: Path) -> tuple[Path, bool]:
    with source.open("rb") as handle:
        signature = handle.read(8)
    if signature.startswith(_BUNDLE_MAGIC):
        return source, False
    if signature[:4] != _CDN_MAGIC:
        raise ValueError(f"{source.name} is not a B3N bundle or __ZN CDN object.")
    temp = tempfile.NamedTemporaryFile(prefix="dso-bundle-", suffix=".nb", delete=False)
    temp_path = Path(temp.name)
    try:
        with temp:
            _inflate_cdn(source, temp)
        with temp_path.open("rb") as handle:
            if handle.read(8) != _BUNDLE_MAGIC:
                raise ValueError(f"Decoded CDN object {source.name} is not a B3N bundle.")
        return temp_path, True
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise


def _parse_bundle(handle) -> tuple[list[tuple[str, str, int, int]], int]:
    raw_header = _read_exact(handle, _HEADER.size, "B3N header")
    magic, count, names_start, records_start, data_start = _HEADER.unpack(raw_header)
    file_size = os.fstat(handle.fileno()).st_size
    if magic != _BUNDLE_MAGIC:
        raise ValueError("Invalid B3N bundle signature.")
    if count > 2_000_000 or not (
        _HEADER.size <= names_start <= records_start <= data_start <= file_size
    ):
        raise ValueError("B3N header has invalid offsets or entry count.")
    if records_start - names_start > 256 * 1024 * 1024:
        raise ValueError("B3N filename table is implausibly large.")
    if count * _RECORD.size > data_start - records_start:
        raise ValueError("B3N record table does not fit before the payload section.")

    name_by_offset: dict[int, str] = {}
    handle.seek(names_start)
    while handle.tell() < records_start:
        name_offset = handle.tell() - names_start
        name_length = struct.unpack("<H", _read_exact(handle, 2, "filename length"))[0]
        if not name_length or handle.tell() + name_length > records_start:
            raise ValueError("B3N contains an invalid filename length.")
        try:
            name = _read_exact(handle, name_length, "filename").decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("B3N filename is not valid UTF-8.") from exc
        name_by_offset[name_offset] = name
    if handle.tell() != records_start:
        raise ValueError("B3N filename table boundary is invalid.")

    entries = []
    handle.seek(records_start)
    for index in range(count):
        name_offset, raw_digest, size, offset = _RECORD.unpack(
            _read_exact(handle, _RECORD.size, f"record {index}")
        )
        name = name_by_offset.get(name_offset)
        if name is None:
            raise ValueError(f"B3N record {index} refers to an unknown filename.")
        try:
            digest = raw_digest.decode("ascii").lower()
        except UnicodeDecodeError as exc:
            raise ValueError(f"B3N record {index} has a non-ASCII MD5.") from exc
        if not re.fullmatch(r"[0-9a-f]{32}", digest):
            raise ValueError(f"B3N record {index} has an invalid MD5.")
        if offset > file_size - data_start or size > file_size - data_start - offset:
            raise ValueError(f"B3N payload for {name!r} exceeds the bundle size.")
        entries.append((name, digest, size, offset))
    return entries, data_start


def _destination(output: Path, name: str, digest: str) -> Path:
    relative = PurePosixPath(name)
    if (
        relative.is_absolute()
        or not relative.parts
        or any(part in ("", ".", "..") for part in relative.parts)
        or any(":" in part or "\\" in part for part in relative.parts)
    ):
        raise ValueError(f"Unsafe path in B3N bundle: {name!r}.")
    # The client's download cache is flat: path separators become underscores.
    flat_name = "_".join(relative.parts) + f".{digest}.nz"
    result = output / flat_name
    if not result.resolve().is_relative_to(output.resolve()):
        raise ValueError(f"Bundle path escapes output directory: {name!r}.")
    return result


def _client_path_aliases(name: str) -> tuple[str, ...]:
    """Return client-facing aliases for bundle namespace paths."""
    if name.startswith("global/export_win32/"):
        return (name[len("global/"):],)
    return ()


def _runtime_destination(runtime_cache: Path, name: str) -> Path:
    relative = PurePosixPath(name)
    if (
        relative.is_absolute()
        or not relative.parts
        or any(part in ("", ".", "..") for part in relative.parts)
        or any(":" in part or "\\" in part for part in relative.parts)
    ):
        raise ValueError(f"Unsafe path in B3N bundle: {name!r}.")
    destination = runtime_cache.joinpath(*relative.parts)
    if not destination.resolve().is_relative_to(runtime_cache.resolve()):
        raise ValueError(f"Bundle path escapes runtime cache: {name!r}.")
    return destination


def _write_resource(
    bundle,
    data_start: int,
    output: Path,
    entry,
    runtime_cache: Path | None = None,
) -> None:
    name, expected_md5, raw_size, offset = entry
    destination = _destination(output, name, expected_md5)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".tmp")
    runtime_destination = None
    runtime_temporary = None
    runtime_out = None
    if runtime_cache is not None:
        runtime_destination = _runtime_destination(runtime_cache, name)
        runtime_destination.parent.mkdir(parents=True, exist_ok=True)
        runtime_temporary = runtime_destination.with_name(runtime_destination.name + ".tmp")
    digest = hashlib.md5()
    try:
        with temporary.open("wb") as out:
            if runtime_temporary is not None:
                runtime_out = runtime_temporary.open("wb")
            out.write(_CDN_MAGIC + struct.pack("<I", raw_size))
            bundle.seek(data_start + offset)

            first = bundle.read(min(8, raw_size))
            if len(first) != min(8, raw_size):
                raise ValueError(f"Truncated B3N payload for {name!r}.")
            digest.update(first)
            nested_cdn = first[:4] == _CDN_MAGIC
            if nested_cdn:
                if len(first) != 8:
                    raise ValueError(f"Truncated nested CDN header for {name!r}.")
                decoded_size = struct.unpack_from("<I", first, 4)[0]
                decoder = zlib.decompressobj()
                decoded_total = 0
                # B3N stores this resource as an already-encoded CDN object. Its
                # bytes are the flat .nz object; the runtime cache needs its payload.
                out.seek(0)
                out.write(first)
                remaining = raw_size - len(first)
                while remaining:
                    chunk = bundle.read(min(_CHUNK, remaining))
                    if not chunk:
                        raise ValueError(f"Truncated B3N payload for {name!r}.")
                    digest.update(chunk)
                    out.write(chunk)
                    if runtime_out is not None:
                        decoded = decoder.decompress(chunk, _CHUNK)
                        if decoded:
                            runtime_out.write(decoded)
                            decoded_total += len(decoded)
                        pending = decoder.unconsumed_tail
                        while pending:
                            part = decoder.decompress(pending, _CHUNK)
                            pending = decoder.unconsumed_tail
                            if part:
                                runtime_out.write(part)
                                decoded_total += len(part)
                    remaining -= len(chunk)
                if runtime_out is not None:
                    tail = decoder.flush()
                    if tail:
                        runtime_out.write(tail)
                        decoded_total += len(tail)
                    if not decoder.eof or decoded_total != decoded_size:
                        raise ValueError(
                            f"Nested CDN size mismatch for {name!r}: expected "
                            f"{decoded_size:,}, got {decoded_total:,}."
                        )
            else:
                # Ordinary B3N payloads are raw resources; wrap only their flat
                # CDN-cache copy and keep the hierarchical runtime copy decoded.
                out.seek(0)
                out.write(_CDN_MAGIC + struct.pack("<I", raw_size))
                compressor = zlib.compressobj()
                if runtime_out is not None:
                    runtime_out.write(first)
                encoded = compressor.compress(first)
                if encoded:
                    out.write(encoded)
                remaining = raw_size - len(first)
                while remaining:
                    chunk = bundle.read(min(_CHUNK, remaining))
                    if not chunk:
                        raise ValueError(f"Truncated B3N payload for {name!r}.")
                    digest.update(chunk)
                    if runtime_out is not None:
                        runtime_out.write(chunk)
                    encoded = compressor.compress(chunk)
                    if encoded:
                        out.write(encoded)
                    remaining -= len(chunk)
                out.write(compressor.flush())
            if runtime_out is not None:
                runtime_out.flush()
                runtime_out.close()
                runtime_out = None
        actual_md5 = digest.hexdigest()
        if actual_md5 != expected_md5:
            raise ValueError(
                f"MD5 mismatch for {name!r}: expected {expected_md5}, got {actual_md5}."
            )
        os.replace(temporary, destination)
        for alias in _client_path_aliases(name):
            alias_destination = _destination(output, alias, expected_md5)
            alias_temporary = alias_destination.with_name(alias_destination.name + ".tmp")
            try:
                with alias_temporary.open("wb") as target:
                    _inflate_cdn(destination, target)
                with alias_temporary.open("rb") as handle:
                    is_nested_object = handle.read(4) == _CDN_MAGIC
                if not is_nested_object:
                    alias_temporary.unlink(missing_ok=True)
                    with destination.open("rb") as source, alias_temporary.open("wb") as target:
                        while chunk := source.read(_CHUNK):
                            target.write(chunk)
                os.replace(alias_temporary, alias_destination)
            finally:
                alias_temporary.unlink(missing_ok=True)
        if runtime_temporary is not None and runtime_destination is not None:
            os.replace(runtime_temporary, runtime_destination)
            for alias in _client_path_aliases(name):
                alias_destination = _runtime_destination(runtime_cache, alias)
                alias_temporary = alias_destination.with_name(alias_destination.name + ".tmp")
                alias_destination.parent.mkdir(parents=True, exist_ok=True)
                try:
                    with runtime_destination.open("rb") as source, alias_temporary.open("wb") as target:
                        while chunk := source.read(_CHUNK):
                            target.write(chunk)
                    os.replace(alias_temporary, alias_destination)
                finally:
                    alias_temporary.unlink(missing_ok=True)
    finally:
        if runtime_out is not None:
            runtime_out.close()
        temporary.unlink(missing_ok=True)
        if runtime_temporary is not None:
            runtime_temporary.unlink(missing_ok=True)


def _find_bundles(source: Path, output: Path) -> list[Path]:
    if source.is_file():
        return [source]
    if not source.is_dir():
        raise ValueError(f"Input does not exist: {source}")
    input_root, output_root = source.resolve(), output.resolve()
    skip_output = output_root != input_root and output_root.is_relative_to(input_root)
    bundles = []
    for path in source.rglob("*"):
        if not path.is_file() or (skip_output and path.resolve().is_relative_to(output_root)):
            continue
        if _BUNDLE_INPUT.fullmatch(path.name):
            bundles.append(path)
    return sorted(bundles, key=lambda path: path.relative_to(source).as_posix().lower())


def _check_nbi_files(source: Path, bundles: list[Path]) -> tuple[int, int, int]:
    """Validate NBI metadata links to bundle objects present in the input tree."""
    if not source.is_dir():
        return 0, 0, 0
    available = {path.name.lower() for path in bundles}
    total = matched = invalid = 0
    for path in source.rglob("*.nbi"):
        total += 1
        try:
            data = path.read_bytes()
            if len(data) < 14 or data[:4] != b"IB3N":
                raise ValueError("invalid IB3N header")
            path_length = struct.unpack_from("<H", data, 6)[0]
            path_end = 8 + path_length
            if path_end + 6 > len(data):
                raise ValueError("truncated NBI path/size fields")
            remote_path = data[8:path_end].decode("utf-8")
            digest_length = struct.unpack_from("<H", data, path_end + 4)[0]
            digest_start = path_end + 6
            if digest_length != 32 or digest_start + digest_length != len(data):
                raise ValueError("invalid NBI MD5 field")
            digest = data[digest_start:].decode("ascii").lower()
            if not re.fullmatch(r"[0-9a-f]{32}", digest):
                raise ValueError("invalid NBI MD5")
            flat = remote_path.replace("/", "_")
            candidates = (f"{flat}._{digest}".lower(), f"{flat}.{digest}.nz".lower())
            if any(candidate in available for candidate in candidates):
                matched += 1
            else:
                print(
                    f"WARNING: NBI bundle is missing from input: {remote_path} ({digest})",
                    flush=True,
                )
        except (OSError, UnicodeDecodeError, ValueError, struct.error) as exc:
            invalid += 1
            print(f"WARNING: Invalid NBI metadata {path}: {exc}", flush=True)
    return total, matched, invalid


def _read_cdn_object(source: Path) -> bytes:
    with source.open("rb") as handle:
        header = _read_exact(handle, 8, "__ZN header")
        if header[:4] != _CDN_MAGIC:
            raise ValueError(f"{source.name} is not a __ZN CDN object.")
        raw_size = struct.unpack_from("<I", header, 4)[0]
        raw = zlib.decompress(handle.read())
    if len(raw) != raw_size:
        raise ValueError(f"{source.name} has an invalid decoded size.")
    return raw


def _toc_path(source: Path, raw: bytes) -> str:
    # The TOC lists full paths. Their common parent is the directory it indexes.
    if source.name.lower().startswith("__toc"):
        return ""
    paths = []
    for line in raw.decode("utf-8").splitlines():
        fields = line.rsplit("|", 2)
        if len(fields) == 3:
            paths.append(fields[0])
    if not paths:
        if source.name.startswith("__toc._"):
            return ""
        raise ValueError(f"Cannot determine directory for TOC {source.name}.")
    first = PurePosixPath(paths[0])
    return first.parent.as_posix() if first.parent.as_posix() != "." else ""


def _load_cache_objects(
    source: Path,
    output: Path,
    list_only: bool,
) -> tuple[dict[str, str], dict[str, bytes], dict[str, str], dict[str, Path], int]:
    """Copy cached standalone CDN objects and collect cached TOC metadata."""
    if not source.is_dir():
        return {}, {}, {}, 0
    file_digests: dict[str, str] = {}
    toc_raw: dict[str, bytes] = {}
    toc_expected: dict[str, str] = {}
    cached_by_digest: dict[str, Path] = {}
    copied = 0
    for path in source.rglob("*"):
        if not path.is_file():
            continue
        toc_match = _TOC_OBJECT.fullmatch(path.name) or _TOC_NZ_OBJECT.fullmatch(path.name)
        object_match = _CACHE_OBJECT.fullmatch(path.name) or _CACHE_NZ_OBJECT.fullmatch(path.name)
        if not object_match:
            continue
        try:
            with path.open("rb") as handle:
                magic = handle.read(4)
            if magic != _CDN_MAGIC:
                continue
            digest = object_match.group("digest").lower()
            cached_by_digest[digest] = path
            # Legacy ._<md5> sidecars hash the wrapped object. Canonical .nz
            # cache names can hash the unwrapped payload, so don't compare those
            # filename digests to the compressed bytes here.
            if _CACHE_OBJECT.fullmatch(path.name) and hashlib.md5(path.read_bytes()).hexdigest() != digest:
                print(f"WARNING: Cached object MD5 differs from filename: {path.name}", flush=True)
            flat = object_match.group("flat")
            destination = output / f"{flat}.{digest}.nz"
            if not list_only:
                if path.resolve() != destination.resolve():
                    # Keep the client's legacy cache key as well as the
                    # human-readable .nz object name. The client may look up
                    # CDN objects by the original `._<md5>` cache filename.
                    sidecar_destination = output / path.name
                    if path.resolve() != sidecar_destination.resolve():
                        sidecar_temporary = sidecar_destination.with_name(
                            sidecar_destination.name + ".tmp"
                        )
                        try:
                            with path.open("rb") as inp, sidecar_temporary.open("wb") as out:
                                while chunk := inp.read(_CHUNK):
                                    out.write(chunk)
                            os.replace(sidecar_temporary, sidecar_destination)
                        finally:
                            sidecar_temporary.unlink(missing_ok=True)
                    temporary = destination.with_name(destination.name + ".tmp")
                    try:
                        with path.open("rb") as inp, temporary.open("wb") as out:
                            while chunk := inp.read(_CHUNK):
                                out.write(chunk)
                        os.replace(temporary, destination)
                    finally:
                        temporary.unlink(missing_ok=True)
                copied += 1
            if toc_match:
                raw = _read_cdn_object(path)
                directory = _toc_path(path, raw)
                toc_raw[directory] = raw
                toc_expected[directory] = digest
                for line in raw.decode("utf-8").splitlines():
                    fields = line.rsplit("|", 2)
                    if len(fields) == 3 and fields[1] == "f":
                        file_digests[fields[0]] = fields[2].lower()
        except (OSError, ValueError, zlib.error) as exc:
            print(f"WARNING: Could not read cached object {path.name}: {exc}", flush=True)
    return file_digests, toc_raw, toc_expected, cached_by_digest, copied


def _write_cached_runtime_assets(
    runtime_cache: Path | None,
    file_digests: dict[str, str],
    cached_by_digest: dict[str, Path],
    toc_raw: dict[str, bytes],
    path_prefixes: list[str] | None = None,
) -> int:
    """Inflate standalone CDN objects and cached TOCs into the runtime tree."""
    if runtime_cache is None:
        return 0
    written = 0
    for name, expected_md5 in file_digests.items():
        if not _matches_prefix(name, path_prefixes):
            continue
        source = cached_by_digest.get(expected_md5.lower())
        if source is None:
            continue
        destination = _runtime_destination(runtime_cache, name)
        # A resource extracted directly from a B3N bundle is authoritative. Do not
        # replace it with a standalone cache object that happens to share a key.
        if destination.is_file():
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(destination.name + ".tmp")
        digest = hashlib.md5()
        try:
            with temporary.open("wb") as out:
                class DigestWriter:
                    def write(self, data: bytes) -> int:
                        digest.update(data)
                        return out.write(data)

                _inflate_cdn(source, DigestWriter())
            actual = digest.hexdigest()
            if actual != expected_md5.lower():
                print(
                    f"WARNING: Skipping cached object for {name!r}: "
                    f"expected MD5 {expected_md5}, got {actual}.",
                    flush=True,
                )
                continue
            os.replace(temporary, destination)
            written += 1
        finally:
            temporary.unlink(missing_ok=True)
    for directory, raw in toc_raw.items():
        destination = _runtime_destination(runtime_cache, f"{directory}/__toc" if directory else "__toc")
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(destination.name + ".tmp")
        try:
            temporary.write_bytes(raw)
            os.replace(temporary, destination)
            written += 1
        finally:
            temporary.unlink(missing_ok=True)
    return written


def _make_toc_object(raw: bytes) -> tuple[str, bytes]:
    # CDN TOC names hash the full __ZN wrapper.
    obj = _CDN_MAGIC + struct.pack("<I", len(raw)) + zlib.compress(raw, 9)
    return hashlib.md5(obj).hexdigest(), obj


def _read_nbi_entries(source: Path) -> dict[str, str]:
    """Return CDN paths and digests named by valid NBI bundle metadata."""
    entries: dict[str, str] = {}
    if not source.is_dir():
        return entries
    for path in source.rglob("*.nbi"):
        try:
            data = path.read_bytes()
            if len(data) < 14 or data[:4] != b"IB3N":
                continue
            path_length = struct.unpack_from("<H", data, 6)[0]
            path_end = 8 + path_length
            if path_end + 6 > len(data):
                continue
            remote_path = data[8:path_end].decode("utf-8")
            digest_length = struct.unpack_from("<H", data, path_end + 4)[0]
            digest_start = path_end + 6
            if digest_length != 32 or digest_start + digest_length != len(data):
                continue
            digest = data[digest_start:].decode("ascii").lower()
            if re.fullmatch(r"[0-9a-f]{32}", digest):
                entries[remote_path] = digest
        except (OSError, UnicodeDecodeError, ValueError, struct.error):
            continue
    return entries


def _write_toc_cache_object(output: Path, flat: str, digest: str, obj: bytes) -> None:
    """Write both the CDN URL object and its legacy client cache key."""
    for destination in (output / f"{flat}.{digest}.nz", output / f"{flat}._{digest}"):
        temporary = destination.with_name(destination.name + ".tmp")
        try:
            with temporary.open("wb") as handle:
                handle.write(obj)
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)


def _rebuild_root_index(
    output: Path,
    source: Path,
    file_digests: dict[str, str],
    toc_raw: dict[str, bytes],
    cached_by_digest: dict[str, Path],
    list_only: bool,
) -> tuple[int, str]:
    """Build a self-consistent local TOC tree and point __root.txt at it.

    Missing per-directory TOCs cannot keep their CDN hashes: the hash covers
    the compressed __ZN bytes, and a clean bundle-only cache may not contain
    those original bytes. Rebuilding the tree gives every local TOC its own
    valid hash and writes a new root hash that the launcher can load locally.
    """
    remote_files = dict(file_digests)
    remote_files.update(_read_nbi_entries(source))
    for name, digest in tuple(remote_files.items()):
        for alias in _client_path_aliases(name):
            remote_files[alias] = digest
    local_bundle_keys = {
        path.name.lower()
        for path in source.rglob("*")
        if path.is_file() and _BUNDLE_INPUT.fullmatch(path.name)
    } if source.is_dir() else set()

    # Keep only files available from extracted bundle resources, local CDN
    # objects, or bundle/NBI cache pairs. This prevents the rebuilt index from
    # advertising assets that would force an HTTP fallback.
    available: dict[str, str] = {}
    for name, digest in remote_files.items():
        flat = "_".join(PurePosixPath(name).parts)
        if (output / f"{flat}.{digest}.nz").is_file():
            available[name] = digest
            continue
        if digest in cached_by_digest:
            available[name] = digest
            continue
        bundle_key = f"{flat}._{digest}"
        if bundle_key.lower() in local_bundle_keys or (output / bundle_key).is_file():
            available[name] = digest

    directories: set[str] = set()
    if "" in toc_raw:
        for line in toc_raw[""].decode("utf-8").splitlines():
            fields = line.rsplit("|", 2)
            if len(fields) == 3 and fields[1] == "d":
                directories.add(fields[0])
    for name in available:
        parts = PurePosixPath(name).parts
        for index in range(1, len(parts)):
            directories.add("/".join(parts[:index]))

    files_by_directory: dict[str, list[tuple[str, str]]] = {}
    for name, digest in available.items():
        parent = PurePosixPath(name).parent.as_posix()
        if parent == ".":
            parent = ""
        files_by_directory.setdefault(parent, []).append((name, digest))
    child_directories: dict[str, set[str]] = {}
    for directory in directories:
        parent = PurePosixPath(directory).parent.as_posix()
        if parent == ".":
            parent = ""
        if parent:
            child_directories.setdefault(parent, set()).add(directory)

    toc_hashes: dict[str, str] = {}
    generated_tocs: dict[str, bytes] = {}
    written = 0
    for directory in sorted(directories, key=lambda value: (value.count("/"), value), reverse=True):
        direct_files = sorted(files_by_directory.get(directory, []), key=lambda item: item[0])
        child_dirs = sorted(child_directories.get(directory, set()))
        lines = [f"{name}|f|{digest}" for name, digest in direct_files]
        lines.extend(f"{child}|d|{toc_hashes[child]}" for child in child_dirs)
        raw = ("\n".join(lines) + "\n").encode("utf-8") if lines else b""
        digest, obj = _make_toc_object(raw)
        toc_hashes[directory] = digest
        generated_tocs[directory] = raw
        flat = directory.replace("/", "_") + "___toc"
        if not list_only:
            _write_toc_cache_object(output, flat, digest, obj)
        written += 1

    root_lines = [f"{directory}|d|{toc_hashes[directory]}" for directory in sorted(directories)]
    root_raw = ("\n".join(root_lines) + "\n").encode("utf-8")
    root_digest, root_obj = _make_toc_object(root_raw)
    if not list_only:
        _write_toc_cache_object(output, "__toc", root_digest, root_obj)
        root_text = root_digest + "\n"
        root_targets = [output / "__root.txt"]
        dlcache = output / "dlcache"
        if dlcache.is_dir():
            root_targets.append(dlcache / "__root.txt")
        for target in root_targets:
            target.write_text(root_text, encoding="ascii")
        runtime_cache = output / "dlcache"
        if runtime_cache.is_dir():
            generated_tocs[""] = root_raw
            for directory, raw in generated_tocs.items():
                destination = _runtime_destination(
                    runtime_cache,
                    f"{directory}/__toc" if directory else "__toc",
                )
                destination.parent.mkdir(parents=True, exist_ok=True)
                temporary = destination.with_name(destination.name + ".tmp")
                try:
                    temporary.write_bytes(raw)
                    os.replace(temporary, destination)
                finally:
                    temporary.unlink(missing_ok=True)
    return written + 1, root_digest


def _generate_missing_tocs(
    output: Path,
    file_digests: dict[str, str],
    toc_raw: dict[str, bytes],
    toc_expected: dict[str, str],
    list_only: bool,
    runtime_cache: Path | None = None,
    path_prefixes: list[str] | None = None,
) -> tuple[int, int]:
    """Rebuild referenced but uncached directory TOCs bottom-up."""
    expected_children: dict[str, str] = {}
    for raw in toc_raw.values():
        for line in raw.decode("utf-8").splitlines():
            fields = line.rsplit("|", 2)
            if len(fields) == 3 and fields[1] == "d":
                expected_children[fields[0]] = fields[2].lower()

    generated: dict[str, tuple[str, bytes, bytes]] = {}
    visiting: set[str] = set()

    def build(directory: str) -> tuple[str, bytes, bytes]:
        if directory in generated:
            return generated[directory]
        if directory in toc_raw:
            digest, obj = _make_toc_object(toc_raw[directory])
            return digest, obj, toc_raw[directory]
        if directory in visiting:
            raise ValueError(f"Cycle while building TOC for {directory!r}.")
        visiting.add(directory)
        prefix = directory + "/" if directory else ""
        direct_files = []
        child_dirs: set[str] = set()
        for file_path, digest in file_digests.items():
            if not file_path.startswith(prefix):
                continue
            rest = file_path[len(prefix):]
            if not rest:
                continue
            if "/" in rest:
                child_dirs.add(rest.split("/", 1)[0])
            else:
                direct_files.append((rest, file_path, digest))
        if not direct_files and not child_dirs:
            visiting.remove(directory)
            raise ValueError(f"No source entries are available for TOC {directory!r}.")

        lines = [f"{path}|f|{digest}" for _, path, digest in sorted(direct_files, key=lambda x: (x[0].casefold(), x[0]))]
        subdirs = set(child_dirs)
        # Parent TOCs can reference a directory even if its resources are absent from the bundles.
        for child_path in expected_children:
            if child_path.startswith(prefix):
                rest = child_path[len(prefix):]
                if rest and "/" not in rest:
                    subdirs.add(rest)
        for child in sorted(subdirs, key=lambda x: (x.casefold(), x)):
            child_path = f"{directory}/{child}" if directory else child
            child_digest, _, _ = build(child_path)
            lines.append(f"{child_path}|d|{child_digest}")
        raw = ("\n".join(lines) + "\n").encode("utf-8")
        digest, obj = _make_toc_object(raw)
        generated[directory] = (digest, obj, raw)
        visiting.remove(directory)
        expected = expected_children.get(directory)
        if expected and expected != digest:
            print(
                f"WARNING: Rebuilt TOC hash for {directory} is {digest}, "
                f"but parent expects {expected}.",
                flush=True,
            )
        return digest, obj, raw

    missing = [
        directory for directory in expected_children
        if directory not in toc_raw
        and _matches_prefix(directory, path_prefixes)
    ]
    for directory in sorted(missing, key=lambda value: (value.count("/"), value.casefold())):
        try:
            build(directory)
        except ValueError as exc:
            print(f"WARNING: {exc}", flush=True)

    # Point each local parent TOC at the rebuilt child. This keeps the client's
    # hierarchical dlcache self-contained even when a CDN TOC was absent.
    for directory, (digest, _obj, _raw) in list(generated.items()):
        parent = PurePosixPath(directory).parent.as_posix()
        if parent == ".":
            parent = ""
        parent_raw = toc_raw.get(parent)
        if parent_raw is None:
            continue
        updated_lines = []
        changed = False
        for line in parent_raw.decode("utf-8").splitlines():
            fields = line.rsplit("|", 2)
            if len(fields) == 3 and fields[0] == directory and fields[1] == "d":
                line = f"{fields[0]}|d|{digest}"
                changed = True
            updated_lines.append(line)
        if changed:
            updated_raw = ("\n".join(updated_lines) + "\n").encode("utf-8")
            toc_raw[parent] = updated_raw
            if runtime_cache is not None and not list_only:
                parent_path = _runtime_destination(
                    runtime_cache, f"{parent}/__toc" if parent else "__toc"
                )
                parent_path.parent.mkdir(parents=True, exist_ok=True)
                temporary = parent_path.with_name(parent_path.name + ".tmp")
                try:
                    temporary.write_bytes(updated_raw)
                    os.replace(temporary, parent_path)
                finally:
                    temporary.unlink(missing_ok=True)

    written = mismatched = 0
    for directory, (digest, obj, raw) in generated.items():
        expected = expected_children.get(directory)
        if expected and expected != digest:
            mismatched += 1
        flat = directory.replace("/", "_")
        flat = f"{flat}___toc" if flat else "__toc"
        destination = output / f"{flat}.{digest}.nz"
        if not list_only:
            temporary = destination.with_name(destination.name + ".tmp")
            try:
                temporary.write_bytes(obj)
                os.replace(temporary, destination)
            finally:
                temporary.unlink(missing_ok=True)
            if runtime_cache is not None:
                runtime_destination = _runtime_destination(
                    runtime_cache, f"{directory}/__toc" if directory else "__toc"
                )
                runtime_destination.parent.mkdir(parents=True, exist_ok=True)
                runtime_temporary = runtime_destination.with_name(
                    runtime_destination.name + ".tmp"
                )
                try:
                    runtime_temporary.write_bytes(raw)
                    os.replace(runtime_temporary, runtime_destination)
                finally:
                    runtime_temporary.unlink(missing_ok=True)
        written += 1
    return written, mismatched


def _process_bundle(
    source: Path,
    output: Path,
    list_only: bool,
    file_digests: dict[str, str],
    runtime_cache: Path | None,
    path_prefixes: list[str] | None,
) -> tuple[int, int]:
    bundle_path, is_temporary = _seekable_bundle(source)
    try:
        with bundle_path.open("rb") as bundle:
            entries, data_start = _parse_bundle(bundle)
            print(f"{source.name}: {len(entries):,} source files", flush=True)
            total_bytes = 0
            selected_count = 0
            for index, entry in enumerate(entries, 1):
                name, digest, size, offset = entry
                if not _matches_prefix(name, path_prefixes):
                    continue
                selected_count += 1
                file_digests[name] = digest
                for alias in _client_path_aliases(name):
                    file_digests[alias] = digest
                if list_only:
                    print(f"{size:>12,}  {digest}  {name}")
                else:
                    _write_resource(bundle, data_start, output, entry, runtime_cache)
                total_bytes += size
                if not list_only and (selected_count % 1000 == 0 or index == len(entries)):
                    print(f"  Wrote {selected_count:,} matching .nz files", flush=True)
            return selected_count, total_bytes
    finally:
        if is_temporary:
            bundle_path.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="A bundle file or folder to scan recursively")
    parser.add_argument("-o", "--output", type=Path, default=Path("unpacked_nz"), help="Flat output directory (DSO cache layout)")
    parser.add_argument("--runtime-cache", type=Path, help="Also write decoded resources into the game's hierarchical dlcache directory")
    parser.add_argument("--path-prefix", action="append", dest="path_prefixes", help="Only write resources below this bundle path prefix; repeat to include more paths")
    parser.add_argument("--list", action="store_true", help="List resources without writing .nz files")
    args = parser.parse_args()
    try:
        source = args.input.expanduser().resolve()
        output = args.output.expanduser().resolve()
        runtime_cache = args.runtime_cache.expanduser().resolve() if args.runtime_cache else None
        bundles = _find_bundles(source, output)
        if not bundles:
            raise ValueError("No .nb or recognized DSO bundle cache files were found.")
        nbi_total, nbi_matched, nbi_invalid = _check_nbi_files(source, bundles)
        if nbi_total:
            print(
                f"NBI check: {nbi_matched:,}/{nbi_total:,} bundle references matched; "
                f"{nbi_invalid:,} invalid metadata files",
                flush=True,
            )
        if not args.list:
            output.mkdir(parents=True, exist_ok=True)
        file_digests, toc_raw, toc_expected, cached_by_digest, cached_objects = _load_cache_objects(
            source, output, args.list
        )
        if cached_objects:
            print(f"Copied {cached_objects:,} standalone cached CDN objects", flush=True)
        total_bundles = total_files = total_bytes = 0
        for bundle in bundles:
            files, size = _process_bundle(
                bundle, output, args.list, file_digests, runtime_cache, args.path_prefixes
            )
            total_bundles += 1
            total_files += files
            total_bytes += size
        runtime_count = 0
        if not args.list and runtime_cache is not None:
            runtime_cache.mkdir(parents=True, exist_ok=True)
            runtime_count = _write_cached_runtime_assets(
                runtime_cache, file_digests, cached_by_digest, toc_raw,
                args.path_prefixes,
            )
        if source.is_dir() and not args.path_prefixes:
            toc_count, root_digest = _rebuild_root_index(
                output, source, file_digests, toc_raw, cached_by_digest, args.list
            )
            toc_mismatches = 0
            if not args.list:
                print(f"Rebuilt local TOC tree; __root.txt -> {root_digest}", flush=True)
        else:
            toc_count, toc_mismatches = _generate_missing_tocs(
                output, file_digests, toc_raw, toc_expected, args.list, runtime_cache,
                args.path_prefixes,
            )
        if not args.list:
            print(
                f"Done: {total_bundles:,} bundles, {total_files:,} source files as .nz, "
                f"{total_bytes:,} uncompressed bytes; {runtime_count:,} runtime files; "
                f"{toc_count:,} generated TOCs "
                f"({toc_mismatches:,} hash mismatches) -> {output}",
                flush=True,
            )
        return 0
    except (OSError, ValueError, zlib.error) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
