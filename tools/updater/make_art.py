"""
Regenerates Dimension Hopper's artwork (all original, drawn procedurally):
  portal.ico / portal.png  - app icon
  bot.png                  - ButterBot slot icon: cube head with camera lens, slim neck, claw arms, wheeled base
  controller.png           - Controller slot icon: portrait green PCB with screen, joystick and buttons

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
def bot():
    """ButterBot: light-grey cube head with a big camera lens and wires on top, slim dark neck,
    thin arms ending in claws, black wheeled base with a white middle band."""
    img, d = canvas(64, 64)
    GREY, GREY_SIDE, GREY_EDGE = (222, 226, 220), (178, 184, 176), (120, 128, 120)
    DARKM, DARKM2 = (44, 48, 45), (28, 31, 29)

    # wires poking out of the top of the head (red, yellow, green)
    for x, col, top in ((28.5, (230, 60, 50), 3.5), (31.5, (250, 200, 40), 2.5), (34.5, (60, 200, 90), 4)):
        d.line(S(x, 11, x, top + 1.5), fill=col, width=int(1.6 * SS))
        d.ellipse(S(x - 1.1, top, x + 1.1, top + 2.2), fill=col)

    # neck column
    d.rounded_rectangle(S(29.5, 33, 34.5, 49), radius=1.2 * SS, fill=DARKM)
    d.rectangle(S(29.5, 36, 34.5, 37.5), fill=(200, 205, 198))

    # arms: shoulder at the neck, elbow out, forearm hanging down, claw at the end
    ARM, JOINT = (112, 120, 114), (150, 158, 152)
    for side in (-1, 1):
        sx, ex, hx = 32 + side * 2.5, 32 + side * 15, 32 + side * 15.5
        d.line(S(sx, 36.5, ex, 38.5, hx, 50.5), fill=ARM, width=int(2.4 * SS), joint="curve")
        d.ellipse(S(ex - 1.8, 36.7, ex + 1.8, 40.3), fill=JOINT)
        # open two-finger claw
        d.line(S(hx, 50.5, hx - side * 3, 55), fill=ARM, width=int(1.7 * SS))
        d.line(S(hx, 50.5, hx + side * 2, 55.5), fill=ARM, width=int(1.7 * SS))

    # head: cube with a shaded right side for a bit of depth
    d.rounded_rectangle(S(18, 9, 46, 34), radius=2.5 * SS, fill=GREY, outline=GREY_EDGE, width=SS)
    d.rounded_rectangle(S(41.5, 9.6, 45.4, 33.4), radius=2 * SS, fill=GREY_SIDE)
    # camera lens: grey bezel, black barrel, glassy centre with a highlight
    c, cy = 30.5, 21.5
    for r, col in ((9, (150, 156, 150)), (7.6, (20, 22, 21)), (4.8, (45, 52, 60)), (3.2, (18, 30, 48))):
        d.ellipse(S(c - r, cy - r, c + r, cy + r), fill=col)
    d.ellipse(S(c - 2.6, cy - 3.0, c - 0.6, cy - 1.0), fill=(220, 235, 255))
    d.ellipse(S(c + 1.2, cy + 1.0, c + 2.2, cy + 2.0), fill=(120, 150, 190))

    # base: black chassis with chunky wheels and a white middle band
    d.rounded_rectangle(S(13, 48, 51, 59), radius=4 * SS, fill=DARKM2)
    for x in (17.5, 46.5):
        d.ellipse(S(x - 5, 48.5, x + 5, 58.5), fill=(16, 17, 16), outline=(70, 76, 72), width=SS)
        d.ellipse(S(x - 1.8, 51.7, x + 1.8, 55.3), fill=(90, 96, 92))
    d.rounded_rectangle(S(23, 49.5, 41, 57.5), radius=1.5 * SS, fill=(232, 234, 230))
    d.rectangle(S(29, 52.5, 35, 54.5), fill=(60, 64, 62))     # front sensor window
    d.ellipse(S(37, 52.6, 39, 54.6), fill=GREEN)              # status LED
    finish_halo(img, (96, 96), HERE / "bot.png")


# ------------------------------------------------------------------------------------------
def controller():
    """Controller: a portrait green PCB with a small colour screen up top, USB port, black
    joystick, red buttons either side and a yellow one."""
    img, d = canvas(64, 64)
    PCB, PCB_EDGE, PCB_TRACE = (30, 140, 64), (14, 84, 36), (52, 170, 86)
    GOLD = (214, 178, 84)

    # USB connector on the top edge
    d.rounded_rectangle(S(28, 1.5, 36, 6.5), radius=1 * SS, fill=(190, 194, 198), outline=(120, 124, 128), width=SS)
    # board
    d.rounded_rectangle(S(17, 4, 47, 62), radius=5 * SS, fill=PCB, outline=PCB_EDGE, width=int(1.2 * SS))
    # a few traces for PCB texture
    for y in (30, 33.5):
        d.line(S(19.5, y, 26, y, 28, y + 2), fill=PCB_TRACE, width=int(0.8 * SS))
        d.line(S(44.5, y, 38, y, 36, y + 2), fill=PCB_TRACE, width=int(0.8 * SS))
    d.line(S(32, 57, 32, 60), fill=PCB_TRACE, width=int(0.8 * SS))
    # mounting holes
    for x, y in ((20.5, 7.5), (43.5, 7.5), (20.5, 58.5), (43.5, 58.5)):
        d.ellipse(S(x - 1.8, y - 1.8, x + 1.8, y + 1.8), fill=GOLD)
        d.ellipse(S(x - 0.8, y - 0.8, x + 0.8, y + 0.8), fill=(10, 30, 16))
    # screen: black bezel, colourful UI
    d.rounded_rectangle(S(22.5, 9, 41.5, 27), radius=1.2 * SS, fill=(14, 14, 16))
    d.rectangle(S(24, 10.5, 40, 25.5), fill=(18, 38, 64))
    d.rectangle(S(24, 10.5, 40, 13), fill=(46, 120, 220))                      # title bar
    d.rounded_rectangle(S(27.5, 15, 36.5, 23.5), radius=1.6 * SS, fill=(220, 224, 218))  # tiny bot face
    d.ellipse(S(30, 17.2, 34, 21.2), fill=(20, 22, 21))
    d.ellipse(S(31.2, 18.2, 32.4, 19.4), fill=(200, 220, 255))
    d.rectangle(S(25, 23.8, 28.5, 24.8), fill=GREEN)
    # joystick
    d.ellipse(S(25, 37, 39, 51), fill=(22, 24, 23), outline=(60, 64, 62), width=SS)
    d.ellipse(S(28, 40, 36, 48), fill=(48, 52, 50))
    d.ellipse(S(29.5, 41.2, 32, 43.7), fill=(90, 96, 92))
    # buttons: red either side, yellow lower right
    for x, y, col, r in ((21.5, 38, (226, 52, 44), 2.8), (42.5, 38, (226, 52, 44), 2.8),
                         (42.5, 50, (250, 204, 40), 2.6), (21.5, 50, (226, 52, 44), 2.6)):
        d.ellipse(S(x - r - 0.6, y - r - 0.6, x + r + 0.6, y + r + 0.6), fill=(20, 22, 21))
        d.ellipse(S(x - r, y - r, x + r, y + r), fill=col)
        d.ellipse(S(x - r * 0.45, y - r * 0.6, x, y - r * 0.15), fill=(255, 255, 255, 140))
    finish_halo(img, (96, 96), HERE / "controller.png")


if __name__ == "__main__":
    portal()
    bot()
    controller()
    print("art written to", HERE)
