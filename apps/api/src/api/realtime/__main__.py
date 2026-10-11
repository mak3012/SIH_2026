"""Real-time command line.

    python -m api.realtime run --wellbore-id 7 --connector witsml \
        --url http://witsml-sim:8090/witsml/store \
        --uid-well SIM --uid-wellbore SIM-1 --uid-log RT
    python -m api.realtime simulate --source synthetic --wellbore-id 7 --speed 60 --port 8090
    python -m api.realtime train --wells 60 --hours 12 --test-wells 12 --write-docs

Database: DATABASE_URL. Detectors: --detectors (default data/models/realtime/detectors.joblib;
physics-only when the file is missing). LLM explainer: LLM_BACKEND / LLM_URL / LLM_MODEL.
"""

import argparse
import asyncio
import json
import logging
import os
import random
import re
import sys
from pathlib import Path
from typing import Any

from api.realtime import connectors as cn
from api.realtime import detectors as dt

log = logging.getLogger("api.realtime")
DOCS = Path(__file__).resolve().parents[5] / "docs" / "realtime.md"


def _connector(a: argparse.Namespace) -> Any:
    if a.connector == "witsml":
        return cn.WitsmlPoller(
            a.wellbore_id, a.url, a.uid_well, a.uid_wellbore, a.uid_log, a.interval
        )
    if a.connector == "wits0":
        return cn.Wits0Listener(
            a.wellbore_id, port=a.port, units=json.loads(a.units) if a.units else None
        )
    if a.connector == "opcua":
        return cn.OpcUaClient(
            a.wellbore_id, a.url, json.loads(a.nodes), json.loads(a.units or "{}"), a.interval
        )
    if a.connector == "sql":
        return cn.SqlPoller(
            a.wellbore_id,
            os.environ["RT_SQL_DSN"],
            a.query,
            json.loads(a.units or "{}"),
            a.interval,
        )
    if a.connector == "etp":
        return cn.Etp12Client(a.wellbore_id, a.url, [])
    raise SystemExit(f"unknown connector {a.connector}")


def run(a: argparse.Namespace) -> None:
    import psycopg
    from prometheus_client import start_http_server

    from api.realtime.service import Service
    from core import telemetry
    from core.llm import LLM, LLMConfig, PgLog, PromptSet

    telemetry.setup("realtime")
    if os.environ.get("RT_METRICS_PORT"):  # Prometheus scrapes it (TLS when certs are set)
        start_http_server(
            int(os.environ["RT_METRICS_PORT"]),
            certfile=os.environ.get("TLS_CERT_FILE"),
            keyfile=os.environ.get("TLS_KEY_FILE"),
        )

    dsn = os.environ["DATABASE_URL"]
    bank = None
    if Path(a.detectors).exists():
        bank = dt.load(Path(a.detectors))
    else:
        log.warning("no detector bank at %s: physics rules only (uncalibrated)", a.detectors)
    prompts = PromptSet(Path(__file__).resolve().parents[1] / "prompts", {"alert_explainer": "v2"})
    llm = LLM(
        LLMConfig.from_env(), PgLog(lambda: psycopg.connect(dsn, autocommit=True)), prompts=prompts
    )
    feed_config = {
        k: v
        for k, v in vars(a).items()
        if k in ("url", "uid_well", "uid_wellbore", "uid_log", "port", "interval")
    }
    asyncio.run(
        Service(dsn, a.wellbore_id, _connector(a), bank, llm, feed_config=feed_config).run()
    )


def simulate(a: argparse.Namespace) -> None:
    import uvicorn

    from api.realtime import simulator as sm
    from api.realtime.synthetic import plan_from_db, random_plan

    if a.source == "synthetic":
        if a.wellbore_id:
            import psycopg

            with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
                plan = plan_from_db(conn, a.wellbore_id, a.from_md)
        else:
            plan = random_plan(random.Random(a.seed), "SIM", a.hours)
        src = sm.synthetic_source(plan, a.seed)
    elif a.source == "forge":
        src = sm.forge_source(Path(a.path))
    else:
        src = sm.volve_source(Path(a.path))
    sim = sm.Simulator(src, a.speed, a.uid_well, a.uid_wellbore, a.uid_log)
    app = sm.make_app(sim)

    @app.on_event("startup")
    async def _start() -> None:
        asyncio.get_running_loop().create_task(sim.run())

    uvicorn.run(app, host="0.0.0.0", port=a.port, ssl_certfile=a.tls_cert, ssl_keyfile=a.tls_key)


def train(a: argparse.Namespace) -> None:
    rows = dt.dataset(range(a.seed, a.seed + a.wells), a.hours)
    bank = dt.train(rows, a.folds, a.seed)
    digest = bank.save(Path(a.out))
    print(f"saved {a.out} sha256 {digest[:12]}", flush=True)
    evaluate(a, bank)


def evaluate(a: argparse.Namespace, bank: dt.Bank | None = None) -> None:
    """Event-level evaluation on held-out synthetic wells (seeds 10000+), with the physics-only
    baseline; prints the tables and, with --write-docs, puts them in docs/realtime.md."""
    bank = bank or dt.load(Path(a.out))
    test = range(10_000 + a.seed, 10_000 + a.seed + a.test_wells)
    events, baseline = dt.evaluate(bank, test, a.hours), dt.evaluate(None, test, a.hours)
    md = dt.report(bank, events, baseline)
    print(md)
    if a.write_docs:
        text = DOCS.read_text(encoding="utf-8")
        new = re.sub(
            r"(<!-- metrics:start -->\n).*?(\n<!-- metrics:end -->)",
            lambda m: m.group(1) + md + m.group(2),
            text,
            flags=re.S,
        )
        DOCS.write_text(new, encoding="utf-8")


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]  # tables use → and ·
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    p = argparse.ArgumentParser(prog="python -m api.realtime")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="ingest one wellbore's feed")
    r.add_argument("--wellbore-id", type=int, required=True)
    r.add_argument("--connector", choices=["witsml", "etp", "wits0", "opcua", "sql"], required=True)
    r.add_argument("--url")
    r.add_argument("--uid-well", default="SIM")
    r.add_argument("--uid-wellbore", default="SIM-1")
    r.add_argument("--uid-log", default="RT")
    r.add_argument("--port", type=int, default=7000, help="WITS0 listen port")
    r.add_argument("--interval", type=float, default=2.0)
    r.add_argument("--units", help="JSON channel -> unit (WITS0, OPC-UA, SQL)")
    r.add_argument("--nodes", help="JSON channel -> OPC-UA node id")
    r.add_argument("--query", help="SQL with %%(since)s (dsn from RT_SQL_DSN)")
    r.add_argument("--detectors", default=str(dt.DEFAULT_PATH))
    s = sub.add_parser("simulate", help="WITSML 1.4.1.1 store replaying a well")
    s.add_argument("--source", choices=["synthetic", "forge", "volve"], default="synthetic")
    s.add_argument("--wellbore-id", type=int, help="synthetic: replay this seeded wellbore")
    s.add_argument("--from-md", type=float)
    s.add_argument("--path", help="forge: the 10 s CSV; volve: directory of WITSML logs")
    s.add_argument("--speed", type=float, default=60.0)
    s.add_argument("--seed", type=int, default=0)
    s.add_argument("--hours", type=float, default=12.0)
    s.add_argument("--port", type=int, default=8090)
    s.add_argument("--tls-cert", help="serve HTTPS with this certificate (and --tls-key)")
    s.add_argument("--tls-key")
    s.add_argument("--uid-well", default="SIM")
    s.add_argument("--uid-wellbore", default="SIM-1")
    s.add_argument("--uid-log", default="RT")
    t = sub.add_parser("train", help="train and evaluate the detector bank on synthetic wells")
    t.add_argument("--wells", type=int, default=60)
    t.add_argument("--hours", type=float, default=12.0)
    t.add_argument("--test-wells", type=int, default=12)
    t.add_argument("--folds", type=int, default=5)
    t.add_argument("--seed", type=int, default=0)
    t.add_argument("--out", default=str(dt.DEFAULT_PATH))
    t.add_argument("--write-docs", action="store_true")
    e = sub.add_parser("evaluate", help="evaluate a trained bank on held-out synthetic wells")
    e.add_argument("--hours", type=float, default=12.0)
    e.add_argument("--test-wells", type=int, default=12)
    e.add_argument("--seed", type=int, default=0)
    e.add_argument("--out", default=str(dt.DEFAULT_PATH))
    e.add_argument("--write-docs", action="store_true")
    a = p.parse_args()
    {"run": run, "simulate": simulate, "train": train, "evaluate": evaluate}[a.cmd](a)


if __name__ == "__main__":
    main()
