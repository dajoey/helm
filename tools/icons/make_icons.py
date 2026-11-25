"""Helm's installed-app icons that the SNES art pipeline does not draw. Run once; commit the output.

    uv venv /tmp/iconvenv && uv pip install --python /tmp/iconvenv/bin/python pillow
    /tmp/iconvenv/bin/python tools/icons/make_icons.py

The wheel is web/snes/icon-32.png (helm-web-snes-src build_assets.py helm_icon(32): a ship's wheel
in SNES gold and wood on night-blue stripes), and web/icon-192.png / icon-512.png are that art
scaled 6x and 16x. Those two fill the square to the edge, which suits purpose "any". A launcher
that masks icons (mobile's circle, a squircle) keeps only the middle 80%, so a maskable icon
needs the wheel inside that circle: this draws the wheel again at a smaller whole-number scale,
centred on the same stripes carried out to the edge, and refuses to write one whose wheel
crosses the safe circle. Whole-number scales only, so every art pixel stays a crisp square.

    web/icon-maskable-192.png   wheel at 4x on 192: its farthest pixel 67 px out, the safe circle 76.8
    web/icon-maskable-512.png   wheel at 11x on 512: 186 px out of 204.8 (12x reached 203, too tight)
    web/apple-touch-icon.png    wheel at 5x on 180, opaque (iOS rounds the corners itself)

These icons are the app's own, byte for byte: its ui/make_icons.py drew
them (the lever, cyan on #1d2030).
"""
from __future__ import annotations

import math
import os
import sys

from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
WEB = os.path.join(ROOT, "web")
ART = os.path.join(WEB, "snes", "icon-32.png")

# build_assets.py helm_icon(): the stripes run from SKY_TOP to SKY_BOT in bands of 4 art pixels.
SKY_TOP, SKY_BOT = (16, 20, 72), (72, 56, 136)
BAND = 4
# Everything drawn over the stripes: the outline, wood, gold, and the one star the wheel left.
WHEEL = {(24, 12, 4), (88, 40, 8), (168, 88, 24), (232, 152, 56), (232, 168, 0), (248, 224, 96), (248, 248, 200)}

OUT = (("icon-maskable-192.png", 192, 4, True), ("icon-maskable-512.png", 512, 11, True), ("apple-touch-icon.png", 180, 5, False))


def stripes(size: int, scale: int) -> Image.Image:
    """The night sky carried to the edge: bands of BAND art pixels at this scale, top to bottom."""
    im = Image.new("RGBA", (size, size))
    px = im.load()
    band = BAND * scale
    for y in range(size):
        t = (y // band) * band / size
        col = tuple(int(a * (1 - t) + b * t) for a, b in zip(SKY_TOP, SKY_BOT))
        for x in range(size):
            px[x, y] = (*col, 255)
    return im


def draw(size: int, scale: int, maskable: bool) -> Image.Image:
    art = Image.open(ART).convert("RGBA")
    n = art.width
    apx = art.load()
    im = stripes(size, scale)
    px = im.load()
    off = (size - n * scale) // 2
    c = size / 2
    reach = 0.0
    for ay in range(n):
        for ax in range(n):
            col = apx[ax, ay]
            if col[:3] not in WHEEL:
                continue
            for dy in range(scale):
                for dx in range(scale):
                    x, y = off + ax * scale + dx, off + ay * scale + dy
                    px[x, y] = col
                    reach = max(reach, math.hypot(x + 0.5 - c, y + 0.5 - c))
    if maskable and reach > 0.4 * size:
        raise SystemExit("maskable %d: the wheel reaches %.1f px, past the %.1f px safe circle" % (size, reach, 0.4 * size))
    draw.reach = reach
    return im


def main() -> int:
    for name, size, scale, maskable in OUT:
        path = os.path.join(WEB, name)
        draw(size, scale, maskable).save(path, optimize=True)
        print("%7d bytes  web/%s  (wheel reach %.1f px, safe radius %.1f)" % (os.path.getsize(path), name, draw.reach, 0.4 * size))
    return 0


if __name__ == "__main__":
    sys.exit(main())
