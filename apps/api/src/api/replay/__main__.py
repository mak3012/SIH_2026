"""Replay and evaluation command line.

    python -m api.replay well --well SA-06 --speed 300        one well through the live path
    python -m api.replay evaluate --write-docs                 leave-one-well-out over every
                                                               synthetic well + extraction + chat
    python -m api.replay fill ui/*.tsx deck/*.md               [measured:<key>] -> metrics.json

DATABASE_URL: the table owner (the replay writes real-time rows and alerts like the live
service). The synthetic truth must be loaded (`cd data && uv run python -m
synthetic_assam.load_truth`); detectors trained (`make rt-models`), or --physics-only.
`well` replays at the given speed, so the live view (/live/<wellbore id>) shows it as it happens;
`evaluate` runs unpaced, in parallel.
"""

import argparse
import gzip
import json
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg

from api.realtime.detectors import DEFAULT_PATH
from api.replay import metrics, report
from api.replay.runner import run_sync

ROOT = Path(__file__).resolve().parents[5]
DOCS = ROOT / "docs" / "evaluation.md"
OUT = ROOT / "docs" / "eval-report"
RESULTS = ROOT / "data" / "raw" / "eval" / "replay_results.json.gz"


def _dsn() -> str:
    return os.environ["DATABASE_URL"]


def _bank(a: argparse.Namespace) -> str | None:
    if a.physics_only:
        return None
    path = Path(a.detectors)
    if not path.exists():
        raise SystemExit(f"no detector bank at {path}: run `make rt-models` or pass --physics-only")
    return str(path)


def _wellbore(conn: psycopg.Connection, well: str) -> int:
    row = conn.execute(
        "SELECT b.id FROM wellbore b JOIN well w ON w.id = b.well_id WHERE b.id::text = %s"
        " OR w.name = %s OR b.name = %s ORDER BY b.parent_id NULLS FIRST, b.id LIMIT 1",
        (well, well, well),
    ).fetchone()
    if row is None:
        raise SystemExit(f"no wellbore {well!r}")
    return int(row[0])


def synthetic_wellbores(conn: psycopg.Connection) -> list[tuple[int, str]]:
    return [
        (int(i), str(n))
        for i, n in conn.execute(
            "SELECT b.id, w.name FROM wellbore b JOIN well w ON w.id = b.well_id"
            " JOIN source_ref r ON r.id = b.source_ref_id JOIN document d ON d.id = r.document_id"
            " WHERE d.synthetic AND b.parent_id IS NULL ORDER BY w.name"
        )
    ]


def cmd_well(a: argparse.Namespace) -> None:
    with psycopg.connect(_dsn()) as conn:
        wb = _wellbore(conn, a.well)
    speed = None if a.speed == 0 else a.speed
    print(
        f"replaying wellbore {wb} at {'max' if speed is None else f'{speed:g}x'};"
        f" live view: https://localhost:5173/#/live/{wb}",
        flush=True,
    )
    r = run_sync((_dsn(), wb, _bank(a), speed, a.seed, a.explain, a.focus))
    w = metrics.well_metrics(r)
    print(
        f"{r['well']}: {r['hours']:.1f} h replayed in {r['wall_s']:.0f} s; windows {r['windows']}"
    )
    print(f"offsets (leave-one-out): {', '.join(f'{n} {s}' for n, s in r['offsets'])}")
    for e in w["events"]:
        lead = "not detected" if e["lead_h"] is None else f"lead {e['lead_h'] * 60:+.1f} min"
        print(f"  event {e['hazard']} at {e['md']:.0f} m: {lead}, highest tier {e['first_tier']}")
    for f in w["false_alerts"]:
        print(f"  false alert {f['hazard']} tier {f['tier']} at {f['ts']}")
    if a.json:
        Path(a.json).write_text(json.dumps(r, default=str))


def chat_eval(modes: list[str]) -> dict[str, Any]:
    from api.assistant import evaluate as aev
    from api.assistant.retrieval import embedder_from_env, index
    from api.assistant.service import PROMPTS, Assistant
    from api.auth import User
    from core.llm import LLM, LLMConfig, PgLog

    emb = embedder_from_env()
    with psycopg.connect(_dsn()) as conn:
        index(conn, emb)
    meta, questions = aev.load_set()
    user = User("eval", frozenset({"planner"}), tuple(meta["user_blocks"]))
    out = {}
    for mode in modes:
        llm = None
        if mode == "llm":
            llm = LLM(
                LLMConfig.from_env(),
                PgLog(lambda: psycopg.connect(_dsn(), autocommit=True)),
                prompts=PROMPTS,
            )
            if not llm.enabled:
                continue
        rep = aev.run(Assistant(lambda: psycopg.connect(_dsn()), llm, emb), questions, user)
        out[mode] = {
            k: rep[k]
            for k in ("questions", "accuracy", "citation", "verifier_reject_rate", "by_category")
        }
    return out


def cmd_evaluate(a: argparse.Namespace) -> None:
    t0 = time.time()
    with psycopg.connect(_dsn()) as conn:
        wells = synthetic_wellbores(conn)
    if not wells:
        raise SystemExit(
            "no synthetic wells: cd data && uv run python -m synthetic_assam.load_truth"
        )
    if a.wells:
        wells = wells[: a.wells]
    bank = _bank(a)
    speed = None if a.speed == 0 else a.speed
    print(f"leave-one-well-out over {len(wells)} wells, {a.jobs} in parallel", flush=True)
    results = []
    with ProcessPoolExecutor(a.jobs) as ex:
        futures = [(n, ex.submit(run_sync, (_dsn(), wb, bank, speed, a.seed))) for wb, n in wells]
        for n, fut in futures:
            r = fut.result()
            results.append(r)
            print(
                f"  {n}: {r['hours']:.1f} h, {len(r['episodes'])} events,"
                f" {len(r['alerts'])} alerts, {r['wall_s']:.0f} s, {r['lost']} samples lost",
                flush=True,
            )
    RESULTS.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(RESULTS, "wt", encoding="utf-8") as fh:  # every risk score, for re-analysis
        json.dump(results, fh, default=str)
    detectors = (
        "physics rules only (uncalibrated)" if bank is None else f"detectors {Path(bank).name}"
    )
    publish(a, results, detectors, t0)


def cmd_report(a: argparse.Namespace) -> None:
    """Rebuild the report and docs from the last saved replay results (no replay)."""
    with gzip.open(RESULTS, "rt", encoding="utf-8") as fh:
        results = json.load(fh)
    publish(a, results, "detectors from the saved run", time.time())


def publish(
    a: argparse.Namespace, results: list[dict[str, Any]], detectors: str, t0: float
) -> None:
    agg = metrics.aggregate(results)
    extraction = report.extraction_from_docs(DOCS)
    chat = None if a.no_chat else chat_eval(["rules", *(["llm"] if a.chat_llm else [])])
    meta = {
        "generated": datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
        "command": "python -m api.replay evaluate" + (" --chat-llm" if a.chat_llm else ""),
        "detectors": detectors,
    }
    hl = report.write_report(Path(a.out), agg, results, extraction, chat, meta)
    if a.write_docs:
        sections = report.markdown(agg, results, extraction, chat, meta, "eval-report")
        report.write_docs(DOCS, sections)
    print(json.dumps(hl, indent=1, default=str))
    print(f"report: {Path(a.out) / 'report.html'} ({time.time() - t0:.0f} s)")


def cmd_fill(a: argparse.Namespace) -> None:
    data = json.loads(Path(a.metrics).read_text())["metrics"]
    print(json.dumps(report.fill([Path(p) for p in a.files], data), indent=1))


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    p = argparse.ArgumentParser(prog="python -m api.replay")
    sub = p.add_subparsers(dest="cmd", required=True)
    w = sub.add_parser("well", help="replay one well through the live path")
    w.add_argument("--well", required=True, help="wellbore id, well name or wellbore name")
    w.add_argument("--speed", type=float, default=300.0, help="1-600x; 0 = as fast as possible")
    w.add_argument("--json", help="write the full result here")
    w.add_argument(
        "--explain", action="store_true", help="LLM explanations of alerts, as on a rig (demo)"
    )
    w.add_argument(
        "--focus",
        choices=["loss", "kick", "stuck_pipe"],
        help="replay only 20 min before to 10 min after the first such recorded event (demo)",
    )
    e = sub.add_parser("evaluate", help="leave-one-well-out over every synthetic well")
    e.add_argument("--wells", type=int, help="only the first N wells")
    e.add_argument("--jobs", type=int, default=min(12, os.cpu_count() or 2))
    e.add_argument("--speed", type=float, default=0.0, help="0 = as fast as possible")
    e.add_argument("--out", default=str(OUT))
    e.add_argument("--write-docs", action="store_true")
    e.add_argument(
        "--chat-llm", action="store_true", help="also evaluate the assistant with the LLM"
    )
    e.add_argument("--no-chat", action="store_true")
    for x in (w, e):
        x.add_argument("--detectors", default=str(DEFAULT_PATH))
        x.add_argument("--physics-only", action="store_true")
        x.add_argument("--seed", type=int, default=0)
    rp = sub.add_parser("report", help="rebuild report and docs from the last saved replay")
    rp.add_argument("--out", default=str(OUT))
    rp.add_argument("--write-docs", action="store_true")
    rp.add_argument("--chat-llm", action="store_true")
    rp.add_argument("--no-chat", action="store_true")
    f = sub.add_parser("fill", help="replace [measured:<key>] placeholders")
    f.add_argument("files", nargs="+")
    f.add_argument("--metrics", default=str(OUT / "metrics.json"))
    a = p.parse_args()
    {"well": cmd_well, "evaluate": cmd_evaluate, "report": cmd_report, "fill": cmd_fill}[a.cmd](a)


if __name__ == "__main__":
    main()
