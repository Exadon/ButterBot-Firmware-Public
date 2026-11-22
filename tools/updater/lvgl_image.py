"""
Images in the format the Clockstar watch reads (LVGL 8 .bin): a 4-byte header, then for indexed images a palette
(B, G, R, A per entry) and the pixels packed MSB first, each row byte aligned.

encode(img)       an RGBA image -> indexed 4-bit (16 colours, one of them transparent if the image has any)
from_file(path)   any image file -> encode(), resized to `size` if given
"""
from __future__ import annotations

import struct
from pathlib import Path

from PIL import Image

CF_INDEXED = {1: 7, 2: 8, 4: 9, 8: 10}


def pack(img: Image.Image, bits: int, palette: list[tuple[int, int, int, int]], index_of) -> bytes:
    w, h = img.size
    out = bytearray(struct.pack("<I", CF_INDEXED[bits] | (w << 10) | (h << 21)))
    for (r, g, b, a) in palette + [(0, 0, 0, 0)] * ((1 << bits) - len(palette)):
        out += bytes((b, g, r, a))
    px = img.load()
    for y in range(h):
        acc = nbits = 0
        row = bytearray()
        for x in range(w):
            acc = (acc << bits) | index_of(px[x, y])
            nbits += bits
            if nbits == 8:
                row.append(acc)
                acc = nbits = 0
        if nbits:
            row.append(acc << (8 - nbits))
        out += row
    return bytes(out)


def palette_of(images: list[Image.Image], colors: int = 16):
    """One palette for a set of images (so animation frames match). Returns (palette, index_of)."""
    images = [i.convert("RGBA") for i in images]
    transparent = any(i.getextrema()[3][0] < 128 for i in images)
    n = colors - 1 if transparent else colors
    strip = Image.new("RGB", (sum(i.width for i in images), max(i.height for i in images)))
    x = 0
    for im in images:
        bg = Image.new("RGB", im.size, (0, 0, 0))
        bg.paste(im, mask=im.split()[3])
        strip.paste(bg, (x, 0))
        x += im.width
    q = strip.quantize(colors=n, method=Image.Quantize.MEDIANCUT)
    raw = q.getpalette()[:n * 3]
    cols = [(raw[k], raw[k + 1], raw[k + 2], 255) for k in range(0, len(raw), 3)]
    palette = ([(0, 0, 0, 0)] if transparent else []) + cols
    first = 1 if transparent else 0
    memo: dict[tuple, int] = {}

    def index_of(p):
        if transparent and p[3] < 128:
            return 0
        key = p[:3]
        if key not in memo:
            r, g, b = key
            memo[key] = min(range(first, len(palette)),
                            key=lambda k: (palette[k][0] - r) ** 2 + (palette[k][1] - g) ** 2 + (palette[k][2] - b) ** 2)
        return memo[key]
    return palette, index_of


def encode(img: Image.Image, bits: int = 4) -> bytes:
    img = img.convert("RGBA")
    palette, index_of = palette_of([img], 1 << bits)
    return pack(img, bits, palette, index_of)


def from_file(path: str | Path, size: tuple[int, int] | None = None) -> bytes:
    img = Image.open(path).convert("RGBA")
    if size and img.size != size:
        img = img.resize(size, Image.LANCZOS)
    return encode(img)


def decode(data: bytes) -> Image.Image:
    """The other way (indexed images only): for previews."""
    h = struct.unpack("<I", data[:4])[0]
    cf, w, hh = h & 31, (h >> 10) & 0x7FF, (h >> 21) & 0x7FF
    bits = {v: k for k, v in CF_INDEXED.items()}[cf]
    n = 1 << bits
    pal = [(data[4 + 4 * i + 2], data[4 + 4 * i + 1], data[4 + 4 * i], data[4 + 4 * i + 3]) for i in range(n)]
    off, stride = 4 + 4 * n, (w * bits + 7) // 8
    img = Image.new("RGBA", (w, hh))
    px = img.load()
    for y in range(hh):
        for x in range(w):
            bit = x * bits
            px[x, y] = pal[(data[off + y * stride + bit // 8] >> (8 - bits - bit % 8)) & (n - 1)]
    return img
