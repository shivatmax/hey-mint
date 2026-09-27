"""Render the Mint orb as an app icon (.icns)."""
import math, subprocess, sys, tempfile
from pathlib import Path
from PIL import Image, ImageDraw, ImageFilter

S = 1024

def orb() -> Image.Image:
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    # macOS icon grid: rounded square inset ~10%.
    pad, radius = 100, 185
    bg = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle([pad, pad, S - pad, S - pad], radius, fill=255)
    grad = Image.new("RGBA", (S, S))
    top, bottom = (28, 30, 44), (10, 11, 18)
    for y in range(S):
        t = y / S
        grad.paste(tuple(int(a + (b - a) * t) for a, b in zip(top, bottom)) + (255,), (0, y, S, y + 1))
    bg.paste(grad, (0, 0), mask)
    img.alpha_composite(bg)

    cx = cy = S // 2
    # Glow.
    glow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse([cx - 250, cy - 250, cx + 250, cy + 250], fill=(90, 150, 255, 150))
    glow = glow.filter(ImageFilter.GaussianBlur(70))
    img.alpha_composite(Image.composite(glow, Image.new("RGBA", (S, S)), mask))

    # Core: radial gradient from a highlight at upper left to deep blue-violet.
    r = 205
    core = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    px = core.load()
    hx, hy = cx - 70, cy - 80
    inner, mid, outer = (235, 245, 255), (80, 150, 255), (110, 70, 220)
    for y in range(cy - r, cy + r):
        for x in range(cx - r, cx + r):
            if (x - cx) ** 2 + (y - cy) ** 2 > r * r:
                continue
            d = min(1.0, math.hypot(x - hx, y - hy) / (r * 1.55))
            if d < 0.45:
                t = d / 0.45; c = tuple(int(a + (b - a) * t) for a, b in zip(inner, mid))
            else:
                t = (d - 0.45) / 0.55; c = tuple(int(a + (b - a) * t) for a, b in zip(mid, outer))
            px[x, y] = c + (255,)
    img.alpha_composite(core)

    # Thin ring.
    ring = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ImageDraw.Draw(ring).ellipse([cx - 290, cy - 290, cx + 290, cy + 290], outline=(120, 170, 255, 110), width=10)
    img.alpha_composite(ring)
    return img

def main(out: str) -> None:
    base = orb()
    with tempfile.TemporaryDirectory() as tmp:
        iconset = Path(tmp) / "Mint.iconset"
        iconset.mkdir()
        for size in (16, 32, 128, 256, 512):
            base.resize((size, size), Image.LANCZOS).save(iconset / f"icon_{size}x{size}.png")
            base.resize((size * 2, size * 2), Image.LANCZOS).save(iconset / f"icon_{size}x{size}@2x.png")
        subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", out], check=True)
    base.resize((256, 256), Image.LANCZOS).save(Path(out).with_suffix(".png"))

if __name__ == "__main__":
    main(sys.argv[1])
