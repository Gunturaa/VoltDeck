#!/usr/bin/env python3
"""Generate Precision Dark VoltDeck PETIR (Engineer Lightning) SVG and PNG icons."""

from __future__ import annotations

from pathlib import Path
from PIL import Image, ImageDraw

ASSETS_DIR = Path(__file__).resolve().parent
ICONS_DIR = ASSETS_DIR / "icons"
SVG_PATH = ASSETS_DIR / "lanshare.svg"

SVG_CONTENT = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512" width="512" height="512">
  <defs>
    <linearGradient id="bg" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0%" stop-color="#141922"/>
      <stop offset="100%" stop-color="#090B0E"/>
    </linearGradient>
    <linearGradient id="bolt" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0%" stop-color="#67E8F9"/>
      <stop offset="55%" stop-color="#22D3EE"/>
      <stop offset="100%" stop-color="#06B6D4"/>
    </linearGradient>
  </defs>
  <!-- Precision Dark Squircle Base -->
  <rect x="24" y="24" width="464" height="464" rx="108" fill="url(#bg)"/>
  <rect x="26" y="26" width="460" height="460" rx="106" fill="none" stroke="#FFFFFF" stroke-opacity="0.14" stroke-width="4"/>
  <!-- Chamfered Engineer Reticle -->
  <path d="M148 76 H364 L436 148 V364 L364 436 H148 L76 364 V148 Z" fill="none" stroke="#FFFFFF" stroke-opacity="0.09" stroke-width="4"/>
  <circle cx="256" cy="256" r="154" fill="none" stroke="#22D3EE" stroke-opacity="0.18" stroke-width="4" stroke-dasharray="14 14"/>
  <!-- Modern Engineer PETIR (Lightning Bolt) -->
  <path d="M284 106 L160 278 H250 L226 406 L354 230 H264 Z" fill="url(#bolt)"/>
  <!-- Live Emerald Telemetry Node -->
  <circle cx="384" cy="384" r="18" fill="#34D399"/>
</svg>
"""


def render_png(size: int) -> Image.Image:
    """Render supersampled anti-aliased VoltDeck PETIR PNG icon with Pillow."""
    scale = 4
    s = size * scale
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    pad = int(s * 0.047)
    radius = int(s * 0.21)

    # Squircle base
    draw.rounded_rectangle([pad, pad, s - pad, s - pad], radius=radius, fill=(14, 17, 23, 255))
    # Hairline inner border
    border_w = max(2, int(s * 0.008))
    draw.rounded_rectangle(
        [pad + border_w, pad + border_w, s - pad - border_w, s - pad - border_w],
        radius=max(4, radius - border_w),
        outline=(255, 255, 255, 38),
        width=border_w,
    )

    # Subtle technical telemetry ring
    cx, cy = s / 2, s / 2
    r1 = s * 0.30
    draw.ellipse(
        [cx - r1, cy - r1, cx + r1, cy + r1],
        outline=(34, 211, 238, 46),
        width=max(2, int(s * 0.008)),
    )

    # Modern Geometric Engineer Lightning Bolt (PETIR)
    bolt_pts = [
        (s * 0.555, s * 0.207),
        (s * 0.312, s * 0.543),
        (s * 0.488, s * 0.543),
        (s * 0.441, s * 0.793),
        (s * 0.691, s * 0.449),
        (s * 0.516, s * 0.449),
    ]
    draw.polygon(bolt_pts, fill=(34, 211, 238, 255))

    # Crisp emerald live node dot
    dot_r = s * 0.036
    dot_x, dot_y = s * 0.75, s * 0.75
    draw.ellipse(
        [dot_x - dot_r, dot_y - dot_r, dot_x + dot_r, dot_y + dot_r],
        fill=(52, 211, 153, 255),
    )

    return img.resize((size, size), Image.Resampling.LANCZOS)


def main() -> None:
    ICONS_DIR.mkdir(parents=True, exist_ok=True)
    SVG_PATH.write_text(SVG_CONTENT, encoding="utf-8")
    for sz in (24, 48, 64, 128, 180, 256, 512):
        out = ICONS_DIR / f"lanshare-{sz}.png"
        render_png(sz).save(out, format="PNG", optimize=True)
    print(f"Generated VoltDeck PETIR SVG and PNG icons in {ICONS_DIR}")


if __name__ == "__main__":
    main()
