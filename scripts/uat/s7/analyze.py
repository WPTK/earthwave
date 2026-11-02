"""S7: decide from pixels whether a window screenshot shows a sixel image or braille text in the map area.

    python analyze.py AUTO.png BRAILLE.png [--json OUT]

The map area is the middle of the window. Two numbers per picture, computed from the PNG only:
  painted   share of pixels in the area that are not the area's most common colour (the terminal background);
  colours   number of distinct colours in the area.
Braille text paints tiny dots on the background; a sixel image paints filled land and water. The caller compares the run
the app chose for itself (auto) with a control run forced to braille by the config file.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

from PIL import Image


def area_stats(path: Path) -> dict[str, object]:
    img = Image.open(path).convert("RGB")
    w, h = img.size
    box = (int(w * 0.05), int(h * 0.15), int(w * 0.95), int(h * 0.80))
    counts = Counter(img.crop(box).getdata())
    total = sum(counts.values())
    background, bg_n = counts.most_common(1)[0]
    return {
        "image": path.name,
        "size": f"{w}x{h}",
        "area_pixels": total,
        "background_rgb": list(background),
        "painted": round(1 - bg_n / total, 4),
        "colours": len(counts),
        "colours_over_1pct": sum(1 for n in counts.values() if n / total > 0.01),
    }


def verdict(auto: dict, braille: dict) -> dict:
    """Sixel drawn when the auto run paints a clearly larger share of the area than the braille control."""
    ratio = auto["painted"] / max(braille["painted"], 1e-6)
    more = auto["colours_over_1pct"] > braille["colours_over_1pct"]
    return {
        "painted_ratio_auto_over_braille": round(ratio, 2),
        "auto_more_colours_over_1pct": more,
        "pixels_look_like_image": ratio >= 2.0 and more,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("auto", type=Path)
    ap.add_argument("braille", type=Path)
    ap.add_argument("--json", type=Path)
    args = ap.parse_args()
    a, b = area_stats(args.auto), area_stats(args.braille)
    out = {"auto": a, "braille": b, **verdict(a, b)}
    text = json.dumps(out, indent=2)
    print(text)
    if args.json:
        args.json.write_text(text, encoding="utf-8")
    return 0 if out["pixels_look_like_image"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
