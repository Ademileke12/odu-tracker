#!/usr/bin/env python3
"""Generate assets/icon.ico (+png) from scratch with Pillow."""
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "assets")

try:
    from PIL import Image, ImageDraw
except ImportError:
    print("Pillow not installed — run: pip install pillow")
    raise SystemExit(1)

GOLD = (255, 179, 50, 255)
GOLD2 = (255, 157, 46, 255)
DARK = (34, 23, 1, 255)
CLEAR = (0, 0, 0, 0)


def draw(size):
    s = size / 48.0
    img = Image.new("RGBA", (size, size), CLEAR)
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, size - 1, size - 1], radius=int(11 * s), fill=GOLD)
    # antenna mast + waves (same geometry as the web SVG logo)
    lw = max(2, int(3.6 * s))
    d.line([16 * s, 33 * s, 16 * s, 24 * s], fill=DARK, width=lw)
    d.ellipse([16 * s - 3.1 * s, 33 * s - 3.1 * s, 16 * s + 3.1 * s, 33 * s + 3.1 * s],
              fill=DARK)
    d.arc([16 * s - 8.5 * s, 24 * s - 8.5 * s, 25 * s, 24 * s + 8.5 * s],
          start=-90, end=0, fill=DARK, width=lw)
    d.arc([16 * s - 15.4 * s, 24 * s - 15.4 * s, 31.4 * s, 39.1 * s],
          start=-90, end=0, fill=DARK, width=lw)
    return img


def main():
    os.makedirs(OUT, exist_ok=True)
    sizes = [16, 24, 32, 48, 64, 128, 256]
    imgs = [draw(sz) for sz in sizes]
    ico = os.path.join(OUT, "icon.ico")
    imgs[-1].save(ico, format="ICO", sizes=[(s, s) for s in sizes],
                  append_images=imgs[:-1])
    draw(256).save(os.path.join(OUT, "icon.png"))
    print("wrote", ico)


if __name__ == "__main__":
    main()
