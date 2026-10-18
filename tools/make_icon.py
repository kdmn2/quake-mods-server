#!/usr/bin/env python3
"""Generate the raster icon assets from the same geometry as static/icon.svg.

There is no SVG rasterizer in this runtime, so the script draws the identical design directly with Pillow
(a documented deviation from rasterising the file; the geometry is simple enough for both to match closely).
Outputs are committed, so normal development does not need to run this.

    python tools/make_icon.py        # writes static/favicon.ico, static/icon-64.png, static/icon-180.png
"""
from pathlib import Path

from PIL import Image, ImageDraw

STATIC = Path(__file__).resolve().parents[1] / "static"
TILE = (25, 90, 200, 255)        # #195ac8
INK = (255, 255, 255, 255)
ACCENT = (255, 209, 92, 255)     # #ffd15c


def draw_large() -> Image.Image:
    """The design at 8x scale with round line caps via smooth arcs, then downscaled for supersampling."""
    S = 8
    size = 64 * S
    s = size / 64.0

    def P(v):                       # scale a coordinate
        return v * s

    im = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle([P(2), P(2), P(62), P(62)], radius=P(14), fill=TILE)

    # arrow (accent), drawn first so the crate overlaps its base cleanly
    d.line([P(32), P(39), P(32), P(19.5)], fill=ACCENT, width=round(6 * s), joint="curve")
    for x1, y1, x2 in ((24, 28, 32), (32, 19.5, 40)):
        d.line([P(x1), P(y1), P(32 if x1 == 24 else x2), P(28 if y1 == 28 else y1)], fill=ACCENT, width=round(6 * s))
    # diagonal strokes with round caps
    d.line([P(24), P(28), P(32), P(19.5)], fill=ACCENT, width=round(6 * s), joint="curve")
    d.line([P(32), P(19.5), P(40), P(28)], fill=ACCENT, width=round(6 * s), joint="curve")
    for cx, cy in ((24, 28), (32, 19.5), (40, 28)):     # round caps on the arrow head
        r = 3 * s
        d.ellipse([P(cx - 3), P(cy - 3), P(cx + 3), P(cy + 3)], fill=ACCENT)

    # crate: white outline "U" + shelf bar
    crate_w = round(5 * s)
    d.line([P(15), P(26), P(15), P(43)], fill=INK, width=crate_w, joint="curve")
    d.line([P(49), P(26), P(49), P(43)], fill=INK, width=crate_w, joint="curve")
    d.line([P(13.5), P(43), P(50.5), P(43)], fill=INK, width=crate_w, joint="curve")
    for cx, cy in ((15, 26), (49, 26), (15, 43), (49, 43)):
        r = 2.5 * s
        d.ellipse([P(cx - 2.5), P(cy - 2.5), P(cx + 2.5), P(cy + 2.5)], fill=INK)
    # shelf block
    d.rounded_rectangle([P(12.5), P(23), P(51.5), P(28.5)], radius=P(2.5), fill=INK)
    return im


def main() -> None:
    STATIC.mkdir(exist_ok=True)
    big = draw_large()
    for size, name in ((64, "icon-64.png"), (180, "icon-180.png")):
        big.resize((size, size), Image.LANCZOS).save(STATIC / name)
    big.resize((48, 48), Image.LANCZOS).save(STATIC / "favicon.ico", sizes=((16, 16), (32, 32), (48, 48)))
    print("written:", ", ".join(sorted(p.name for p in STATIC.iterdir())))


if __name__ == "__main__":
    main()
