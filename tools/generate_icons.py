"""
Run once to generate extension icons.
Usage: python tools/generate_icons.py
Requires: pip install Pillow
"""
import sys
from pathlib import Path

try:
    from PIL import Image, ImageDraw
except ImportError:
    sys.exit("Run: pip install Pillow")

OUT = Path(__file__).parent.parent / "chrome_extension" / "icons"
OUT.mkdir(parents=True, exist_ok=True)

BG    = (10, 13, 20, 255)
RING  = (0, 212, 170, 255)
INNER = (15, 27, 48, 255)
CE    = (239, 83, 80, 255)
PE    = (0, 212, 170, 255)
MID   = (100, 140, 200, 220)


def make(size):
    img  = Image.new("RGBA", (size, size), BG)
    draw = ImageDraw.Draw(img)
    m = max(1, size // 16)
    draw.ellipse([m, m, size-m-1, size-m-1], outline=RING, width=max(1, size//12))
    p = size // 4
    draw.ellipse([p, p, size-p-1, size-p-1], fill=INNER)
    if size >= 24:
        bw  = max(1, size // 10)
        bot = size - size // 5
        g   = max(1, size // 20)
        hs  = [int(size*.25), int(size*.18), int(size*.32)]
        cs  = [CE, MID, PE]
        tw  = len(hs)*bw + (len(hs)-1)*g
        x0  = (size - tw) // 2
        for i, (h, c) in enumerate(zip(hs, cs)):
            x = x0 + i*(bw+g)
            draw.rectangle([x, bot-h, x+bw-1, bot], fill=c)
    return img


for sz in (16, 48, 128):
    path = OUT / f"icon{sz}.png"
    make(sz).save(path, "PNG")
    print(f"  ✓ {path}")

print("\nDone. Load extension: chrome://extensions/ → Developer mode → Load unpacked")
