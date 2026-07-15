"""Generate assets/icon.ico + icon.png — blue rounded tile with a white
robot head (the Orchard RPA worker logo). Run once; outputs are committed."""
from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).parent.parent / "assets"
OUT.mkdir(exist_ok=True)
S = 256

# Vertical gradient #0d8ee9 → #005a9e
grad = Image.new("RGBA", (S, S))
gd = ImageDraw.Draw(grad)
for y in range(S):
    t = y / S
    gd.line([(0, y), (S, y)], fill=(
        int(0x0D + (0x00 - 0x0D) * t),
        int(0x8E + (0x5A - 0x8E) * t),
        int(0xE9 + (0x9E - 0xE9) * t), 255))

# Rounded-square mask
mask = Image.new("L", (S, S), 0)
ImageDraw.Draw(mask).rounded_rectangle([6, 6, S - 6, S - 6], radius=58, fill=255)
img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
img.paste(grad, (0, 0), mask)

d = ImageDraw.Draw(img)
W = (255, 255, 255, 255)
B = (0, 90, 158, 255)

# Antenna
d.line([(128, 92), (128, 58)], fill=W, width=10)
d.ellipse([116, 38, 140, 62], fill=W)
# Head
d.rounded_rectangle([62, 92, 194, 196], radius=26, fill=W)
# Ears
d.rounded_rectangle([42, 122, 60, 166], radius=8, fill=W)
d.rounded_rectangle([196, 122, 214, 166], radius=8, fill=W)
# Eyes
d.ellipse([88, 118, 116, 146], fill=B)
d.ellipse([140, 118, 168, 146], fill=B)
# Smile
d.rounded_rectangle([96, 160, 160, 174], radius=7, fill=B)

img.save(OUT / "icon.png")
img.save(OUT / "icon.ico",
         sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
print(f"Wrote {OUT / 'icon.ico'} and icon.png")
