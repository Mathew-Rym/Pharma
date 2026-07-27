#!/usr/bin/env python3
"""Generate every raster form of the Pharma OS mark from one geometry definition.

Run:  .venv/bin/python brand/make_assets.py

Why Pillow and not an SVG rasteriser: cairosvg needs cairo + pango system libraries,
which is exactly the dependency that eats an hour on a first Railway deploy. The mark
is four primitives, so drawing it directly keeps asset generation dependency-free and
guarantees the PNGs match logo.svg because both come from the numbers below.
"""
from pathlib import Path

from PIL import Image, ImageDraw

ORANGE = (255, 122, 0, 255)
OUT = Path(__file__).parent

# Geometry on a 512x512 canvas — identical to logo.svg.
HEADS = [(200, 118, 42), (312, 118, 42)]      # cx, cy, r
BODY = (168, 182, 344, 414)                   # x0, y0, x1, y1
NOTCH = (238, 220, 274, 414)                  # the gap between the legs
NOTCH_R = 12


def render(size: int, pad: float = 0.0, bg: tuple | None = None) -> Image.Image:
    """Draw the mark at `size`.

    pad shrinks the mark inside the canvas. WhatsApp and Apple crop avatars to a
    circle, so the mark needs breathing room or the heads get clipped.
    """
    SS = 8                                     # supersample, then downscale for AA
    c = size * SS
    img = Image.new("RGBA", (c, c), bg or (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    scale = (c / 512) * (1 - pad * 2)
    off = c * pad

    def X(v):
        return off + v * scale

    for cx, cy, r in HEADS:
        d.ellipse([X(cx - r), X(cy - r), X(cx + r), X(cy + r)], fill=ORANGE)

    d.rectangle([X(BODY[0]), X(BODY[1]), X(BODY[2]), X(BODY[3])], fill=ORANGE)

    # Punch the notch as fully transparent pixels, not white — a white notch becomes a
    # visible white bar on any dark background.
    nx0, ny0, nx1, ny1 = (X(v) for v in NOTCH)
    notch = Image.new("RGBA", (c, c), (0, 0, 0, 0))
    nd = ImageDraw.Draw(notch)
    nd.rounded_rectangle([nx0, ny0, nx1, ny1 + 40], radius=NOTCH_R * scale,
                         fill=(0, 0, 0, 255))
    # paste transparency through the notch mask
    img.paste((0, 0, 0, 0), (0, 0), notch)

    return img.resize((size, size), Image.LANCZOS)


def main() -> None:
    made = []

    # Dashboard / browser
    for n in (16, 32, 48, 192, 512):
        p = OUT / f"icon-{n}.png"
        render(n).save(p)
        made.append(p.name)

    # .ico with the sizes Windows and browsers actually ask for
    ico = OUT / "favicon.ico"
    render(256).save(ico, sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (256, 256)])
    made.append(ico.name)

    # Apple touch icon: no alpha channel support in practice, so flatten onto white
    p = OUT / "apple-touch-icon-180.png"
    render(180, pad=0.10, bg=(255, 255, 255, 255)).save(p)
    made.append(p.name)

    # WhatsApp profile picture. Cropped to a circle by WhatsApp, hence the padding.
    # Must be uploaded by hand -- GOWA's /user/avatar only READS avatars.
    p = OUT / "whatsapp-profile-640.png"
    render(640, pad=0.14, bg=(255, 255, 255, 255)).save(p)
    made.append(p.name)

    # PDF marks. fpdf2 handles PNG alpha, so the header mark stays transparent.
    p = OUT / "pdf-mark-96.png"
    render(96).save(p)
    made.append(p.name)

    # Watermark: same mark at low opacity, sitting behind page content.
    wm = render(600)
    alpha = wm.getchannel("A").point(lambda a: int(a * 0.05))
    wm.putalpha(alpha)
    p = OUT / "pdf-watermark-600.png"
    wm.save(p)
    made.append(p.name)

    print(f"wrote {len(made)} asset(s) to {OUT}/")
    for m in made:
        print("  ", m)


if __name__ == "__main__":
    main()
