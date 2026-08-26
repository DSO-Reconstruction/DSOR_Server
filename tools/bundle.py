#!/usr/bin/env python3
"""Read and rewrite the client's Nebula3 asset bundles.

The client keeps 162,097 files in 623 bundles, and every one of them -- the loading
screen, each of the 175 UI layouts, every texture and model -- is reachable. The
format was recovered from the files themselves; nothing here is guessed.

A bundle::

    "_B3N" "HB3N"
    u32 count            how many files
    u32 24               where the path table starts, which is always here
    u32 table_end        where the path table ends and the entry table begins
    u32 data_at          where the blobs begin

    count x (u16 length, path)                       the path table
    count x (u32 path_offset, char[32] md5,          the entry table, 44 bytes each
             u32 size, u32 offset)
    the blobs, each "__ZN" + u32 plain size + a zlib stream

The md5 is of the *stored* bytes -- the "__ZN" header and the zlib stream together,
not the file's own content. That matters, because the client checks it:
``N3BFileUtil::ReadBundleFile(): MD5 checksum error on file data %d in '%s'``. A
replacement whose hash is not recomputed is refused at read time, loudly.

The blob for one file is not fixed length, so replacing one moves every blob behind
it. This rewrites the whole bundle rather than patching in place, and recomputes the
path table, the entry table and every offset from the file list it is given.

The bundle's own size and md5 are repeated in ``bundles/<name>.nbi``, so those are
rewritten too. The chain goes further -- a directory toc, then ``__root.txt`` -- and
that part is the patcher's, not the client's: nothing in the binary reads it at play
time. It is left alone, and said so here rather than silently.
"""

from __future__ import annotations

import hashlib
import pathlib
import struct
import sys
import zlib

MAGIC = b"_B3N" + b"HB3N"
HEADER = 24
ENTRY = 44
#: The wrapper every stored file carries: a magic, the plain size, then zlib.
BLOB_MAGIC = b"__ZN"


def unwrap(stored: bytes) -> bytes:
    """The file's own bytes, out of the ``__ZN`` wrapper."""
    if stored[:4] != BLOB_MAGIC:
        return stored
    plain = zlib.decompress(stored[8:])
    said = struct.unpack_from("<I", stored, 4)[0]
    if said != len(plain):
        raise ValueError(f"header says {said} bytes, zlib gave {len(plain)}")
    return plain


def wrap(plain: bytes, level: int = 9) -> bytes:
    """*plain* in the wrapper the bundle stores, ready to hash."""
    return BLOB_MAGIC + struct.pack("<I", len(plain)) + zlib.compress(plain, level)


def read(path: pathlib.Path) -> list[tuple[str, bytes]]:
    """Every file in the bundle at *path*, in bundle order, still wrapped.

    Wrapped rather than unwrapped, because that is what the md5 covers: handing back
    plain bytes and recompressing them would change the hash of files nobody touched,
    and zlib's output is not promised to be reproducible across versions.
    """
    raw = path.read_bytes()
    if raw[:8] != MAGIC:
        raise ValueError(f"{path.name} is not a bundle")
    count, header, table_end, data_at = struct.unpack_from("<4I", raw, 8)
    if header != HEADER:
        raise ValueError(f"{path.name}: path table starts at {header}, not {HEADER}")

    at = HEADER
    names = []
    for _ in range(count):
        length = struct.unpack_from("<H", raw, at)[0]
        at += 2
        names.append(raw[at : at + length].decode("ascii"))
        at += length
    if at != table_end:
        raise ValueError(f"{path.name}: path table ends at {at}, header says {table_end}")

    out = []
    for index, name in enumerate(names):
        base = table_end + index * ENTRY
        recorded = raw[base + 4 : base + 36].decode("ascii")
        size, offset = struct.unpack_from("<2I", raw, base + 36)
        stored = raw[data_at + offset : data_at + offset + size]
        if len(stored) != size:
            raise ValueError(f"{name}: {len(stored)} bytes of {size} present")
        digest = hashlib.md5(stored).hexdigest()
        if digest != recorded:
            raise ValueError(f"{name}: md5 {digest}, bundle says {recorded}")
        out.append((name, stored))
    return out


def write(path: pathlib.Path, files: list[tuple[str, bytes]]) -> None:
    """Write *files* -- (path, wrapped bytes) -- as a bundle at *path*."""
    table = bytearray()
    where = {}
    for name, _stored in files:
        raw = name.encode("ascii")
        where[name] = len(table)
        table += struct.pack("<H", len(raw)) + raw

    table_end = HEADER + len(table)
    data_at = table_end + ENTRY * len(files)

    entries = bytearray()
    blobs = bytearray()
    for name, stored in files:
        entries += struct.pack("<I", where[name])
        entries += hashlib.md5(stored).hexdigest().encode("ascii")
        entries += struct.pack("<2I", len(stored), len(blobs))
        blobs += stored

    path.write_bytes(
        MAGIC
        + struct.pack("<4I", len(files), HEADER, table_end, data_at)
        + bytes(table)
        + bytes(entries)
        + bytes(blobs)
    )


def replace(path: pathlib.Path, name: str, plain: bytes) -> None:
    """Put *plain* in the bundle at *path* as the file called *name*."""
    files = read(path)
    if not any(held == name for held, _ in files):
        raise KeyError(f"{name} is not in {path.name}")
    write(path, [(held, wrap(plain) if held == name else data) for held, data in files])


def index_name(bundle_file: str) -> str:
    """``bundles_required_bundle9.nb._<hash>`` -> ``bundles_required_bundle9.nbi``.

    The hash suffix goes and so does the ``.nb``: the index is named for the bundle's
    logical path, not for the file on disk.
    """
    stem = bundle_file.split("._", 1)[0]
    if stem.endswith(".nb"):
        stem = stem[: -len(".nb")]
    return stem + ".nbi"


def read_index(index: pathlib.Path) -> tuple[str, int, str]:
    """The logical path, size and hash an ``.nbi`` records.

    **Do not rewrite this.** The hash is not the md5 of the local file -- bundle9's
    file hashes to 6e7e3f96 while its index says 4501e5d2, and the size it claims is
    5,230,695 against 5,266,773 on disk. It is the content id the download came from,
    and it is what *names* the cached file: ``bundles_required_bundle9.nb._4501e5d2...``.
    Recomputing it would leave the client looking for a file that does not exist.

    Nothing local is validated against it, which is why an edited bundle works with
    its index untouched. What *is* checked is the per-file md5 inside the bundle --
    ``N3BFileUtil::ReadBundleFile(): MD5 checksum error on file data %d in '%s'`` --
    and :func:`write` recomputes every one of those.
    """
    raw = index.read_bytes()
    if raw[:4] != b"IB3N":
        raise ValueError(f"{index.name} is not a bundle index")
    at = 6
    length = struct.unpack_from("<H", raw, at)[0]
    at += 2
    logical = raw[at : at + length].decode("ascii")
    at += length
    size = struct.unpack_from("<I", raw, at)[0]
    at += 4
    digest = struct.unpack_from("<H", raw, at)[0]
    at += 2
    return logical, size, raw[at : at + digest].decode("ascii")


def index_all(client: pathlib.Path) -> dict[str, str]:
    """Every file in every bundle under *client*, to the bundle holding it."""
    found = {}
    for bundle in sorted(client.glob("*.nb._*")):
        raw = bundle.read_bytes()
        if raw[:8] != MAGIC:
            continue
        count, _header, _table_end, _data_at = struct.unpack_from("<4I", raw, 8)
        at = HEADER
        for _ in range(count):
            length = struct.unpack_from("<H", raw, at)[0]
            at += 2
            found[raw[at : at + length].decode("ascii")] = bundle.name
            at += length
    return found


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print(__doc__)
        print("usage: bundle.py <client dir> list [pattern]")
        print("       bundle.py <client dir> extract <path> [dest]")
        print("       bundle.py <client dir> replace <path> <source file>")
        return 2
    client = pathlib.Path(argv[1]).expanduser()
    what = argv[2]
    held = index_all(client)

    if what == "list":
        pattern = argv[3] if len(argv) > 3 else ""
        for name in sorted(held):
            if pattern in name:
                print(f"{held[name]}  {name}")
        return 0

    name = argv[3]
    if name not in held:
        print(f"{name} is in no bundle under {client}")
        return 1
    bundle = client / held[name]

    if what == "extract":
        for got, stored in read(bundle):
            if got == name:
                dest = pathlib.Path(argv[4]) if len(argv) > 4 else pathlib.Path(
                    name.rsplit("/", 1)[-1]
                )
                dest.write_bytes(unwrap(stored))
                print(f"{name} -> {dest} ({dest.stat().st_size} bytes)")
                return 0
    if what == "replace":
        replace(bundle, name, pathlib.Path(argv[4]).read_bytes())
        print(f"{name} <- {argv[4]}, {bundle.name} rewritten (index left alone)")
        return 0
    print(f"unknown command {what}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
