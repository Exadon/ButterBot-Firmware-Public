"""
Draws the Clockstar watch themes Dimension Hopper ships (original art, generated from shapes and maths - nothing
copied from the watch's own artwork or from any show) into themes/<id>/:

  theme.json               id, name, description
  preview.png              how the watch face looks (for Dimension Hopper's Watch tab)
  bg.bin                   the clock screen's background (also behind the menu and settings)
  bg_bot.bin               the screen below the clock (notifications)
  clockIcons/0..9.bin      the clock's digits, 20 x 20
  clockIcons/colon.bin     7 x 20

On the watch a theme lives in /spiffs/t/<id>/ and replaces the files of the same name. The watch's storage keeps
names to 31 characters, so ids are 1-8 characters.
Run (needs Pillow):  python make_themes.py
"""
from __future__ import annotations

import json
import math
import random
from pathlib import Path

from PIL import Image

import lvgl_image as li

HERE = Path(__file__).resolve().parent
OUT = HERE / "themes"

RAMP = [(6, 30, 14), (14, 70, 30), (24, 120, 46), (40, 180, 70), (57, 255, 106), (130, 255, 90), (182, 255, 59),
        (225, 255, 190)]


def lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def ramp(v):
    v = max(0.0, min(1.0, v)) * (len(RAMP) - 1)
    i = int(v)
    return RAMP[-1] if i >= len(RAMP) - 1 else lerp(RAMP[i], RAMP[i + 1], v - i)


def portal_px(dx, dy, R, phase=0.0):
    """Colour of a swirling portal at offset (dx, dy) from its centre, or None outside it."""
    r = math.hypot(dx, dy)
    a = math.atan2(dy, dx)
    edge = R * (1 + 0.05 * math.sin(6 * a + phase * 2) + 0.03 * math.sin(11 * a - phase * 3))
    if r > edge:
        return None
    k = r / edge
    arms = 0.5 + 0.5 * math.sin(3 * a + 7 * k * math.pi / 2 - phase)
    v = 0.35 + 0.45 * arms * (1 - k) + 0.55 * (1 - k) ** 3
    if k > 0.88:
        v = max(v, 0.75 + 0.25 * (k - 0.88) / 0.12)
    return ramp(v)


def portal_lab_bg() -> Image.Image:
    """Night over the lab: a dark green sky with sparks, a big portal rising from the bottom (dimmed so the clock
    and menu stay readable)."""
    rnd = random.Random(42)
    img = Image.new("RGBA", (128, 128))
    px = img.load()
    for y in range(128):
        for x in range(128):
            px[x, y] = lerp((3, 10, 8), (8, 34, 20), y / 127) + (255,)
    for _ in range(26):                                        # specks
        x, y = rnd.randrange(128), rnd.randrange(80)
        px[x, y] = rnd.choice([(40, 110, 60), (60, 160, 80), (24, 80, 44)]) + (255,)
    for (cx, cy) in ((20, 22), (104, 14), (90, 58), (14, 66)):   # sparkles
        for d in range(-2, 3):
            c = (90, 220, 110) if d == 0 else (40, 130, 70)
            px[cx + d, cy] = c + (255,)
            px[cx, cy + d] = c + (255,)
    for y in range(70, 128):                                   # the portal, centre below the bottom edge
        for x in range(128):
            c = portal_px(x - 64, y - 150, 78, phase=0.6)
            if c:
                dim = 0.55 if y > 96 else 0.35 + 0.2 * (y - 70) / 26
                base = px[x, y][:3]
                px[x, y] = lerp(base, c, dim) + (255,)
    return img


def portal_lab_bot() -> Image.Image:
    """The notifications screen: the lab wall - dark grid, goo pooled along the bottom."""
    rnd = random.Random(9)
    img = Image.new("RGBA", (128, 128))
    px = img.load()
    for y in range(128):
        for x in range(128):
            c = (5, 16, 10) if (x % 16 and y % 16) else (10, 34, 18)
            px[x, y] = c + (255,)
    x = 0
    while x < 128:
        w = rnd.randint(8, 18)
        hgt = rnd.randint(3, 8)
        for xx in range(x, min(128, x + w)):
            t = (xx - x) / max(1, w - 1)
            top = 127 - int(hgt * math.sin(t * math.pi) ** 0.7)
            for y in range(top, 128):
                px[xx, y] = ((130, 255, 90) if y == top else (40, 180, 70)) + (255,)
        x += w - 2
    return img


# Chunky 8 x 12 digits, two-pixel strokes
DIGITS = {
    "0": [".111111.", "11111111", "11....11", "11...111", "11..1.11", "11.1..11", "111...11", "11....11", "11....11", "11....11", "11111111", ".111111."],
    "1": ["...11...", "..111...", ".1111...", "...11...", "...11...", "...11...", "...11...", "...11...", "...11...", "...11...", ".111111.", ".111111."],
    "2": [".111111.", "11111111", "11....11", "......11", ".....111", "....111.", "...111..", "..111...", ".111....", "111.....", "11111111", "11111111"],
    "3": [".111111.", "11111111", "11....11", "......11", "......11", "..11111.", "..11111.", "......11", "......11", "11....11", "11111111", ".111111."],
    "4": ["....111.", "...1111.", "..11.11.", ".11..11.", "11...11.", "11...11.", "11111111", "11111111", ".....11.", ".....11.", ".....11.", ".....11."],
    "5": ["11111111", "11111111", "11......", "11......", "1111111.", "11111111", "......11", "......11", "......11", "11....11", "11111111", ".111111."],
    "6": [".111111.", "11111111", "11....11", "11......", "11......", "1111111.", "11111111", "11....11", "11....11", "11....11", "11111111", ".111111."],
    "7": ["11111111", "11111111", "......11", ".....11.", ".....11.", "....11..", "....11..", "...11...", "...11...", "..11....", "..11....", "..11...."],
    "8": [".111111.", "11111111", "11....11", "11....11", "11....11", ".111111.", ".111111.", "11....11", "11....11", "11....11", "11111111", ".111111."],
    "9": [".111111.", "11111111", "11....11", "11....11", "11....11", "11111111", ".1111111", "......11", "......11", "11....11", "11111111", ".111111."],
}


GOO = dict(top=(120, 255, 100), bottom=(30, 170, 70), rim=(6, 40, 18), ink=(4, 26, 12), glint=(225, 255, 200),
           dot=(90, 240, 100))


def digit_tile(ch: str, c: dict = GOO, radius: int = 4, glint: bool = True) -> Image.Image:
    """A 20 x 20 tile with the digit cut in, a rim and a glint (colours from c)."""
    img = Image.new("RGBA", (20, 20), (0, 0, 0, 0))
    px = img.load()
    for y in range(20):
        for x in range(20):
            # rounded square
            cx = min(max(x, radius), 19 - radius)
            cy = min(max(y, radius), 19 - radius)
            d = (x - cx) ** 2 + (y - cy) ** 2
            if d > radius * radius:
                continue
            edge = d > (radius - 1) ** 2 or x in (0, 19) or y in (0, 19)
            if edge:
                px[x, y] = c["rim"] + (255,)
            else:
                px[x, y] = lerp(c["top"], c["bottom"], y / 19) + (255,)
    if glint:
        px[3, 3] = px[4, 3] = px[3, 4] = c["glint"] + (255,)
    for gy, row in enumerate(DIGITS[ch]):
        for gx, bit in enumerate(row):
            if bit == "1":
                px[6 + gx, 4 + gy] = c["ink"] + (255,)
    return img


def colon_tile(c: dict = GOO) -> Image.Image:
    img = Image.new("RGBA", (7, 20), (0, 0, 0, 0))
    px = img.load()
    for cy in (6, 13):
        for y in range(cy - 2, cy + 2):
            for x in range(1, 6):
                rim = y in (cy - 2, cy + 1) or x in (1, 5)
                px[x, y] = (c["rim"] if rim else c["dot"]) + (255,)
    return img


def write_theme(theme_id: str, name: str, description: str, files: dict[str, Image.Image], preview: Image.Image):
    folder = OUT / theme_id
    folder.mkdir(parents=True, exist_ok=True)
    for rel, img in files.items():
        p = folder / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(li.encode(img))
    preview.save(folder / "preview.png")
    (folder / "theme.json").write_text(json.dumps({"id": theme_id, "name": name, "description": description},
                                                  indent=2), encoding="utf-8")


def build_theme(theme_id: str, name: str, description: str, bg: Image.Image, bot: Image.Image, colours: dict,
                **tile):
    files = {"bg.bin": bg, "bg_bot.bin": bot, "clockIcons/colon.bin": colon_tile(colours)}
    for d in "0123456789":
        files[f"clockIcons/{d}.bin"] = digit_tile(d, colours, **tile)
    # Preview: the clock screen with 12:34 roughly where the watch draws it
    prev = bg.copy()
    x = 64 - (4 * 20 + 7 + 3 * 2) // 2
    for ch in "12:34":
        t = colon_tile(colours) if ch == ":" else digit_tile(ch, colours, **tile)
        prev.alpha_composite(t, (x, 40))
        x += t.width + 2
    write_theme(theme_id, name, description, files, prev)


def portal_lab():
    build_theme("portal", "Portal Lab",
                "Night over the lab: a portal rising behind the clock, goo-green digits, a lab wall with goo "
                "for the notifications.", portal_lab_bg(), portal_lab_bot(), GOO)


# --- Deep Space: a ringed planet over a starfield, ice-blue digits --------------------------------------------------
ICE = dict(top=(200, 240, 255), bottom=(70, 150, 230), rim=(10, 24, 60), ink=(8, 18, 48), glint=(255, 255, 255),
           dot=(140, 210, 255))


def stars(px, rnd, n):
    for _ in range(n):
        x, y = rnd.randrange(128), rnd.randrange(128)
        v = rnd.choice([70, 110, 160, 220])
        px[x, y] = (v, v, min(255, v + 30), 255)


def deep_space_bg() -> Image.Image:
    rnd = random.Random(7)
    img = Image.new("RGBA", (128, 128))
    px = img.load()
    for y in range(128):
        for x in range(128):
            px[x, y] = lerp((2, 3, 14), (10, 8, 34), y / 127) + (255,)
    stars(px, rnd, 70)
    # a planet low on the right, lit from the upper left, with a tilted ring
    pcx, pcy, R = 98, 112, 34
    for y in range(128):
        for x in range(128):
            dx, dy = x - pcx, y - pcy
            if dx * dx + dy * dy <= R * R:
                lit = max(0.0, (-dx - dy) / (R * 1.5)) + 0.15
                band = 0.08 * math.sin(dy / 3.0)
                px[x, y] = lerp((20, 14, 50), (150, 90, 170), max(0.0, min(1.0, lit + band))) + (255,)
    for t in range(720):
        a = t / 720 * 2 * math.pi
        for r in (46, 47, 50):
            x = pcx + r * math.cos(a)
            y = pcy + r * math.sin(a) * 0.28 - (x - pcx) * 0.18
            xi, yi = int(x), int(y)
            in_front = math.sin(a) > 0
            behind_planet = (xi - pcx) ** 2 + (yi - pcy) ** 2 <= R * R
            if 0 <= xi < 128 and 0 <= yi < 128 and (in_front or not behind_planet):
                px[xi, yi] = ((200, 170, 230) if r != 50 else (120, 100, 170)) + (255,)
    return img


def deep_space_bot() -> Image.Image:
    rnd = random.Random(11)
    img = Image.new("RGBA", (128, 128))
    px = img.load()
    for y in range(128):
        for x in range(128):
            px[x, y] = lerp((4, 4, 18), (2, 2, 10), y / 127) + (255,)
    stars(px, rnd, 50)
    # a faint nebula band across the bottom
    for y in range(80, 128):
        for x in range(128):
            v = 0.5 + 0.5 * math.sin(x / 11.0 + y / 7.0) * math.sin(y / 13.0)
            if v > 0.55:
                px[x, y] = lerp(px[x, y][:3], (60, 30, 90), (v - 0.55) * 0.9) + (255,)
    return img


def deep_space():
    build_theme("space", "Deep Space",
                "A ringed planet over a starfield, ice-blue digits, a quiet nebula behind the notifications.",
                deep_space_bg(), deep_space_bot(), ICE)


# --- Butter: ButterBot's own colours - cream digits on golden toast, a pat of butter ---------------------------------
CREAM = dict(top=(255, 246, 200), bottom=(250, 210, 90), rim=(90, 52, 14), ink=(80, 44, 10), glint=(255, 255, 240),
             dot=(255, 220, 110))


def butter_bg() -> Image.Image:
    img = Image.new("RGBA", (128, 128))
    px = img.load()
    rnd = random.Random(3)
    for y in range(128):
        for x in range(128):
            # toast: brown with crumb speckle, darker toward the crust at the edges
            edge = min(x, y, 127 - x, 127 - y)
            base = lerp((70, 38, 12), (130, 78, 30), min(1.0, edge / 18))
            n = rnd.randint(-8, 8)
            px[x, y] = tuple(max(0, min(255, c + n)) for c in base) + (255,)
    # a pat of butter in the lower half, melting at the edges
    for y in range(84, 118):
        for x in range(30, 98):
            k = ((x - 64) / 34) ** 4 + ((y - 101) / 17) ** 4
            if k <= 1:
                px[x, y] = lerp((255, 238, 150), (240, 190, 60), (y - 84) / 34) + (255,)
            elif k <= 1.6:
                px[x, y] = lerp(px[x, y][:3], (230, 170, 50), 0.6) + (255,)
    for (x0, y0) in ((40, 88), (41, 88), (40, 89)):            # shine
        px[x0, y0] = (255, 255, 230, 255)
    return img


def butter_bot() -> Image.Image:
    img = Image.new("RGBA", (128, 128))
    px = img.load()
    for y in range(128):
        for x in range(128):
            check = ((x // 16) + (y // 16)) % 2
            px[x, y] = ((60, 34, 12) if check else (48, 26, 8)) + (255,)
    return img


def butter():
    build_theme("butter", "Butter",
                "ButterBot's own colours: cream digits on golden toast with a pat of butter, a checked tablecloth "
                "for the notifications.", butter_bg(), butter_bot(), CREAM, radius=3)


# --- Lab Terminal: an old lab screen - green scanlines, square amber digits ----------------------------------------
AMBER = dict(top=(255, 190, 60), bottom=(220, 130, 20), rim=(40, 22, 0), ink=(30, 16, 0), glint=(255, 230, 160),
             dot=(255, 170, 40))


def terminal_bg(seed: int, readout: bool) -> Image.Image:
    img = Image.new("RGBA", (128, 128))
    px = img.load()
    rnd = random.Random(seed)
    for y in range(128):
        for x in range(128):
            v = 6 if y % 3 else 12                             # scanlines
            dx, dy = (x - 64) / 64, (y - 64) / 64
            vign = max(0.0, 1 - 0.35 * (dx * dx + dy * dy))   # the tube's darker corners
            px[x, y] = (int(v * 0.6 * vign), int(v * 1.6 * vign), int(v * 0.6 * vign), 255)
    if readout:   # a few rows of "readout" at the top and bottom
        for row in (8, 12, 104, 108, 112):
            x = 6
            while x < 122:
                w = rnd.randint(2, 7)
                if rnd.random() < 0.75:
                    for xx in range(x, min(122, x + w)):
                        px[xx, row] = (40, 110, 50, 255)
                        px[xx, row + 1] = (24, 70, 30, 255)
                x += w + rnd.randint(1, 3)
    return img


def terminal():
    build_theme("terminal", "Lab Terminal",
                "An old lab screen: green scanlines and readouts, square amber digits.",
                terminal_bg(5, True), terminal_bg(6, False), AMBER, radius=1, glint=False)


THEMES = (portal_lab, deep_space, butter, terminal)

if __name__ == "__main__":
    for make in THEMES:
        make()
    print("written to", OUT)
