#!/usr/bin/env python3
"""Read and write the DDS textures the client keeps in its bundles.

Only the format the client actually uses for its UI art: a single DXT3 surface with
no mipmaps. ``loadingscreen.dds`` is 512x256, and 128 bytes of header plus
128 x 64 blocks of 16 bytes is exactly the 131,200 the bundle holds -- so matching
that layout byte for byte is a check, not a guess.

DXT3 rather than DXT1 because the art is a logo on transparency and DXT3 carries four
bits of alpha per pixel explicitly, uncompressed. DXT5 interpolates alpha and would
band the edges; ImageMagick writes DXT5 with mipmaps whatever it is asked for, which
is why this exists instead of a call to it.

A block is eight bytes of alpha -- one nibble per pixel, low nibble first, rows top to
bottom -- then a DXT1 colour block: two RGB565 endpoints and sixteen two-bit indices.
With c0 > c1 the palette is c0, c1, (2c0+c1)/3, (c0+2c1)/3, which is the four-colour
mode; this always writes that mode, so the alpha channel never steals a colour slot.
"""

from __future__ import annotations

import pathlib
import struct
import sys

import numpy as np

HEADER = 128
BLOCK = 16
FOURCC_DXT3 = b"DXT3"

DDSD_CAPS = 0x1
DDSD_HEIGHT = 0x2
DDSD_WIDTH = 0x4
DDSD_PIXELFORMAT = 0x1000
DDSD_LINEARSIZE = 0x80000
DDPF_FOURCC = 0x4
DDSCAPS_TEXTURE = 0x1000


def _to565(rgb: np.ndarray) -> np.ndarray:
    r = (rgb[..., 0].astype(np.uint16) >> 3) & 0x1F
    g = (rgb[..., 1].astype(np.uint16) >> 2) & 0x3F
    b = (rgb[..., 2].astype(np.uint16) >> 3) & 0x1F
    return (r << 11) | (g << 5) | b


def _from565(value: np.ndarray) -> np.ndarray:
    r = ((value >> 11) & 0x1F) * 255 // 31
    g = ((value >> 5) & 0x3F) * 255 // 63
    b = (value & 0x1F) * 255 // 31
    return np.stack([r, g, b], axis=-1).astype(np.int16)


def encode_dxt3(pixels: np.ndarray) -> bytes:
    """*pixels* is (height, width, 4) uint8 RGBA. Returns the surface, no header."""
    height, width, channels = pixels.shape
    if channels != 4:
        raise ValueError(f"RGBA expected, got {channels} channels")
    if width % 4 or height % 4:
        raise ValueError(f"{width}x{height} is not a multiple of four")

    # (blocks down, blocks across, 4, 4, 4): one 4x4 tile per block.
    tiles = (
        pixels.reshape(height // 4, 4, width // 4, 4, 4).transpose(0, 2, 1, 3, 4)
    )
    flat = tiles.reshape(-1, 16, 4)
    rgb = flat[..., :3].astype(np.int16)
    alpha = flat[..., 3]

    # Endpoints along the block's principal axis, not its bounding box. The bounding
    # box was the first attempt and it is visibly wrong on this art: a soft glow over
    # a wide dark area came back speckled, because the box corners are colours the
    # block does not contain and the four palette slots get spent reaching them.
    mean = rgb.mean(axis=1, keepdims=True)
    centred = rgb - mean
    # One power iteration finds the dominant direction well enough for 4x4 blocks, and
    # a block with no variation keeps a zero axis, which the flat path below handles.
    axis = centred[:, 0, :].astype(np.float64)
    for _ in range(4):
        weights = (centred * axis[:, None, :]).sum(axis=2)
        axis = (centred * weights[:, :, None]).sum(axis=1)
        norm = np.linalg.norm(axis, axis=1, keepdims=True)
        axis = np.divide(axis, norm, out=np.zeros_like(axis), where=norm > 1e-9)
    along = (centred * axis[:, None, :]).sum(axis=2)
    hi = np.clip(mean[:, 0, :] + axis * along.max(axis=1)[:, None], 0, 255)
    lo = np.clip(mean[:, 0, :] + axis * along.min(axis=1)[:, None], 0, 255)
    flat = np.linalg.norm(axis, axis=1) < 1e-9
    hi[flat] = rgb[flat, 0, :]
    lo[flat] = rgb[flat, 0, :]
    c0 = _to565(np.rint(hi).astype(np.int16))
    c1 = _to565(np.rint(lo).astype(np.int16))
    # No nudge to force c0 > c1. That test picks the palette only for DXT1; DXT2 to
    # DXT5 always decode the four-colour form, so equal endpoints are legal and are
    # exactly what a flat block wants. Forcing c0 to at least 1 was putting RGB565
    # value 1 -- a faint blue -- into every solid black block, which is where the blue
    # speckle over the transparent area came from.

    a = _from565(c0)
    b = _from565(c1)
    a = a.astype(np.int32)
    b = b.astype(np.int32)
    palette = np.stack(
        [a, b, (2 * a + b) // 3, (a + 2 * b) // 3], axis=1
    )  # (blocks, 4, 3)

    # Nearest palette entry per pixel, by squared distance -- in int32, which is not
    # a detail. Squaring a channel difference in int16 overflows at 182: 255 squared is
    # 65,025 against a ceiling of 32,767, so the distances wrapped negative and argmin
    # picked whichever entry happened to wrap furthest. That put 34 levels of average
    # error on *fully opaque* pixels, where a block is a smooth gradient and the
    # palette can reproduce it almost exactly.
    diff = (rgb[:, None, :, :].astype(np.int32) - palette[:, :, None, :].astype(np.int32))
    indices = (diff * diff).sum(axis=3).argmin(axis=1).astype(np.uint32)

    out = bytearray()
    packed_alpha = ((alpha >> 4).astype(np.uint16)).reshape(-1, 8, 2)
    alpha_bytes = (packed_alpha[..., 0] | (packed_alpha[..., 1] << 4)).astype(np.uint8)
    index_words = np.zeros(len(flat), dtype=np.uint32)
    for position in range(16):
        index_words |= indices[:, position] << (2 * position)

    for block in range(len(flat)):
        out += alpha_bytes[block].tobytes()
        out += struct.pack("<HHI", int(c0[block]), int(c1[block]), int(index_words[block]))
    return bytes(out)


def decode_dxt3(surface: bytes, width: int, height: int) -> np.ndarray:
    """The inverse, so an encode can be checked rather than trusted."""
    blocks = np.frombuffer(surface, dtype=np.uint8).reshape(-1, BLOCK)
    alpha_nibbles = blocks[:, :8]
    low = alpha_nibbles & 0x0F
    high = alpha_nibbles >> 4
    alpha = np.stack([low, high], axis=2).reshape(-1, 16) * 17

    words = blocks[:, 8:].copy().view(np.uint8).reshape(-1, 8)
    c0 = words[:, 0].astype(np.uint16) | (words[:, 1].astype(np.uint16) << 8)
    c1 = words[:, 2].astype(np.uint16) | (words[:, 3].astype(np.uint16) << 8)
    packed = (
        words[:, 4].astype(np.uint32)
        | (words[:, 5].astype(np.uint32) << 8)
        | (words[:, 6].astype(np.uint32) << 16)
        | (words[:, 7].astype(np.uint32) << 24)
    )
    a = _from565(c0)
    b = _from565(c1)
    palette = np.stack([a, b, (2 * a + b) // 3, (a + 2 * b) // 3], axis=1)
    indices = np.stack([(packed >> (2 * i)) & 3 for i in range(16)], axis=1)
    rgb = np.take_along_axis(palette, indices[:, :, None], axis=1)

    out = np.concatenate([rgb.astype(np.uint8), alpha[:, :, None].astype(np.uint8)], axis=2)
    tiles = out.reshape(height // 4, width // 4, 4, 4, 4)
    return tiles.transpose(0, 2, 1, 3, 4).reshape(height, width, 4)


def header(width: int, height: int, surface: int) -> bytes:
    out = bytearray(HEADER)
    out[0:4] = b"DDS "
    struct.pack_into("<I", out, 4, 124)
    struct.pack_into(
        "<I", out, 8,
        DDSD_CAPS | DDSD_HEIGHT | DDSD_WIDTH | DDSD_PIXELFORMAT | DDSD_LINEARSIZE,
    )
    struct.pack_into("<I", out, 12, height)
    struct.pack_into("<I", out, 16, width)
    struct.pack_into("<I", out, 20, surface)
    struct.pack_into("<I", out, 28, 1)          # one mip level: the surface itself
    struct.pack_into("<I", out, 76, 32)         # pixel format size
    struct.pack_into("<I", out, 80, DDPF_FOURCC)
    out[84:88] = FOURCC_DXT3
    struct.pack_into("<I", out, 108, DDSCAPS_TEXTURE)
    return bytes(out)


def write(path: pathlib.Path, pixels: np.ndarray) -> None:
    height, width = pixels.shape[:2]
    surface = encode_dxt3(pixels)
    path.write_bytes(header(width, height, len(surface)) + surface)


def read(path: pathlib.Path) -> np.ndarray:
    raw = path.read_bytes()
    if raw[:4] != b"DDS ":
        raise ValueError(f"{path.name} is not a DDS")
    height, width = struct.unpack_from("<2I", raw, 12)
    fourcc = raw[84:88]
    if fourcc != FOURCC_DXT3:
        raise ValueError(f"{path.name} is {fourcc!r}, not DXT3")
    return decode_dxt3(raw[HEADER:HEADER + (width // 4) * (height // 4) * BLOCK],
                       width, height)


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("usage: dds.py <in.png|in.dds> <out.dds|out.png>")
        raise SystemExit(2)
    from PIL import Image

    source, dest = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
    if source.suffix.lower() == ".dds":
        Image.fromarray(read(source), "RGBA").save(dest)
    else:
        write(dest, np.array(Image.open(source).convert("RGBA")))
    print(f"{source} -> {dest} ({dest.stat().st_size} bytes)")
