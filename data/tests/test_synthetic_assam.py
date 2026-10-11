import json
import math
import random
import struct
import wave
from pathlib import Path

import pypdfium2 as pdfium
import pytest
from PIL import Image, ImageDraw

from synthetic_assam import render, voice
from synthetic_assam.gold import QUOTA, build_gold
from synthetic_assam.model import build_well, load_config

CFG = load_config()


@pytest.fixture(scope="module")
def wells() -> list:
    return [build_well(i, 1, CFG) for i in range(12)]


def test_days_sum_to_24h_and_events_are_in_the_timeline(wells: list) -> None:
    for w in wells:
        for d in w.days:
            assert abs(sum(a.hours for a in d.activities) - 24) < 1e-9
        npt_ids = {a.event_id for d in w.days for a in d.activities if a.event_id}
        assert npt_ids == {e.id for e in w.events}


def test_truth_is_consistent(wells: list) -> None:
    for w in wells:
        mds = [t.md for t in w.tops]
        assert mds == sorted(mds) and w.td_md >= w.td_tvd - 1e-6
        assert w.casing[0].shoe_md < w.casing[1].shoe_md < w.casing[2].shoe_md
        barail = next(t for t in w.tops if t.formation == "Barail")
        assert w.casing[1].shoe_md < barail.md
        for e in w.events:
            assert e.t_start is not None and e.t_end is not None
            assert (e.loss_bbl is not None) == (e.type == "loss")


def test_same_seed_same_well() -> None:
    assert build_well(3, 1, CFG) == build_well(3, 1, CFG)
    assert build_well(3, 1, CFG) != build_well(3, 2, CFG)


def test_rotate_bbox_follows_pixels() -> None:
    # Draw a black box, rotate the page like a scan, compare with the transformed bbox.
    w, h = 620, 877  # A4 aspect
    img = Image.new("L", (w, h), 255)
    box = [0.15, 0.2, 0.45, 0.26]
    ImageDraw.Draw(img).rectangle([box[0] * w, box[1] * h, box[2] * w, box[3] * h], fill=0)
    for angle in (-2.5, 3.0):
        rot = img.rotate(angle, resample=Image.Resampling.NEAREST, fillcolor=255)
        x0, y0, x1, y1 = Image.eval(rot, lambda v: 255 - v).getbbox()  # type: ignore[misc]
        got = render.rotate_bbox(box, angle)
        assert math.isclose(got[0], x0 / w, abs_tol=0.01) and math.isclose(
            got[3], y1 / h, abs_tol=0.01
        )


def test_render_marks_synthetic_and_records_fields(wells: list, tmp_path: Path) -> None:
    w = next(x for x in wells if x.events)
    fmt = render.Fmt(w, random.Random(0))
    doc = render.wcr(w, fmt, "SYNTHETIC; test")
    path = tmp_path / "wcr.pdf"
    path.write_bytes(doc.pdf())
    pdf = pdfium.PdfDocument(str(path))
    assert pdf.get_metadata_dict()["Subject"].startswith("SYNTHETIC")
    text = "".join(pdf[i].get_textpage().get_text_range() for i in range(len(pdf)))
    assert text.count("SYNTHETIC") >= len(pdf)  # watermark on every page
    names = {f.name for f in doc.fields}
    assert {"well_name", "latitude", "td_md", "tops[0].md", "events[0].type"} <= names
    for f in doc.fields:  # every printed string really is on its page
        assert f.printed.replace(",", "") in pdf[
            f.page - 1
        ].get_textpage().get_text_range().replace(",", "")

    pages = render.rasterize(render.wcr(w, fmt, "SYNTHETIC"), random.Random(1), True, "01.01.1990")
    render.save_images(pages, tmp_path / "fax.pdf", "t", "SYNTHETIC")
    assert (
        pdfium.PdfDocument(str(tmp_path / "fax.pdf")).get_metadata_dict()["Keywords"] == "SYNTHETIC"
    )


def test_field_note_without_fonts_falls_back(wells: list) -> None:
    w = next(x for x in wells if x.events)
    images, fields = render.field_note(
        w, w.events[0], render.Fmt(w, random.Random(0)), random.Random(0), []
    )
    assert len(images) == 1 and {f.name for f in fields} >= {"well_name", "depth_md", "event_type"}


def test_gold_set_is_stratified_and_complete() -> None:
    docs = [
        {
            "path": f"d{i}",
            "doc_type": t,
            "render": r,
            "well": "W",
            "pages": 1,
            "depth_unit": "m",
            "sha256": "x",
            "vintage": "2000-01-01",
            "fields": [{}],
        }
        for i, (t, r) in enumerate([k for k, n in QUOTA.items() for _ in range(n + 2)])
    ]
    for d in docs:
        d["render"] = d["render"] or "typed"
    gold = build_gold(docs, seed=1)
    assert gold["count"] == 50 and gold["synthetic"] is True
    assert sum(d["doc_type"] == "field_note" for d in gold["documents"]) == 12
    assert gold == build_gold(docs, seed=1)


def test_voice_note_script_metadata_and_duration(wells: list, tmp_path: Path, monkeypatch) -> None:
    w = next(x for x in wells if x.events)

    def fake_tts(cmd: list[str], **_: object) -> None:  # writes 1.5 s of tone like espeak-ng -w
        with wave.open(cmd[cmd.index("-w") + 1], "wb") as f:
            f.setnchannels(1), f.setsampwidth(2), f.setframerate(22050)
            f.writeframes(
                b"".join(struct.pack("<h", int(8000 * math.sin(i / 10))) for i in range(33075))
            )

    monkeypatch.setattr(voice, "espeak", lambda: "espeak-ng")
    monkeypatch.setattr(voice.subprocess, "run", fake_tts)
    notes = voice.voice_notes(w, random.Random(0), tmp_path)
    assert notes and notes[0]["duration_s"] == 1.5
    wav = Path(str(notes[0]["path"]))
    assert b"SYNTHETIC" in wav.read_bytes()[-400:]
    with wave.open(str(wav)) as f:  # still a valid WAV after adding LIST/INFO
        assert f.getnframes() == 33075
    transcript = wav.with_suffix(".txt").read_text()
    assert (
        transcript.startswith("This is a synthetic recording.")
        and w.name.replace("-", " ") in transcript
    )
    assert json.dumps(notes, default=str)


def test_rebuilt_documents_are_byte_identical(wells: list) -> None:
    w = wells[0]

    def build() -> tuple[bytes, bytes]:
        fmt = render.Fmt(w, random.Random(0))
        typed = render.ddr(w, w.days[0], fmt, "SYNTHETIC").pdf()
        pages = render.rasterize(
            render.ddr(w, w.days[0], fmt, "SYNTHETIC"), random.Random(2), False, "x"
        )
        return typed, b"".join(p.tobytes() for p in pages)

    assert build() == build()


def test_truth_loader_records_feet_to_metres_like_the_pipeline() -> None:
    from synthetic_assam.load_truth import converted, wcr_fields
    from synthetic_assam.model import build_well, load_config

    cfg = load_config()
    w = next(w for w in (build_well(i, 1, cfg) for i in range(30)) if w.depth_unit == "ft")
    _, fields, _ = wcr_fields(w, 1)
    (c,) = converted([("td_md", fields["td_md"])])
    assert c["from"] == "ft" and c["to"] == "m" and c["factor"] == 0.3048
    assert abs(float(c["printed"].replace(",", "")) * 0.3048 - c["value"]) < 0.001
    assert converted([("well_name", fields["well_name"])]) == []
