"""Generates DataBridge's web branding: loading splash (light + dark), favicon and app icons.

The mark is the Material "hub" glyph used as the logo inside the studio; the wordmark is Roboto Bold (the
studio's font). Run once after changing the design; the PNGs are committed in databridge/ui/branding/.

    pip install pillow font-roboto   # dev tools only; the app itself doesn't use Pillow
    python scripts/make_brand_assets.py --roboto /path/to/Roboto-Bold.ttf
"""

import argparse
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

OUT = Path(__file__).resolve().parents[1] / "databridge" / "ui" / "branding"
HUB = chr(0xF051D)  # hub_baseline in Flutter's MaterialIcons font
PRIMARY = (81, 91, 146)        # the studio's primary colour (indigo seed, light scheme)
PRIMARY_DARK = (186, 195, 255)  # primary in the dark scheme
TEXT = (27, 27, 33)
TEXT_DARK = (228, 225, 233)
SS = 4  # supersampling for smooth edges


def material_font(size: int) -> ImageFont.FreeTypeFont:
    import flet_web

    path = Path(flet_web.__file__).parent / "web" / "assets" / "fonts" / "MaterialIcons-Regular.otf"
    return ImageFont.truetype(str(path), size)


def _colored(mask: Image.Image, color) -> Image.Image:
    """Solid colour with the mask as alpha (no dark fringes when scaling, unlike drawing on transparent RGBA)."""
    img = Image.new("RGBA", mask.size, (*color, 255))
    img.putalpha(mask)
    return img


def glyph_mask(size: int, pad: float = 0.0) -> Image.Image:
    """The hub mark as an alpha mask, tightly fitted into a `size` px square."""
    big = size * SS
    font = material_font(int(big * 1.25))
    canvas = Image.new("L", (big * 2, big * 2), 0)
    ImageDraw.Draw(canvas).text((big // 2, big // 2), HUB, font=font, fill=255)
    canvas = canvas.crop(canvas.getbbox())
    inner = int(big * (1 - 2 * pad))
    canvas.thumbnail((inner, inner), Image.LANCZOS)
    out = Image.new("L", (big, big), 0)
    out.paste(canvas, ((big - canvas.width) // 2, (big - canvas.height) // 2))
    return out.resize((size, size), Image.LANCZOS)


def lockup(roboto: str, mark_color, text_color, height: int = 440) -> Image.Image:
    """Mark above the wordmark, for the loading screen (shown about 260 px wide)."""
    H = height * SS
    font = ImageFont.truetype(roboto, int(H * 0.25))
    box = ImageDraw.Draw(Image.new("L", (1, 1))).textbbox((0, 0), "DataBridge", font=font)
    tw, th = box[2] - box[0], box[3] - box[1]
    W = int(tw * 1.1)
    mark_size = int(H * 0.52)
    mark = Image.new("L", (W, H), 0)
    mark.paste(glyph_mask(mark_size // SS).resize((mark_size, mark_size), Image.LANCZOS),
               ((W - mark_size) // 2, int(H * 0.02)))
    text = Image.new("L", (W, H), 0)
    ImageDraw.Draw(text).text(((W - tw) // 2 - box[0], int(H * 0.64) - box[1] + int(H * 0.02)), "DataBridge",
                              font=font, fill=255)
    size = (W // SS, H // SS)
    img = Image.alpha_composite(_colored(mark.resize(size, Image.LANCZOS), mark_color),
                                _colored(text.resize(size, Image.LANCZOS), text_color))
    return img.crop(img.getbbox())


def tile(size: int, radius: float, pad: float, bg=PRIMARY, fg=(255, 255, 255), rgb: bool = False) -> Image.Image:
    """White mark on the primary colour: favicon and app icons (radius 0 = full bleed for maskable icons)."""
    big = size * SS
    shape = Image.new("L", (big, big), 0)
    ImageDraw.Draw(shape).rounded_rectangle((0, 0, big - 1, big - 1), radius=int(big * radius), fill=255)
    img = _colored(shape.resize((size, size), Image.LANCZOS), bg)
    img.alpha_composite(_colored(glyph_mask(size, pad=pad), fg))
    if rgb:  # apple-touch-icon: no transparency
        flat = Image.new("RGB", img.size, bg)
        flat.paste(img, (0, 0), img)
        return flat
    return img


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--roboto", required=True, help="path to Roboto-Bold.ttf")
    args = ap.parse_args()
    (OUT / "icons").mkdir(parents=True, exist_ok=True)
    lockup(args.roboto, PRIMARY, TEXT).save(OUT / "icons" / "loading-animation.png", optimize=True)
    lockup(args.roboto, PRIMARY_DARK, TEXT_DARK).save(OUT / "icons" / "loading-dark.png", optimize=True)
    tile(64, 0.22, 0.16).resize((32, 32), Image.LANCZOS).save(OUT / "favicon.png", optimize=True)
    tile(192, 0.22, 0.18).save(OUT / "icons" / "icon-192.png", optimize=True)
    tile(512, 0.22, 0.18).save(OUT / "icons" / "icon-512.png", optimize=True)
    tile(192, 0.0, 0.26).save(OUT / "icons" / "icon-maskable-192.png", optimize=True)  # safe zone for masks
    tile(512, 0.0, 0.26).save(OUT / "icons" / "icon-maskable-512.png", optimize=True)
    tile(192, 0.0, 0.2, rgb=True).save(OUT / "icons" / "apple-touch-icon-192.png", optimize=True)
    print("written to", OUT)


if __name__ == "__main__":
    main()
