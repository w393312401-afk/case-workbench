"""Build generic selector previews with the same renderer used for photo captions."""
from pathlib import Path
import sys

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from carousel_layout import fit_caption, paint_caption


def main():
    target = ROOT / "static" / "caption-styles"
    target.mkdir(parents=True, exist_ok=True)
    for style in ("yellow_impact", "orange_depth", "red_comic", "lime_pop"):
        canvas = Image.new("RGBA", (1024, 320), (0, 0, 0, 0))
        fit = fit_caption(1024, 320, ["YOUR TITLE", "GOES HERE"], style=style,
                          limit=1, scale=1700, margin=18)
        paint_caption(canvas, fit, max(0, (320 - fit["used"]) // 2), align="center")
        canvas.save(target / f"{style}.png", optimize=True)
        print(style)


if __name__ == "__main__":
    main()
