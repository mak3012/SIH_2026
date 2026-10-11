"""Evaluate extraction on the synthetic gold set and (optionally) write the results into
docs/evaluation.md between the `eval:start` / `eval:end` markers.

Usage: python -m pipelines.evaluate [--gold PATH] [--raw DIR] [--write-docs] [--json OUT]

Scoring, per field (list items pooled: tops[].md):
  TP  predicted, and value matches gold (norm.same: 0.16 m, 1e-5 deg, 0.006 otherwise)
  FP  predicted but wrong, or predicted a field the gold set does not have
  FN  gold field not predicted, or predicted wrong (a wrong value counts as FP and FN)
Also checked: every field below the confidence threshold is routed to review (and no other),
IADC mapping of DDR operations against the printed codes, and DDR event stitching against the
generator's truth.
"""

import argparse
import json
import re
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from pipelines import ocr
from pipelines.ddr import PRINTED_CODE_IADC, Row, depth_in, iadc, stitch
from pipelines.norm import same
from pipelines.process import DocResult, Settings, process

ROOT = Path(__file__).resolve().parents[3]
RAW = ROOT / "data" / "raw"
GOLD = RAW / "synthetic_assam" / "gold" / "gold_set.json"
DOCS = ROOT / "docs" / "evaluation.md"


def family(name: str) -> str:
    return re.sub(r"\[\d+\]", "[]", name)


# Table rows are matched on this column before fields are compared, so one dropped or extra
# row does not shift every later row out of place.
ANCHOR = {
    "tops": "md",
    "survey": "md",
    "casing": "shoe_md",
    "lot_fit": "md",
    "events": "md",
    "time_breakdown": "from",
}


def align(gold: dict[str, Any], got: dict[str, Any]) -> dict[str, Any]:
    """Rename predicted list rows to the index of the gold row with the same anchor value."""
    out = {n: v for n, v in got.items() if not re.match(r"^\w+\[\d+\]", n)}
    rows: dict[str, dict[int, dict[str, Any]]] = defaultdict(lambda: defaultdict(dict))
    for n, v in got.items():
        m = re.match(r"^(\w+)\[(\d+)\](.*)$", n)
        if m:
            rows[m.group(1)][int(m.group(2))][m.group(3)] = v
    for prefix, pred in rows.items():
        key = ANCHOR.get(prefix)
        gold_idx = {}
        for n, f in gold.items():
            m = re.match(rf"^{prefix}\[(\d+)\]\.{key}$", n) if key else None
            if m:
                gold_idx[int(m.group(1))] = f["value"]
        taken: set[int] = set()
        for i, fields in sorted(pred.items()):
            j = i
            if key and f".{key}" in fields:
                v = fields[f".{key}"].value
                j = next(
                    (
                        gi
                        for gi, gv in sorted(gold_idx.items(), key=lambda kv: abs(kv[0] - i))
                        if gi not in taken and same(f"{prefix}[0].{key}", v, gv)
                    ),
                    i,
                )
            taken.add(j)
            for suffix, v in fields.items():
                out[f"{prefix}[{j}]{suffix}"] = v
    return out


def score(gold: list[dict[str, Any]], got: DocResult) -> Counter[tuple[str, str]]:
    c: Counter[tuple[str, str]] = Counter()
    g = {f["name"]: f for f in gold}
    fields = align(g, got.fields)
    for n, f in g.items():
        want = f["value"]
        p = fields.get(n)
        if p is None:
            c[(family(n), "fn")] += 1
        elif same(n, p.value, want):
            c[(family(n), "tp")] += 1
        else:
            c[(family(n), "fp")] += 1
            c[(family(n), "fn")] += 1
    for n in fields:
        if n not in g:
            c[(family(n), "fp")] += 1
    return c


def prf(tp: int, fp: int, fn: int) -> tuple[float, float, float]:
    p = tp / (tp + fp) if tp + fp else 0.0
    r = tp / (tp + fn) if tp + fn else 0.0
    return p, r, (2 * p * r / (p + r) if p + r else 0.0)


def run(gold_path: Path, raw: Path, settings: Settings) -> dict[str, Any]:
    gold = json.loads(gold_path.read_text())
    by_style: dict[str, Counter[tuple[str, str]]] = defaultdict(Counter)
    readable: Counter[str] = Counter()
    total: Counter[str] = Counter()
    routing_errors = 0
    routed = {"accepted": 0, "review": 0}
    iadc_hits = iadc_total = 0
    for d in gold["documents"]:
        style = d["render"]
        total[style] += 1
        r = process(
            raw / d["path"],
            settings=settings,
            registered_type=None,
            cache=raw / ".cache" / "pages" / Path(d["path"]).stem,
        )
        if r.fields:
            readable[style] += 1
        by_style[style] += score(d["fields"], r)
        for f in r.fields.values():
            routed["accepted" if f.accepted else "review"] += 1
            if (f.confidence < settings.threshold) == f.accepted:
                routing_errors += 1
        if d["doc_type"] == "ddr":
            for i in range(100):
                code, desc = (
                    r.fields.get(f"time_breakdown[{i}].code"),
                    r.fields.get(f"time_breakdown[{i}].description"),
                )
                if code is None or desc is None:
                    break
                iadc_total += 1
                iadc_hits += iadc(str(desc.value))[0] == PRINTED_CODE_IADC.get(str(code.value))
    return {
        "gold": gold_path.relative_to(ROOT).as_posix(),
        "documents": len(gold["documents"]),
        "engines": {e: ocr.available(e) for e in ("docling", "paddleocr")},
        "threshold": settings.threshold,
        "by_style": by_style,
        "readable": readable,
        "total": total,
        "routing_errors": routing_errors,
        "routed": routed,
        "iadc": (iadc_hits, iadc_total),
        "stitching": stitching(raw, settings),
    }


def stitching(raw: Path, settings: Settings) -> dict[str, Any]:
    """Stitch each synthetic well's typed DDRs; compare with truth.json every event whose report
    days all have a typed DDR (the others need OCR and are scored with the OCR runs)."""
    docs = json.loads((raw / "synthetic_assam" / "documents.json").read_text())["documents"]
    wells: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for d in docs:
        if d["doc_type"] == "ddr":
            wells[d["well"]].append(d)
    found = matched = npt_ok = multi_day = expected = 0
    for well, ddrs in sorted(wells.items()):
        typed_days = {d["vintage"] for d in ddrs if d["render"] == "typed"}
        truth = json.loads((raw / "synthetic_assam" / "wells" / well / "truth.json").read_text())
        todo = []
        for te in truth["well"]["events"]:
            t0, t1 = datetime.fromisoformat(te["t_start"]), datetime.fromisoformat(te["t_end"])
            days = {
                (t0.date() + timedelta(days=k)).isoformat()
                for k in range(
                    (t1 - timedelta(seconds=1)).date().toordinal() - t0.date().toordinal() + 1
                )
            }
            if days <= typed_days:
                todo.append(te)
        if not todo:
            continue
        rows = []
        for d in ddrs:
            if d["render"] != "typed":
                continue
            r = process(
                raw / d["path"],
                settings=settings,
                cache=raw / ".cache" / "pages" / Path(d["path"]).stem,
            )
            day = date.fromisoformat(r.fields["report_date"].value)
            for i in range(200):
                f = r.fields.get(f"time_breakdown[{i}].from")
                if f is None:
                    break
                desc = str(r.fields[f"time_breakdown[{i}].description"].value)
                rows.append(
                    Row(
                        day,
                        f.value,
                        r.fields[f"time_breakdown[{i}].to"].value,
                        r.fields[f"time_breakdown[{i}].hours"].value,
                        desc,
                        depth_in(desc, r.depth_unit),
                    )
                )
        events = [e for e in stitch(rows) if e.type != "rig_repair"]
        found += len(events)
        for te in todo:
            expected += 1
            t0 = datetime.fromisoformat(te["t_start"])
            hit = next((e for e in events if e.type == te["type"] and e.t_start == t0), None)
            if hit:
                matched += 1
                npt_ok += abs(hit.npt_hr - te["npt_hr"]) < 0.01
                multi_day += hit.days > 1
    return {
        "expected": expected,
        "found": found,
        "matched": matched,
        "npt_exact": npt_ok,
        "multi_day": multi_day,
    }


def markdown(res: dict[str, Any]) -> str:
    lines = [
        f"Gold set: `{res['gold']}` ({res['documents']} documents). Threshold "
        f"{res['threshold']}. Optional engines in this run: "
        + ", ".join(f"{k} {'yes' if v else 'no'}" for k, v in res["engines"].items())
        + f". Generated {date.today().isoformat()} by `python -m pipelines.evaluate`.",
        "",
        "### By how the document was produced",
        "",
        "| Render | Docs | Readable | Precision | Recall | F1 |",
        "|---|---|---|---|---|---|",
    ]
    for style in ("typed", "scanned", "fax", "handwritten"):
        c = res["by_style"].get(style, Counter())
        tp, fp, fn = (sum(v for (_, k), v in c.items() if k == x) for x in ("tp", "fp", "fn"))
        p, r, f = prf(tp, fp, fn)
        n, ok = res["total"][style], res["readable"][style]
        cells = (
            f"{p:.3f} | {r:.3f} | **{f:.3f}**"
            if ok
            else "not evaluated: no engine could read these pages in this run"
        )
        lines.append(f"| {style} | {n} | {ok} | {cells} |")
    lines += [
        "",
        "### Per field",
        "",
        "Typed counts, then F1 per render (`-`: field not in that stratum or not readable).",
        "",
        "| Field | TP | FP | FN | typed F1 | scanned F1 | fax F1 | handwritten F1 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    styles = ("typed", "scanned", "fax", "handwritten")
    fams = sorted({k for st in styles for k, _ in res["by_style"].get(st, Counter())})
    c = res["by_style"].get("typed", Counter())
    for fam in fams:
        f1s = []
        for st in styles:
            cs = res["by_style"].get(st, Counter())
            n = cs[(fam, "tp")] + cs[(fam, "fp")] + cs[(fam, "fn")]
            f1s.append(
                f"{prf(cs[(fam, 'tp')], cs[(fam, 'fp')], cs[(fam, 'fn')])[2]:.3f}"
                if n and res["readable"][st]
                else "-"
            )
        lines.append(
            f"| `{fam}` | {c[(fam, 'tp')]} | {c[(fam, 'fp')]} | {c[(fam, 'fn')]} | "
            + " | ".join(f1s)
            + " |"
        )
    hits, total = res["iadc"]
    st = res["stitching"]
    lines += [
        "",
        "### Routing, IADC mapping, stitching",
        "",
        f"- Fields routed: {res['routed']['accepted']} accepted, {res['routed']['review']} to "
        f"review. Fields whose routing disagrees with their confidence: "
        f"**{res['routing_errors']}**.",
        f"- IADC code from operation text vs printed mnemonic: {hits}/{total}"
        + (f" ({hits / total:.1%})" if total else "")
        + ".",
        f"- DDR stitching (events whose report days all have a typed DDR): {st['matched']}/"
        f"{st['expected']} truth events found with exact start time, {st['npt_exact']} with "
        f"exact NPT hours, "
        f"{st['multi_day']} of them spanning more than one report; {st['found']} events stitched.",
    ]
    return "\n".join(lines)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--gold", type=Path, default=GOLD)
    p.add_argument("--raw", type=Path, default=RAW)
    p.add_argument("--write-docs", action="store_true")
    p.add_argument("--json", type=Path)
    a = p.parse_args()
    res = run(a.gold, a.raw, Settings.from_env())
    md = markdown(res)
    print(md)
    if a.json:
        out = dict(res)
        out["by_style"] = {
            st: {f"{k}|{kind}": v for (k, kind), v in c.items()}
            for st, c in res["by_style"].items()
        }
        a.json.write_text(json.dumps(out, indent=1, default=str))
    if a.write_docs:
        text = DOCS.read_text(encoding="utf-8")
        start, end = "<!-- eval:start -->", "<!-- eval:end -->"
        if start not in text:
            raise SystemExit(f"{DOCS} has no {start} marker")
        head, rest = text.split(start, 1)
        DOCS.write_text(
            head + start + "\n" + md + "\n" + end + rest.split(end, 1)[1], encoding="utf-8"
        )


if __name__ == "__main__":
    main()
