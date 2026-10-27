#!/usr/bin/env python3
"""Generate Android/desktop brand assets from scratch with Pillow.

Outputs
-------
assets/icon.png            1024x1024 launcher/source icon (desktop + electron)
android_app/assets/icon.png  same — buildozer's `icon.filename` resolves here
                            (cwd is android_app/ during CI) and p4a derives
                            every launcher mipmap density from it; 1024px was
                            needed because the old 256px source made Android
                            upscale a blurry logo ("not scaled").
android_app/assets/presplash.png  1080x1920 full-screen launch image
                            (buildozer's `presplash.filename`) — solid dark
                            background matching the app theme, logo centred,
                            wordmark underneath. The activity shows it with
                            ImageView FIT_CENTER, so same-colour letterbox
                            bars are invisible on taller/wider screens.
assets/icon.ico            classic multi-size .ico (ICO caps at 256px)
"""
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "assets")
ANDROID_OUT = os.path.join(ROOT, "android_app", "assets")

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    print("Pillow not installed — run: pip install pillow")
    raise SystemExit(1)

GOLD = (255, 179, 50, 255)
GOLD2 = (255, 157, 46, 255)
DARK = (34, 23, 1, 255)
CLEAR = (0, 0, 0, 0)

APP_BG = (5, 7, 11, 255)              # #05070b — android.presplash_color
ICON_PX = 1024                        # launcher/source icon resolution
PRESPLASH_W, PRESPLASH_H = 1080, 1920
FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


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


def wordmark_font(px):
    try:
        return ImageFont.truetype(FONT_BOLD, px)
    except Exception:
        return None


def make_presplash():
    """Full-screen launch image: dark canvas + centred logo + wordmark."""
    img = Image.new("RGBA", (PRESPLASH_W, PRESPLASH_H), APP_BG)

    logo_px = 560
    logo = draw(logo_px)
    lx = (PRESPLASH_W - logo_px) // 2
    ly = int(PRESPLASH_H * 0.40)
    img.alpha_composite(logo, (lx, ly))

    font = wordmark_font(84)
    if font is not None:
        d = ImageDraw.Draw(img)
        label = "ODU TRACKER"
        box = d.textbbox((0, 0), label, font=font)
        tw, th = box[2] - box[0], box[3] - box[1]
        tx = (PRESPLASH_W - tw) // 2 - box[0]
        ty = ly + logo_px + 90
        d.text((tx, ty), label, font=font, fill=GOLD)
        # subtle tagline, only if a regular weight exists too
        try:
            small = ImageFont.truetype(FONT_BOLD.replace("-Bold", ""), 40)
            sub = "MTN Broadband 5G ODU dashboard"
            sbox = d.textbbox((0, 0), sub, font=small)
            sw = sbox[2] - sbox[0]
            d.text(((PRESPLASH_W - sw) // 2 - sbox[0], ty + th + 42), sub,
                   font=small, fill=(150, 160, 175, 255))
        except Exception:
            pass
    return img


def main():
    os.makedirs(OUT, exist_ok=True)
    os.makedirs(ANDROID_OUT, exist_ok=True)

    # .ico keeps the classic multi-size set (format caps at 256px)
    sizes = [16, 24, 32, 48, 64, 128, 256]
    imgs = [draw(sz) for sz in sizes]
    ico = os.path.join(OUT, "icon.ico")
    imgs[-1].save(ico, format="ICO", sizes=[(s, s) for s in sizes],
                  append_images=imgs[:-1])

    # launcher/source icon at 1024px — written to BOTH locations:
    # repo root (desktop/electron) and android_app/ (buildozer cwd).
    big = draw(ICON_PX)
    root_png = os.path.join(OUT, "icon.png")
    android_png = os.path.join(ANDROID_OUT, "icon.png")
    big.save(root_png)
    big.save(android_png)

    presplash = os.path.join(ANDROID_OUT, "presplash.png")
    make_presplash().save(presplash)

    print("wrote", ico)
    print(f"wrote {root_png} ({ICON_PX}x{ICON_PX})")
    print(f"wrote {android_png} ({ICON_PX}x{ICON_PX})")
    print(f"wrote {presplash} ({PRESPLASH_W}x{PRESPLASH_H})")


if __name__ == "__main__":
    main()
