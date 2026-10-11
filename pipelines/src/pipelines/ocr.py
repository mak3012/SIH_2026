"""Optional layout/OCR engines. Each returns PageLayout objects in the common cell model.

Docling and PaddleOCR come with the `ocr` extra (`uv sync --extra ocr`); both download their
models on first use. Missing engines are reported, not faked: a page no engine can read goes to
the review queue as a whole.
"""

import base64
import json
import logging
import os
from functools import cache
from pathlib import Path
from typing import Any

from pipelines.layout import Cell, PageLayout

log = logging.getLogger(__name__)


def available(engine: str) -> bool:
    try:
        if engine == "docling":
            import docling.document_converter  # noqa: F401
        elif engine == "paddleocr":
            import paddleocr  # noqa: F401
        else:
            return False
    except ImportError:
        return False
    return True


@cache
def _docling() -> Any:
    from docling.document_converter import DocumentConverter

    return DocumentConverter()


def _docling_cells(doc: Any) -> dict[int, list[Cell]]:
    cells: dict[int, list[Cell]] = {}

    def add(page_no: int, text: str, bbox: Any) -> None:
        size = doc.pages[page_no].size
        box = bbox.to_top_left_origin(page_height=size.height)
        cells.setdefault(page_no, []).append(
            Cell(
                text.strip(),
                [
                    round(box.l / size.width, 4),
                    round(box.t / size.height, 4),
                    round(box.r / size.width, 4),
                    round(box.b / size.height, 4),
                ],
                DOCLING_CONF,
            )
        )

    for item, _level in doc.iterate_items():
        prov = getattr(item, "prov", None) or []
        if not prov:
            continue
        table = getattr(item, "data", None)
        if table is not None and getattr(table, "table_cells", None):
            for tc in table.table_cells:
                if tc.bbox is not None and tc.text.strip():
                    add(prov[0].page_no, tc.text, tc.bbox)
        elif getattr(item, "text", "").strip():
            add(prov[0].page_no, item.text, prov[0].bbox)
    return cells


# Docling does not expose a per-item OCR confidence; OCR text gets this fixed value.

DOCLING_CONF = float(os.environ.get("DOCLING_CONF", "0.9"))


def docling_layout(pdf_path: Path) -> list[PageLayout]:
    """Docling on a whole PDF (layout + table structure, OCR where there is no text)."""
    doc = _docling().convert(str(pdf_path)).document
    cells = _docling_cells(doc)
    return [PageLayout(p, cells.get(p, []), "docling", False) for p in sorted(doc.pages)]


def docling_image(image: Path, page: int) -> PageLayout:
    """Docling on one (deskewed) page image."""
    doc = _docling().convert(str(image)).document
    cells = _docling_cells(doc)
    return PageLayout(page, cells.get(1, []), "docling", False, str(image))


@cache
def _paddle() -> Any:
    from paddleocr import PaddleOCR

    return PaddleOCR(
        lang="en",
        # Mobile detector and a bounded input size: the server models on a 300 dpi page take
        # minutes per page on CPU. Override with PADDLE_DET / PADDLE_MAX_SIDE.
        text_detection_model_name=os.environ.get("PADDLE_DET", "PP-OCRv5_mobile_det"),
        text_det_limit_type="max",
        text_det_limit_side_len=int(os.environ.get("PADDLE_MAX_SIDE", "2000")),
        use_doc_orientation_classify=False,
        use_doc_unwarping=False,
        use_textline_orientation=True,
        # oneDNN kernels fail in PaddlePaddle 3.x on some CPUs ("ConvertPirAttribute2Runtime
        # Attribute not support"); plain CPU kernels are slower but work everywhere.
        enable_mkldnn=os.environ.get("PADDLE_MKLDNN", "0") == "1",
    )


def paddle_layout(image: Path, page: int) -> PageLayout:
    """PaddleOCR text lines on one rendered page, with per-line recognition confidence."""
    from PIL import Image

    width, height = Image.open(image).size
    cells = []
    for res in _paddle().predict(str(image)):
        data = res.json.get("res", res) if hasattr(res, "json") else res
        for text, score, box in zip(
            data["rec_texts"], data["rec_scores"], data["rec_boxes"], strict=False
        ):
            x0, y0, x1, y1 = (float(v) for v in box)
            cells.append(
                Cell(
                    str(text),
                    [
                        round(x0 / width, 4),
                        round(y0 / height, 4),
                        round(x1 / width, 4),
                        round(y1 / height, 4),
                    ],
                    float(score),
                )
            )
    return PageLayout(page, cells, "paddleocr", False, str(image))


def vlm_layout(image: Path, page: int, llm: Any, conf: float, **log_ctx: Any) -> PageLayout:
    """A vision-language model transcribes a hard page line by line, with approximate boxes on a
    0-1000 grid (the convention Qwen2.5-VL and similar models are trained on)."""
    schema = {
        "type": "object",
        "properties": {
            "lines": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string"},
                        "bbox": {
                            "type": "array",
                            "items": {"type": "number"},
                            "minItems": 4,
                            "maxItems": 4,
                        },
                    },
                    "required": ["text", "bbox"],
                },
            }
        },
        "required": ["lines"],
    }
    image_b64 = base64.b64encode(image.read_bytes()).decode()
    out = llm.complete_json(
        "transcribe_page", {}, schema, images=[image_b64], vision=True, **log_ctx
    )
    cells = []
    for line in (out or {}).get("lines", []):
        text = str(line.get("text", "")).strip()
        box = line.get("bbox") or [0, 0, 1000, 1000]
        if text:
            x0, y0, x1, y1 = (min(1.0, max(0.0, float(v) / 1000)) for v in box[:4])
            cells.append(Cell(text, [round(x0, 4), round(y0, 4), round(x1, 4), round(y1, 4)], conf))
    return PageLayout(
        page, cells, "vlm", False, str(image), notes=[json.dumps({"vlm_lines": len(cells)})]
    )
