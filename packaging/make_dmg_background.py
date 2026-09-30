"""Draws the DMG window's background (run by hand; the PNGs are committed so the build needs no Pillow).

    python3 packaging/make_dmg_background.py        # -> packaging/dmg/background.png and background@2x.png

660 x 440 points: a headline, an arrow from the app to Applications, and a card about the installer script.
The icons themselves are placed by packaging/dmg_settings.py, so the positions here must match it.
"""
import math
import os
import sys

from PIL import Image, ImageDraw, ImageFilter, ImageFont

W, H = 660, 440
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dmg")
MINT = (25, 184, 148)          # the arrow
INK = (22, 38, 44)
DIM = (92, 112, 118)


def font(size, weight="Semibold"):
    """SF, from the system (the build Mac always has it); Helvetica Neue if it is ever missing."""
    for path, index in (("/System/Library/Fonts/SFNS.ttf", None), ("/System/Library/Fonts/HelveticaNeue.ttc", 0)):
        if os.path.exists(path):
            f = ImageFont.truetype(path, size)
            if index is None:
                try:
                    f.set_variation_by_name(weight)
                except Exception:
                    pass
            return f
    return ImageFont.load_default()


def draw(scale: int) -> Image.Image:
    s = scale
    img = Image.new("RGB", (W * s, H * s))
    px = img.load()
    top, bottom = (238, 250, 246), (222, 240, 244)
    for y in range(H * s):                      # a soft vertical wash, mint to sky
        t = y / (H * s - 1)
        row = tuple(round(top[i] + (bottom[i] - top[i]) * t) for i in range(3))
        for x in range(W * s):
            px[x, y] = row
    # The glow is an alpha mask over a flat colour (blurring an RGBA image would darken its edges).
    mask = Image.new("L", img.size, 0)
    ImageDraw.Draw(mask).ellipse((W * s * 0.05, -H * s * 0.55, W * s * 0.95, H * s * 0.35), fill=130)
    glow = Image.new("RGBA", img.size, (160, 235, 215, 0))
    glow.putalpha(mask.filter(ImageFilter.GaussianBlur(40 * s)))
    img = Image.alpha_composite(img.convert("RGBA"), glow)
    d = ImageDraw.Draw(img)

    def text(xy, words, size, fill, weight="Semibold", anchor="mm"):
        d.text((xy[0] * s, xy[1] * s), words, font=font(size * s, weight), fill=fill, anchor=anchor)

    text((W / 2, 48), "Drag Hey Mint to Applications", 27, INK)
    text((W / 2, 80), "Then open it from Launchpad or Spotlight. It asks for what it needs, step by step.", 13, DIM,
         "Regular")

    # The arrow: a dashed arc from the app's slot to Applications, with a head.
    x0, x1, y = 252, 408, 172
    points = []
    for i in range(0, 61):
        t = i / 60
        points.append((x0 + (x1 - x0) * t, y - math.sin(math.pi * t) * 22))
    for i in range(0, 57, 4):
        d.line([(points[i][0] * s, points[i][1] * s), (points[i + 2][0] * s, points[i + 2][1] * s)], fill=MINT,
               width=int(5 * s))
    ex, ey = points[-1]
    ang = math.atan2(points[-1][1] - points[-3][1], points[-1][0] - points[-3][0])
    head = [(ex + 4 * math.cos(ang), ey + 4 * math.sin(ang))]
    for da in (2.5, -2.5):
        head.append((ex + 4 * math.cos(ang) + 15 * math.cos(ang + da), ey + 4 * math.sin(ang) + 15 * math.sin(ang + da)))
    d.polygon([(a * s, b * s) for a, b in head], fill=MINT)

    # The card about the installer script.
    card = (36, 262, W - 36, 420)
    smask = Image.new("L", img.size, 0)
    ImageDraw.Draw(smask).rounded_rectangle([c * s for c in (card[0], card[1] + 4, card[2], card[3] + 4)],
                                            radius=18 * s, fill=60)
    shadow = Image.new("RGBA", img.size, (20, 60, 70, 0))
    shadow.putalpha(smask.filter(ImageFilter.GaussianBlur(9 * s)))
    img = Image.alpha_composite(img, shadow)
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([c * s for c in card], radius=18 * s, fill=(255, 255, 255, 215), outline=(255, 255, 255, 255),
                        width=int(1.5 * s))
    text((60, 292), "macOS says it can't check Hey Mint?", 15, INK, anchor="lm")
    text((60, 316), "Open Terminal, paste this line and press Return:", 12.5, DIM, "Regular", "lm")
    pill = (58, 330, 436, 358)                   # the command, in a pill (it is also on the page the icon opens)
    d.rounded_rectangle([c * s for c in pill], radius=8 * s, fill=(232, 244, 242, 255), outline=(200, 224, 220, 255),
                        width=int(1 * s))
    mono = ImageFont.truetype("/System/Library/Fonts/Menlo.ttc", int(12 * s)) if os.path.exists(
        "/System/Library/Fonts/Menlo.ttc") else font(12 * s, "Regular")
    d.text((70 * s, 344 * s), "curl -fsSL hey-mint.pages.dev/install.sh | bash", font=mono, fill=INK, anchor="lm")
    text((60, 378), "It installs Hey Mint, approves it with macOS and opens it.", 12, DIM, "Regular", "lm")
    text((60, 399), "Or: System Settings \u25B8 Privacy & Security \u25B8 Open Anyway.", 11.5, (130, 148, 152), "Regular", "lm")
    return img.convert("RGB")


if __name__ == "__main__":
    os.makedirs(OUT, exist_ok=True)
    draw(1).save(os.path.join(OUT, "background.png"))
    draw(2).save(os.path.join(OUT, "background@2x.png"))
    print("wrote", OUT, file=sys.stderr)
