"""
Regenerates Dimension Hopper's artwork (all original, drawn procedurally):
  portal.ico / portal.png  - app icon
  bot.png                  - ButterBot slot icon, cartoon style: cube head + lens, jointed arms, tank treads
  controller.png           - Controller slot icon: the green PCB with ESP32 module, screen, toggle, joystick, buttons

Run:  .venv\\Scripts\\python make_art.py
Drawn at 4x and downsampled for clean anti-aliasing.
"""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

HERE = Path(__file__).parent
SS = 4  # supersampling factor

GREEN = (57, 255, 106)
LIME = (182, 255, 59)
TEAL = (31, 214, 176)
DARK = (14, 26, 18)
DARKER = (7, 13, 9)
EDGE = (31, 90, 44)
BUTTER = (255, 214, 92)
BUTTER_HI = (255, 240, 170)
WHITE = (234, 255, 239)


def canvas(w, h):
    img = Image.new("RGBA", (w * SS, h * SS), (0, 0, 0, 0))
    return img, ImageDraw.Draw(img)


def S(*v):
    """Scale design-space coordinates (in final pixels) to the supersampled canvas."""
    return [x * SS for x in v]


def finish(img, size, path, glow=True):
    out = img
    if glow:
        g = img.filter(ImageFilter.GaussianBlur(3 * SS))
        out = Image.alpha_composite(g, img)
    out = out.resize(size, Image.LANCZOS)
    out.save(path)
    return out


def finish_halo(img, size, path, color=GREEN, strength=0.55):
    """Soft portal-green glow behind a realistically coloured object, then downsample."""
    alpha = img.getchannel("A").filter(ImageFilter.GaussianBlur(2.5 * SS))
    alpha = alpha.point(lambda a: int(a * strength))
    halo = Image.new("RGBA", img.size, color + (0,))
    halo.putalpha(alpha)
    out = Image.alpha_composite(halo, img).resize(size, Image.LANCZOS)
    out.save(path)
    return out


# ------------------------------------------------------------------------------------------
def portal():
    size = 256
    img, d = canvas(64, 64)
    c = 32
    for r, col, w in ((30, EDGE, 3), (28, GREEN, 2.5), (25, LIME, 2)):
        d.ellipse(S(c - r, c - r * 0.92, c + r, c + r * 0.92), outline=col, width=int(w * SS))
    for k in range(7):
        for j in range(5):
            r = 26 - j * 4.5
            st = k * 360 / 7 + j * 30
            d.arc(S(c - r, c - r * 0.92, c + r, c + r * 0.92), st, st + 40 - j * 4,
                  fill=[GREEN, LIME, TEAL][(k + j) % 3], width=int(max(0.8, 2.5 - j * 0.45) * SS))
    for r, col in ((8.5, (18, 61, 30)), (6.5, (31, 122, 55)), (4.5, GREEN), (2.2, WHITE)):
        d.ellipse(S(c - r, c - r, c + r, c + r), fill=col)
    out = finish(img, (size, size), HERE / "portal.png")
    out.save(HERE / "portal.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])


# ------------------------------------------------------------------------------------------
def bot(out_size=(84, 96), name="bot.png"):
    """ButterBot in a flat cartoon style: cube head (shaded top + side) with a big lens and wire loops,
    tapered neck on a thin column, long jointed arms with flat pincer feet, tank-tread base."""
    img, d = canvas(64, 72)
    INK = (22, 24, 28)                      # cartoon outline
    ow = int(1.3 * SS)
    HEAD, HEAD_TOP, HEAD_SIDE = (188, 194, 200), (214, 219, 224), (146, 152, 160)
    METAL, METAL_DK, JOINT = (104, 110, 118), (78, 84, 92), (170, 176, 184)
    TREAD, WHEEL = (30, 32, 36), (196, 150, 70)

    def poly(pts, fill):
        d.polygon(S(*pts), fill=fill, outline=INK, width=ow)

    # wire loops on top of the head (behind it)
    for x0, col in ((24, (226, 138, 40)), (29, (240, 176, 52))):
        d.arc(S(x0, 1.5, x0 + 8, 11), 180, 360, fill=INK, width=int(2.6 * SS))
        d.arc(S(x0, 1.5, x0 + 8, 11), 180, 360, fill=col, width=int(1.5 * SS))
    d.ellipse(S(40, 4.5, 43, 7.5), fill=(200, 60, 50), outline=INK, width=SS)  # little red nub

    # neck: tapered block under the head, then a thin column down to the base
    poly((26, 30, 38, 30, 36, 41, 28, 41), METAL)
    poly((29.5, 41, 34.5, 41, 34.5, 57, 29.5, 57), METAL_DK)

    # arms: shoulder joint on the head's lower sides, elbow out, long forearm, flat pincer foot
    for side in (-1, 1):
        shx, elx, wrx = 32 + side * 15.5, 32 + side * 18.5, 32 + side * 16.5
        d.line(S(shx, 25, elx, 40), fill=INK, width=int(4.6 * SS))
        d.line(S(shx, 25, elx, 40), fill=METAL, width=int(3.0 * SS))
        d.line(S(elx, 40, wrx, 55), fill=INK, width=int(4.6 * SS))
        d.line(S(elx, 40, wrx, 55), fill=METAL, width=int(3.0 * SS))
        for jx, jy, r in ((shx, 25, 2.4), (elx, 40, 2.4)):
            d.ellipse(S(jx - r, jy - r, jx + r, jy + r), fill=JOINT, outline=INK, width=ow)
            d.ellipse(S(jx - 0.7, jy - 0.7, jx + 0.7, jy + 0.7), fill=INK)
        # flat foot/claw pointing inward, plus a small back toe
        poly((wrx - 1.6, 53.5, wrx + 1.6, 53.5, wrx + side * 1.5, 58.5, wrx - side * 7, 59.8, wrx - side * 7, 57.4),
             (200, 204, 210))

    # head: front face, slanted top face, shaded right side
    poly((15, 9, 44, 9, 49, 5, 20, 5), HEAD_TOP)
    poly((44, 9, 49, 5, 49, 27, 44, 31), HEAD_SIDE)
    poly((15, 9, 44, 9, 44, 31, 15, 31), HEAD)
    # lens: dark barrel, grey bezel, teal glass, glint
    cx, cy = 27.5, 19.5
    d.ellipse(S(cx - 9.5, cy - 9.5, cx + 9.5, cy + 9.5), fill=(70, 76, 84), outline=INK, width=ow)
    d.ellipse(S(cx - 7.4, cy - 7.4, cx + 7.4, cy + 7.4), fill=(34, 38, 44), outline=INK, width=SS)
    d.ellipse(S(cx - 5, cy - 5, cx + 5, cy + 5), fill=(40, 92, 104))
    d.ellipse(S(cx - 3.2, cy - 3.2, cx + 3.2, cy + 3.2), fill=(150, 228, 236))
    d.ellipse(S(cx - 4.2, cy - 4.6, cx - 1.6, cy - 2.0), fill=(240, 255, 255))

    # tank-tread base: tread loop, grey deck on top, three round wheels
    d.rounded_rectangle(S(11, 57, 53, 70), radius=6 * SS, fill=TREAD, outline=INK, width=ow)
    for i in range(9):  # tread cleats
        x = 15 + i * 4.3
        d.line(S(x, 57.6, x, 59.2), fill=(70, 74, 80), width=SS)
    poly((16, 55, 48, 55, 50, 58.5, 14, 58.5), (150, 156, 164))
    for x in (21.5, 32, 42.5):
        d.ellipse(S(x - 3.6, 60, x + 3.6, 67.2), fill=(54, 56, 60), outline=INK, width=SS)
        d.ellipse(S(x - 1.8, 61.8, x + 1.8, 65.4), fill=WHEEL)
    finish_halo(img, out_size, HERE / name)


# ------------------------------------------------------------------------------------------
def controller(out_size=(60, 110), name="controller.png"):
    """Controller, as built: portrait green PCB, ESP32 module + green LED up top, colour screen,
    ON/OFF toggle, big black joystick, red/black buttons left, red/yellow buttons right."""
    img, d = canvas(50, 92)
    INK = (10, 30, 16)
    PCB, PCB_EDGE, SILK = (24, 128, 60), (12, 74, 34), (226, 236, 228)
    GOLD = (214, 178, 84)
    ow = int(1.0 * SS)

    # board, with slightly cut-in "shoulders" at the top like the real outline
    d.rounded_rectangle(S(4, 6, 46, 89), radius=7 * SS, fill=PCB, outline=PCB_EDGE, width=int(1.2 * SS))
    for x in (4, 46):
        d.ellipse(S(x - 2.2, 15, x + 2.2, 19.4), fill=(0, 0, 0, 0))
    # mounting holes
    for x, y in ((9, 11), (41, 11), (9, 84), (41, 84)):
        d.ellipse(S(x - 2.2, y - 2.2, x + 2.2, y + 2.2), fill=GOLD)
        d.ellipse(S(x - 1.1, y - 1.1, x + 1.1, y + 1.1), fill=INK)
    # white silkscreen hatching down both edges
    for i in range(5):
        y = 27 + i * 3.4
        d.line(S(5.5, y + 2, 8.5, y), fill=SILK, width=int(0.8 * SS))
        d.line(S(41.5, y, 44.5, y + 2), fill=SILK, width=int(0.8 * SS))
    for i in range(4):
        d.ellipse(S(6.2, 47 + i * 2.2, 7.4, 48.2 + i * 2.2), fill=SILK)   # tiny label dots

    # ESP32 module (silver can) at the top edge, green status LED to its right
    d.rounded_rectangle(S(16, 3, 34, 16), radius=1 * SS, fill=(176, 182, 188), outline=(110, 116, 122), width=ow)
    d.rectangle(S(18, 7, 32, 14.5), fill=(200, 205, 210))
    for i in range(3):
        d.line(S(19.5, 9 + i * 1.8, 30.5, 9 + i * 1.8), fill=(140, 146, 152), width=int(0.6 * SS))
    d.ellipse(S(36.5, 6.5, 41.5, 11.5), fill=(120, 255, 140))
    d.ellipse(S(37.8, 7.8, 40.2, 10.2), fill=(230, 255, 235))

    # screen: black bezel, dark display with a yellow header and green text rows
    d.rounded_rectangle(S(11, 19, 39, 44), radius=1.5 * SS, fill=(12, 12, 14), outline=(40, 44, 48), width=ow)
    d.rectangle(S(13.5, 21.5, 36.5, 41.5), fill=(8, 18, 12))
    d.rectangle(S(13.5, 21.5, 36.5, 24.5), fill=(232, 196, 40))
    d.rectangle(S(14.5, 26, 29, 28), fill=(70, 200, 90))
    for i, w in enumerate((18, 14, 16, 9)):
        d.rectangle(S(15, 30 + i * 2.8, 15 + w, 31.4 + i * 2.8), fill=(110, 220, 120))

    # flex connector, then the ON/OFF toggle
    d.rectangle(S(17, 46, 33, 48.6), fill=(214, 204, 176))
    d.rounded_rectangle(S(20.5, 51, 29.5, 56.5), radius=1 * SS, fill=(150, 156, 162), outline=(80, 86, 92), width=ow)
    d.line(S(25, 53.8, 27.5, 50.2), fill=(220, 224, 228), width=int(1.6 * SS))

    # buttons: red top-left, black bottom-left, red top-right, yellow bottom-right
    for (x, y), col in (((11.5, 57.5), (220, 44, 40)), ((11.5, 69.5), (26, 28, 30)),
                        ((38.5, 57.5), (220, 44, 40)), ((38.5, 69.5), (246, 204, 36))):
        d.ellipse(S(x - 5, y - 5, x + 5, y + 5), fill=(20, 22, 24))
        d.ellipse(S(x - 3.8, y - 3.8, x + 3.8, y + 3.8), fill=col)
        d.ellipse(S(x - 2.4, y - 2.8, x - 0.2, y - 0.8), fill=(255, 255, 255, 150))
    # joystick: big black stick between them
    d.ellipse(S(15, 59, 35, 79), fill=(22, 24, 26), outline=(52, 56, 60), width=ow)
    d.ellipse(S(18.5, 62.5, 31.5, 75.5), fill=(36, 38, 42))
    d.ellipse(S(20.5, 64, 25.5, 68.5), fill=(84, 88, 94))
    # USB-C at the bottom edge
    d.rounded_rectangle(S(21, 86.5, 29, 90.5), radius=1.2 * SS, fill=(186, 190, 196), outline=(110, 116, 122), width=ow)
    finish_halo(img, out_size, HERE / name)


if __name__ == "__main__":
    portal()
    bot()
    controller()
    # larger renders for the Guide tab diagrams
    bot((196, 224), "bot_guide.png")
    controller((150, 276), "controller_guide.png")
    print("art written to", HERE)
