"""Page layout: cells of text with a page-relative bbox and a recognition confidence.

A cell is a run of text a person would read as one value: one table cell, one label, one
value. Engines, in the order the pipeline tries them per page:

- text_layer: the PDF's own text (born-digital pages). Exact; confidence 1.0.
- docling:    Docling layout + table structure (optional extra `ocr`).
- paddleocr:  PaddleOCR on the 300 dpi render (optional extra `ocr`); per-line confidence.
- vlm:        a vision-language model transcribes hard pages (handwriting, bad scans).

bbox is [x0, y0, x1, y1] as fractions of the page, origin top-left, as in source_ref.bbox.
"""

import math
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c


@dataclass
class Cell:
    text: str
    bbox: list[float]
    conf: float = 1.0

    @property
    def xc(self) -> float:
        return (self.bbox[0] + self.bbox[2]) / 2

    @property
    def yc(self) -> float:
        return (self.bbox[1] + self.bbox[3]) / 2


@dataclass
class PageLayout:
    page: int  # 1-based
    cells: list[Cell]
    engine: str
    has_text_layer: bool
    image: str | None = None  # path of the 300 dpi render, if made
    angle: float = 0.0  # deskew applied before OCR; cells are in deskewed coordinates
    notes: list[str] = field(default_factory=list)

    @property
    def mean_conf(self) -> float:
        return sum(c.conf for c in self.cells) / len(self.cells) if self.cells else 0.0

    def rows(self, tol: float = 0.004) -> list[list[Cell]]:
        """Cells grouped into visual rows (similar vertical centre), each sorted left to right."""
        out: list[list[Cell]] = []
        for c in sorted(self.cells, key=lambda c: (c.yc, c.bbox[0])):
            if out and abs(out[-1][0].yc - c.yc) <= max(tol, (c.bbox[3] - c.bbox[1]) * 0.45):
                out[-1].append(c)
            else:
                out.append([c])
        return [sorted(r, key=lambda c: c.bbox[0]) for r in out]

    def text(self) -> str:
        return "\n".join(" ".join(c.text for c in r) for r in self.rows())


def text_layer(pdf_path: Path | str) -> list[PageLayout]:
    """Cells from the PDF text layer. Characters are joined into a cell until a horizontal gap
    wider than ~1.6 average character widths, a line change, or the end of a text run."""
    pdf = pdfium.PdfDocument(str(pdf_path))
    pages = []
    for i in range(len(pdf)):
        page = pdf[i]
        w, h = page.get_size()
        tp = page.get_textpage()
        text = tp.get_text_range()
        chars = []
        rotated = 0
        for k in range(tp.count_chars()):
            ch = text[k] if k < len(text) else ""
            angle = pdfium_c.FPDFText_GetCharAngle(tp.raw, k) % (2 * math.pi)
            if ch.strip() and min(angle, 2 * math.pi - angle) > 0.05:
                rotated += 1  # watermarks, stamps, vertical labels: not table/label text
                continue
            left, bottom, right, top = tp.get_charbox(k, loose=True)
            chars.append((ch, left, bottom, right, top))
        cells = [c for c in _cells(chars, w, h) if not is_watermark(c.text)]
        notes = [f"skipped {rotated} rotated glyphs"] if rotated else []
        pages.append(PageLayout(i + 1, cells, "text_layer", bool(cells), notes=notes))
    pdf.close()
    return pages


def is_watermark(text: str) -> bool:
    """Our own SYNTHETIC marking (watermark word and footer) is not document content."""
    t = text.strip().upper()
    return t == "SYNTHETIC" or t.startswith("SYNTHETIC DATA -")


def _cells(
    chars: list[tuple[str, float, float, float, float]], w: float, h: float
) -> Iterable[Cell]:
    buf: list[tuple[str, float, float, float, float]] = []

    def flush() -> Cell | None:
        glyphs = [c for c in buf if c[0].strip()]
        buf.clear()
        if not glyphs:
            return None
        text = "".join(c[0] for c in _with_spaces(glyphs)).strip()
        x0 = min(c[1] for c in glyphs)
        x1 = max(c[3] for c in glyphs)
        y1 = max(c[4] for c in glyphs)
        y0 = min(c[2] for c in glyphs)
        return Cell(
            text, [round(x0 / w, 4), round(1 - y1 / h, 4), round(x1 / w, 4), round(1 - y0 / h, 4)]
        )

    for ch in chars:
        if ch[0] in ("\r", "\n"):
            if (c := flush()) is not None:
                yield c
            continue
        if buf:
            prev = next((b for b in reversed(buf) if b[0].strip()), None)
            if prev is not None and ch[0].strip():
                height = max(prev[4] - prev[2], 1.0)
                gap = ch[1] - prev[3]
                new_line = abs((ch[2] + ch[4]) / 2 - (prev[2] + prev[4]) / 2) > height * 0.6
                if new_line or gap > 1.6 * height * 0.55 or gap < -height:
                    if (c := flush()) is not None:
                        yield c
        buf.append(ch)
    if (c := flush()) is not None:
        yield c


def _with_spaces(
    glyphs: list[tuple[str, float, float, float, float]],
) -> list[tuple[str, float, float, float, float]]:
    """Re-insert a single space where glyphs are separated by a word gap inside one cell."""
    out = [glyphs[0]]
    for a, b in zip(glyphs, glyphs[1:], strict=False):
        height = max(a[4] - a[2], 1.0)
        if b[1] - a[3] > height * 0.18:
            out.append((" ", a[3], a[2], b[1], a[4]))
        out.append(b)
    return out


def render_pages(pdf_path: Path | str, out_dir: Path, dpi: int = 300) -> list[Path]:
    """Render every page to PNG at `dpi` (cached: existing files are reused)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    pdf = pdfium.PdfDocument(str(pdf_path))
    paths = []
    for i in range(len(pdf)):
        p = out_dir / f"p{i + 1:03d}_{dpi}.png"
        if not p.exists():
            pdf[i].render(scale=dpi / 72, grayscale=True).to_pil().save(p)
        paths.append(p)
    pdf.close()
    return paths


def deskew(image: Path, max_angle: float = 4.0) -> tuple[Path, float]:
    """Rotate a scanned page upright. Returns (deskewed image path, angle applied, degrees
    counter-clockwise). The angle maximises the variance of row ink (text lines line up)."""
    from PIL import Image, ImageOps

    img = Image.open(image).convert("L")
    small = ImageOps.invert(img.resize((600, int(600 * img.height / img.width))))

    def sharpness(a: float) -> float:
        rows = list(small.rotate(a, resample=Image.Resampling.BILINEAR).resize(
            (1, small.height), Image.Resampling.BOX).getdata())  # fmt: skip
        mean = sum(rows) / len(rows)
        return float(sum((r - mean) ** 2 for r in rows))

    best = max((a / 4 for a in range(int(-max_angle * 4), int(max_angle * 4) + 1)), key=sharpness)
    best = max((best + d / 20 for d in range(-5, 6)), key=sharpness)
    out = image.with_name(image.stem + "_deskew.png")
    if abs(best) < 0.05:
        return image, 0.0
    if not out.exists():
        img.rotate(best, resample=Image.Resampling.BICUBIC, fillcolor=255).save(out)
    return out, round(best, 2)


def rotate_bbox(bbox: list[float], angle: float, aspect: float = 297 / 210) -> list[float]:
    """Axis-aligned box around `bbox` after Image.rotate(angle) about the page centre.
    Coordinates are page fractions; `aspect` is height / width (A4 by default)."""
    t = math.radians(angle)
    xs, ys = [], []
    for x, y in ((bbox[0], bbox[1]), (bbox[2], bbox[1]), (bbox[0], bbox[3]), (bbox[2], bbox[3])):
        dx, dy = x - 0.5, (y - 0.5) * aspect
        rx, ry = dx * math.cos(t) + dy * math.sin(t), -dx * math.sin(t) + dy * math.cos(t)
        xs.append(0.5 + rx)
        ys.append(0.5 + ry / aspect)
    return [round(min(1.0, max(0.0, v)), 4) for v in (min(xs), min(ys), max(xs), max(ys))]
