"""WITSML simulator: replays a well at 1x-600x as a WITSML 1.4.1.1 store, so the real
connector (WitsmlPoller) is what reads it.

Sources:
- synthetic  a seeded wellbore from the database (plan_from_db: survey, tops, casing, events)
             or a random training well; channels from synthetic.py
- forge      Utah FORGE 16A(78)-32 10 s Pason CSV (data/raw/forge, `--with-timeseries`)
- volve      Volve WITSML real-time log files (*.xml) in a directory

Timestamps are rebased so the first sample is "now"; data time then advances `speed` times
faster than the wall clock. The store answers WMLS_GetFromStore for its one log
(uidWell/uidWellbore/uid as configured) with rows newer than startDateTimeIndex, at most
MAX_ROWS per call, never rows "in the future" of the replay clock.

    python -m api.realtime simulate --source synthetic --wellbore-id 7 --speed 60 --port 8090
"""

import asyncio
import bisect
import csv
import re
import time
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

from fastapi import FastAPI, Request, Response

from api.realtime.channels import CHANNELS, Sample
from api.realtime.connectors import SOAP, WML, WMLS, canonical, parse_logs, parse_time
from api.realtime.synthetic import Plan, generate

MAX_ROWS = 2000
MIN_SPEED, MAX_SPEED = 1.0, 600.0
MNEMONIC = {
    "bit_md": "DBTM",
    "hole_md": "DMEA",
    "block_pos": "BPOS",
    "hookload": "HKLA",
    "wob": "WOBA",
    "rpm": "RPMA",
    "torque": "TQA",
    "spp": "SPPA",
    "flow_in": "MFIA",
    "flow_out": "MFOA",
    "pit_vol": "TVA",
    "mw_in": "MDIA",
    "rop": "ROPA",
}
WITSML_UNIT = {
    "m": "m",
    "kN": "kN",
    "rpm": "rpm",
    "kN.m": "kN.m",
    "bar": "bar",
    "L/min": "L/min",
    "m3": "m3",
    "sg": "g/cm3",
    "m/h": "m/h",
}

Row = tuple[datetime, dict[str, float]]


# ------------------------------------------------------------------ sources


def synthetic_source(plan: Plan, seed: int = 0, dt: float = 5.0) -> Iterator[Row]:
    for t in generate(plan, seed=seed, dt=dt):
        yield t.ts, t.values


# Pason CSV header keywords -> channel (FORGE). Units from "(...)" in the header.
FORGE_KEYS = [
    ("hole depth", "hole_md"),
    ("bit depth", "bit_md"),
    ("block height", "block_pos"),
    ("hook load", "hookload"),
    ("weight on bit", "wob"),
    ("rotary rpm", "rpm"),
    ("top drive rpm", "rpm"),
    ("rotary torque", "torque"),
    ("top drive torque", "torque"),
    ("standpipe pressure", "spp"),
    ("total pump output", "flow_in"),
    ("total mud volume", "pit_vol"),
    ("rate of penetration", "rop"),
]
FORGE_UNITS = {
    "feet": "ft",
    "klbs": "klbf",
    "psi": "psi",
    "gal_per_min": "gal/min",
    "gpm": "gal/min",
    "barrels": "bbl",
    "ft_per_hr": "ft/h",
    "rpm": "rpm",
    "kft_lb": "kft.lbf",
    "ft_lb": "ft.lbf",
}


def forge_columns(header: list[str]) -> dict[int, tuple[str, float]]:
    """Column index -> (channel, factor). Unknown units (e.g. torque in amps) are skipped.
    Mapping by header keywords: check it against the real file when it is downloaded."""
    out: dict[int, tuple[str, float]] = {}
    for i, h in enumerate(header):
        low = h.lower()
        m = re.search(r"\(([^)]*)\)", low)
        unit = FORGE_UNITS.get(m.group(1).strip()) if m else None
        for key, ch in FORGE_KEYS:
            if key in low and ch not in {c for c, _ in out.values()}:
                mp = canonical(ch, unit) if (unit or not m) else None
                if mp:
                    out[i] = mp
                break
    return out


def forge_source(path: Path) -> Iterator[Row]:
    with path.open(encoding="utf-8-sig", newline="") as f:
        rd = csv.reader(f)
        header = next(rd)
        cols = forge_columns(header)
        low = [h.lower() for h in header]
        i_date = next(i for i, h in enumerate(low) if "date" in h)
        i_time = next((i for i, h in enumerate(low) if h.startswith("time")), None)
        for r in rd:
            stamp = r[i_date] + (" " + r[i_time] if i_time is not None else "")
            try:
                ts = datetime.strptime(stamp.strip(), "%Y/%m/%d %H:%M:%S").replace(tzinfo=UTC)
            except ValueError:
                ts = parse_time(stamp)
            vals = {}
            for i, (ch, k) in cols.items():
                try:
                    vals[ch] = float(r[i]) * k
                except (ValueError, IndexError):
                    pass
            yield ts, vals


def volve_source(directory: Path) -> Iterator[Row]:
    """All time-indexed logs in *.xml under `directory`, merged in time order."""
    rows: list[Row] = []
    for p in sorted(directory.rglob("*.xml")):
        try:
            rows += parse_logs(p.read_text(encoding="utf-8", errors="replace"))
        except (ET.ParseError, ValueError):
            continue
    rows.sort(key=lambda r: r[0])
    yield from rows


# ------------------------------------------------------------------ replay + store


class Simulator:
    def __init__(
        self,
        source: Iterator[Row],
        speed: float | None = 60.0,
        uid_well: str = "SIM",
        uid_wellbore: str = "SIM-1",
        uid_log: str = "RT",
        rebase: bool = True,
    ) -> None:
        """`speed` None = unpaced: rows are released as fast as they are generated (batch
        evaluation); the store and the poller work exactly as at 1x-600x."""
        if speed is not None and not MIN_SPEED <= speed <= MAX_SPEED:
            raise ValueError(f"speed must be {MIN_SPEED:g}x-{MAX_SPEED:g}x")
        self.source, self.speed, self.rebase = source, speed, rebase
        self.uids = (uid_well, uid_wellbore, uid_log)
        self.times: list[datetime] = []
        self.rows: list[dict[str, float]] = []
        self.done = False
        self.paused = False  # tests: freeze the feed to make it stale

    async def run(self) -> None:
        t0 = w0 = shift = None
        for ts, vals in self.source:
            if t0 is None:
                t0, w0 = ts, time.monotonic()
                shift = datetime.now(UTC) - ts if self.rebase else timedelta(0)
            if self.speed is None:
                if len(self.rows) % 500 == 0:
                    await asyncio.sleep(0)  # let the store answer polls
            else:
                wait = w0 + (ts - t0).total_seconds() / self.speed - time.monotonic()  # type: ignore[operator]
                if wait > 0:
                    await asyncio.sleep(wait)
            while self.paused:
                await asyncio.sleep(0.1)
            self.times.append(ts + shift)  # type: ignore[operator]
            self.rows.append(vals)
        self.done = True

    def after(self, since: datetime | None) -> list[Row]:
        i = 0 if since is None else bisect.bisect_right(self.times, since)
        return list(zip(self.times[i : i + MAX_ROWS], self.rows[i : i + MAX_ROWS], strict=True))

    def log_xml(self, since: datetime | None) -> str:
        names = [c for c in CHANNELS if c in MNEMONIC]
        mn = ",".join(["TIME", *(MNEMONIC[c] for c in names)])
        un = ",".join(["s", *(WITSML_UNIT[CHANNELS[c][0]] for c in names)])
        data = "".join(
            "<data>"
            + ",".join([ts.isoformat(), *(f"{v[c]:.4f}" if c in v else "" for c in names)])
            + "</data>"
            for ts, v in self.after(since)
        )
        w, wb, lg = (escape(u) for u in self.uids)
        return (
            f'<logs xmlns="{WML}" version="1.4.1.1">'
            f'<log uidWell="{w}" uidWellbore="{wb}" uid="{lg}">'
            "<indexType>date time</indexType><indexCurve>TIME</indexCurve>"
            f"<logData><mnemonicList>{mn}</mnemonicList><unitList>{un}</unitList>{data}</logData>"
            "</log></logs>"
        )


def _envelope(op: str, result: int, xml_out: str = "", msg: str = "") -> Response:
    body = (
        f'<soap:Envelope xmlns:soap="{SOAP}"><soap:Body><{op}Response xmlns="{WMLS}">'
        f"<Result>{result}</Result><XMLout>{escape(xml_out)}</XMLout>"
        f"<SuppMsgOut>{escape(msg)}</SuppMsgOut></{op}Response></soap:Body></soap:Envelope>"
    )
    return Response(body, media_type="text/xml")


def make_app(sim: Simulator) -> FastAPI:
    app = FastAPI(title="WITSML 1.4.1.1 simulator")

    @app.post("/witsml/store")
    async def store(request: Request) -> Response:
        env = ET.fromstring(await request.body())
        body = env.find(f"{{{SOAP}}}Body")
        op = body[0].tag.split("}")[-1] if body is not None and len(body) else ""
        if op == "WMLS_GetVersion":
            return Response(
                f'<soap:Envelope xmlns:soap="{SOAP}"><soap:Body><WMLS_GetVersionResponse '
                f'xmlns="{WMLS}"><Result>1.4.1.1</Result></WMLS_GetVersionResponse>'
                "</soap:Body></soap:Envelope>",
                media_type="text/xml",
            )
        if op != "WMLS_GetFromStore":
            return _envelope(op or "Unknown", -425, msg="only WMLS_GetFromStore / GetVersion")
        if (body[0].findtext("WMLtypeIn") or "").strip() != "log":  # type: ignore[index]
            return _envelope(op, -487, msg="only the log object is served")
        q = ET.fromstring(body[0].findtext("QueryIn") or "")  # type: ignore[index]
        lg = q.find(f"{{{WML}}}log")
        if lg is None or (lg.get("uidWell"), lg.get("uidWellbore"), lg.get("uid")) != sim.uids:
            return _envelope(op, 2, msg="no such log")  # 2 = success, nothing found
        start = lg.findtext(f"{{{WML}}}startDateTimeIndex")
        return _envelope(op, 1, sim.log_xml(parse_time(start) if start else None))

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {"rows": len(sim.rows), "done": sim.done, "speed": sim.speed}

    return app


def samples(sim: Simulator, wellbore_id: int) -> list[Sample]:
    return [Sample(wellbore_id, ts, v) for ts, v in sim.after(None)]
