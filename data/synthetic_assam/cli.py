"""Generate synthetic Upper Assam-style wells, their documents, voice notes and the gold set.

Usage (from data/): python -m synthetic_assam [--wells 30] [--seed 1] [--config FILE]
                    [--no-audio] [--no-download] [--jobs N]

Output: data/raw/synthetic_assam/
  wells/<well>/truth.json        full ground truth (canonical units)
  wells/<well>/*.pdf             rendered documents
  wells/<well>/audio/*.wav|.txt  voice notes and transcripts
  documents.json                 every document with field-level truth
  gold/gold_set.json             50-document evaluation set
  register.json                  what register.py loads into the object store + Postgres
Wells whose inputs have not changed are reused, so reruns are cheap.
"""

import argparse
import hashlib
import json
import os
import random
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict
from pathlib import Path
from typing import Any

from fetch import RAW, sha256_of, write_register
from synthetic_assam import render
from synthetic_assam.fonts import handwriting_fonts
from synthetic_assam.gold import build_gold
from synthetic_assam.model import DEFAULT_CONFIG, Well, build_well, load_config
from synthetic_assam.voice import espeak, voice_notes

OUT = RAW / "synthetic_assam"
VERSION = "3"  # bump when rendering changes, to invalidate reused wells
DOC_TYPE_DB = {"wcr": "wcr", "ddr": "ddr", "survey_report": "survey_report", "field_note": "other"}


def style(rng: random.Random, year: int) -> str:
    r = rng.random()
    if year < 1995:
        return "typed" if r < 0.1 else "fax" if r < 0.5 else "scanned"
    if year < 2008:
        return "typed" if r < 0.4 else "scanned"
    return "typed" if r < 0.8 else "scanned"


def emit(
    doc: render.TypedDoc,
    path: Path,
    render_style: str,
    rng: random.Random,
    title: str,
    keywords: str,
    stamp: str,
) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    if render_style == "typed":
        path.write_bytes(doc.pdf())
        return doc.page
    pages = render.rasterize(doc, rng, render_style == "fax", stamp)
    render.save_images(pages, path, title, keywords)
    return len(pages)


def render_well(
    index: int, seed: int, cfg_path: str, fonts: list[str], audio: bool, key: str
) -> list[dict[str, Any]]:
    cfg = load_config(Path(cfg_path))
    well = build_well(index, seed, cfg)
    wdir = OUT / "wells" / well.name
    cached = wdir / "records.json"
    if cached.exists():
        saved = json.loads(cached.read_text())
        if saved["key"] == key and all((RAW / r["path"]).exists() for r in saved["records"]):
            print(f"  ok      {well.name} (unchanged)")
            return list(saved["records"])

    rng = random.Random(seed * 7919 + index)
    fmt = render.Fmt(well, rng)
    kw = f"SYNTHETIC; synthetic_assam; seed={seed}; well={well.name}; not real data"
    stamp_date = fmt.date(well.completed.date())
    records: list[dict[str, Any]] = []

    def record(
        path: Path,
        doc_type: str,
        rstyle: str,
        title: str,
        vintage: str,
        pages: int,
        fields: list[render.Field],
    ) -> None:
        records.append(
            {
                "path": path.relative_to(RAW).as_posix(),
                "kind": "document",
                "doc_type": doc_type,
                "render": rstyle,
                "well": well.name,
                "title": title,
                "vintage": vintage,
                "pages": pages,
                "depth_unit": well.depth_unit,
                "sha256": sha256_of(path),
                "fields": render.fields_json(fields),
            }
        )

    wstyle = style(rng, well.spud.year)
    completed = well.completed.date().isoformat()
    for doc_type, builder, suffix in (
        ("wcr", render.wcr, "WCR"),
        ("survey_report", render.survey_report, "SURVEY"),
    ):
        title = f"{well.name} {suffix} (SYNTHETIC)"
        doc = builder(well, fmt, kw)
        path = wdir / f"{well.name}_{suffix}.pdf"
        pages = emit(doc, path, wstyle, rng, title, kw, stamp_date)
        record(path, doc_type, wstyle, title, completed, pages, doc.fields)

    event_days = {d.no for d in well.days if d.event_ids}
    others = [d.no for d in well.days if d.no not in event_days and d.no != 1]
    chosen = sorted(event_days | {1} | set(rng.sample(others, min(1, len(others)))))
    for day in (d for d in well.days if d.no in chosen):
        title = f"{well.name} DDR {day.no} (SYNTHETIC)"
        dstyle = style(rng, well.spud.year)
        doc = render.ddr(well, day, fmt, kw)
        path = wdir / f"{well.name}_DDR_{day.no:03d}.pdf"
        pages = emit(doc, path, dstyle, rng, title, kw, fmt.date(day.date))
        record(path, "ddr", dstyle, title, day.date.isoformat(), pages, doc.fields)

    font_paths = [Path(f) for f in fonts]
    for e in well.events:
        title = f"{well.name} field note {e.id} (SYNTHETIC)"
        images, fields = render.field_note(well, e, fmt, rng, font_paths)
        path = wdir / f"{well.name}_NOTE_{e.id.split('-')[-1]}.pdf"
        render.save_images(images, path, title, kw)
        assert e.t_start is not None
        record(path, "field_note", "handwritten", title, e.t_start.date().isoformat(), 1, fields)

    if audio:
        for n in voice_notes(well, rng, wdir / "audio"):
            wav = Path(str(n.pop("path")))
            records.append(
                {
                    "path": wav.relative_to(RAW).as_posix(),
                    "kind": "audio",
                    "well": well.name,
                    "title": f"{well.name} voice note {n['event_id']} (SYNTHETIC)",
                    "sha256": sha256_of(wav),
                    **n,
                }
            )

    truth = {
        "synthetic": True,
        "generator": f"synthetic_assam v{VERSION}",
        "seed": seed,
        "stratigraphy_status": cfg["basin"]["status"],
        "confirmed_by": cfg["basin"]["confirmed_by"],
        "well": asdict(well),
    }
    (wdir / "truth.json").write_text(json.dumps(truth, indent=1, default=str) + "\n")
    cached.write_text(json.dumps({"key": key, "records": records}) + "\n")
    print(f"  built   {well.name}: {len(records)} files, {len(well.events)} events")
    return records


def register_items(
    records: list[dict[str, Any]], wells: dict[str, Well]
) -> list[dict[str, object]]:
    items: list[dict[str, object]] = []
    for r in records:
        w = wells[r["well"]]
        base = {
            "path": r["path"],
            "kind": r["kind"],
            "title": r["title"],
            "source": "synthetic_assam",
            "synthetic": True,
            "classification": "internal",
            "block_id": w.block,
            "block_name": f"{w.block} (synthetic)",
        }
        if r["kind"] == "document":
            items.append({**base, "doc_type": DOC_TYPE_DB[r["doc_type"]], "vintage": r["vintage"]})
        else:
            items.append(
                {**base, "duration_s": r["duration_s"], "speaker": r["speaker"], "lang": r["lang"]}
            )
    return items


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--wells", type=int, default=None, help="default: [wells].count in config")
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    p.add_argument("--no-audio", action="store_true")
    p.add_argument("--no-download", action="store_true", help="do not fetch handwriting fonts")
    p.add_argument("--jobs", type=int, default=min(8, os.cpu_count() or 1))
    args = p.parse_args()

    cfg = load_config(args.config)
    n = args.wells or cfg["wells"]["count"]
    print(
        f"synthetic_assam: {n} wells, seed {args.seed}, config {args.config.name} "
        f"({cfg['basin']['status']})"
    )
    fonts = [str(f) for f in handwriting_fonts(download=not args.no_download)]
    audio = not args.no_audio and espeak() is not None
    if not args.no_audio and not audio:
        print("  warning: espeak-ng not on PATH; skipping voice notes")
    key = hashlib.sha256(
        (VERSION + args.config.read_text() + str(args.seed) + "|".join(fonts) + str(audio)).encode()
    ).hexdigest()

    jobs = [(i, args.seed, str(args.config), fonts, audio, key) for i in range(n)]
    if args.jobs <= 1:
        records = [r for j in jobs for r in render_well(*j)]
    else:
        with ProcessPoolExecutor(max_workers=args.jobs) as pool:
            records = [r for rs in pool.map(render_well, *zip(*jobs, strict=True)) for r in rs]

    wells = {w.name: w for w in (build_well(i, args.seed, cfg) for i in range(n))}
    OUT.mkdir(parents=True, exist_ok=True)
    docs = [r for r in records if r["kind"] == "document"]
    (OUT / "documents.json").write_text(
        json.dumps({"synthetic": True, "seed": args.seed, "documents": docs}, indent=1) + "\n"
    )
    gold = build_gold(docs, seed=args.seed)
    (OUT / "gold").mkdir(exist_ok=True)
    (OUT / "gold" / "gold_set.json").write_text(json.dumps(gold, indent=1) + "\n")
    write_register("synthetic_assam", register_items(records, wells))
    audio_n = sum(r["kind"] == "audio" for r in records)
    print(f"  {len(docs)} documents, {audio_n} voice notes, gold set of {len(gold['documents'])}")
