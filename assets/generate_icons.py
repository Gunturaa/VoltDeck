#!/usr/bin/env python3
"""Generate multi-resolution PNG icons for VoltDeck from the master glass-bolt icon."""

from __future__ import annotations

from pathlib import Path
from PIL import Image, ImageDraw

ASSETS_DIR = Path(__file__).resolve().parent
ICONS_DIR = ASSETS_DIR / "icons"
MASTER_ICON = ASSETS_DIR / "voltdeck-icon-master.png"
FULL_ICON = ASSETS_DIR / "voltdeck-icon-full.png"
SVG_PATH = ASSETS_DIR / "lanshare.svg"

SVG_CONTENT = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512" width="512" height="512">
  <defs>
    <linearGradient id="glass" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0%" stop-color="#222B3E"/>
      <stop offset="50%" stop-color="#0A0D16"/>
      <stop offset="100%" stop-color="#0E1B3A"/>
    </linearGradient>
    <linearGradient id="bolt" x1="0.2" y1="0" x2="0.8" y2="1">
      <stop offset="0%" stop-color="#E0F2FE"/>
      <stop offset="45%" stop-color="#60A5FA"/>
      <stop offset="100%" stop-color="#2563EB"/>
    </linearGradient>
  </defs>
  <rect x="24" y="24" width="464" height="464" rx="112" fill="url(#glass)"/>
  <rect x="26" y="26" width="460" height="460" rx="110" fill="none" stroke="#93C5FD" stroke-opacity="0.34" stroke-width="5"/>
  <path d="M300 104 L158 282 H248 L212 408 L354 226 H264 Z" fill="url(#bolt)"/>
</svg>
"""


def _fallback_icon(size: int) -> Image.Image:
    s = size * 4
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    pad = int(s * 0.047)
    d.rounded_rectangle([pad, pad, s - pad, s - pad], radius=int(s * 0.22), fill=(11, 16, 30, 255))
    d.rounded_rectangle([pad, pad, s - pad, s - pad], radius=int(s * 0.22), outline=(147, 197, 253, 90), width=max(2, int(s * 0.01)))
    bolt = [(s * 0.58, s * 0.20), (s * 0.31, s * 0.55), (s * 0.49, s * 0.55), (s * 0.42, s * 0.80), (s * 0.69, s * 0.44), (s * 0.51, s * 0.44)]
    d.polygon(bolt, fill=(96, 165, 250, 255))
    return img.resize((size, size), Image.Resampling.LANCZOS)


def main() -> None:
    ICONS_DIR.mkdir(parents=True, exist_ok=True)
    SVG_PATH.write_text(SVG_CONTENT, encoding="utf-8")

    master = Image.open(MASTER_ICON).convert("RGBA") if MASTER_ICON.is_file() else None
    full = Image.open(FULL_ICON).convert("RGBA") if FULL_ICON.is_file() else master

    for sz in (24, 48, 64, 128, 180, 256, 512):
        out = ICONS_DIR / f"lanshare-{sz}.png"
        # Use full-bleed square for 180px Apple Touch Icon (iOS applies its own mask cleanly)
        src = full if (sz == 180 and full is not None) else master
        if src is not None:
            resized = src.resize((sz, sz), Image.Resampling.LANCZOS)
        else:
            resized = _fallback_icon(sz)
        resized.save(out, format="PNG", optimize=True)
    print(f"Generated VoltDeck icons in {ICONS_DIR}")


if __name__ == "__main__":
    main()
