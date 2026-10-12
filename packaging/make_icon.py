"""Draw the app icon (a person sitting up straight) and write packaging/PostureGuard.icns.

    --png  also write docs/icon.png for the README
"""

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent
SIZE = 1024
SCALE = 4  # draw large, then downsample for smooth edges


def draw():
    s = SIZE * SCALE
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))

    # Rounded square with a top-to-bottom teal gradient, inset like macOS app icons.
    inset, radius = int(s * 0.1), int(s * 0.18)
    gradient = Image.new("RGBA", (s, s))
    top, bottom = (45, 196, 160), (17, 122, 128)
    gd = ImageDraw.Draw(gradient)
    for y in range(s):
        t = y / s
        gd.line([(0, y), (s, y)], fill=tuple(round(a + (b - a) * t) for a, b in zip(top, bottom)) + (255,))
    mask = Image.new("L", (s, s), 0)
    ImageDraw.Draw(mask).rounded_rectangle([inset, inset, s - inset, s - inset], radius, fill=255)
    img.paste(gradient, (0, 0), mask)

    d = ImageDraw.Draw(img)
    white, w = (255, 255, 255, 255), int(s * 0.055)
    u = lambda x, y: (int(s * x), int(s * y))  # noqa: E731

    # Head, an upright spine, the arm reaching to a desk, and the chair.
    hx, hy, hr = 0.44, 0.27, 0.065
    d.ellipse([u(hx - hr, hy - hr), u(hx + hr, hy + hr)], fill=white)
    d.line([u(0.44, 0.38), u(0.44, 0.60)], fill=white, width=w)                     # spine
    d.line([u(0.44, 0.60), u(0.62, 0.60), u(0.62, 0.76)], fill=white, width=w, joint="curve")  # thigh + shin
    d.line([u(0.44, 0.44), u(0.58, 0.52), u(0.72, 0.50)], fill=white, width=w, joint="curve")  # arm
    d.line([u(0.70, 0.48), u(0.80, 0.48)], fill=white, width=int(w * 0.7))            # desk
    d.line([u(0.33, 0.36), u(0.33, 0.68), u(0.50, 0.68)], fill=(255, 255, 255, 150),
           width=int(w * 0.6), joint="curve")                                         # chair
    for x, y in (u(0.44, 0.38), u(0.44, 0.60), u(0.62, 0.60), u(0.44, 0.44), u(0.58, 0.52)):
        d.ellipse([x - w // 2, y - w // 2, x + w // 2, y + w // 2], fill=white)       # round joints

    return img.resize((SIZE, SIZE), Image.LANCZOS)


def main():
    icon = draw()
    out = HERE / "PostureGuard.icns"
    with tempfile.TemporaryDirectory() as tmp:
        iconset = Path(tmp) / "PostureGuard.iconset"
        iconset.mkdir()
        for px in (16, 32, 128, 256, 512):
            icon.resize((px, px), Image.LANCZOS).save(iconset / f"icon_{px}x{px}.png")
            icon.resize((px * 2, px * 2), Image.LANCZOS).save(iconset / f"icon_{px}x{px}@2x.png")
        subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(out)], check=True)
    if "--png" in sys.argv:  # the small icon at the top of the README
        icon.resize((256, 256), Image.LANCZOS).save(HERE.parent / "docs" / "icon.png")
    print(f"Wrote {out}")


if __name__ == "__main__":
    if not shutil.which("iconutil"):
        sys.exit("iconutil not found (it ships with macOS)")
    main()
