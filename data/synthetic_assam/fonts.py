"""Handwriting fonts: SIL Open Font License fonts from the Google Fonts repository, pinned to one
commit and checksummed via fetch.py. SYNTH_HANDWRITING_FONTS (paths separated by os.pathsep)
overrides the download, e.g. to use fonts already on the machine.
"""

import os
import sys
from pathlib import Path

from fetch import RAW, fetch

COMMIT = "9710da1eacb3be272583c3224dcb70f9da6eadbb"  # google/fonts, 2026-09-30
FONTS = [
    "ofl/reeniebeanie/ReenieBeanie.ttf",
    "ofl/indieflower/IndieFlower-Regular.ttf",
    "ofl/nothingyoucoulddo/NothingYouCouldDo.ttf",
    "ofl/shadowsintolight/ShadowsIntoLight.ttf",
]
BASE = f"https://raw.githubusercontent.com/google/fonts/{COMMIT}/"


def handwriting_fonts(download: bool = True) -> list[Path]:
    override = os.environ.get("SYNTH_HANDWRITING_FONTS")
    if override:
        return [Path(p) for p in override.split(os.pathsep) if p]
    if not download:
        return []
    out = []
    for rel in FONTS:
        dest = RAW / "fonts" / Path(rel).name
        try:
            fetch(BASE + rel, dest)
            out.append(dest)
        except OSError as e:
            print(f"  warning: handwriting font {rel} unavailable ({e})", file=sys.stderr)
    if not out:
        print(
            "  warning: no handwriting fonts; field notes fall back to Pillow's default font",
            file=sys.stderr,
        )
    return out
