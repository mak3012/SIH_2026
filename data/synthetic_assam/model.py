"""Ground truth for one synthetic well: location, datum, trajectory, tops, casing, events and a
minute-resolution operations timeline that the daily drilling reports are cut from.

Everything here is canonical (metres, sg, UTC-naive local rig time). Units, datums and wording
as printed are decided later by the renderer; the printed strings go into the gold set.
"""

import random
import tomllib
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from core.depth import Position, Station, at_md, at_tvd, minimum_curvature
from core.geodesy import wgs84_to_kalianpur1975

DEFAULT_CONFIG = Path(__file__).with_name("stratigraphy.toml")
EVENT_TYPES = ("loss", "stuck_pipe", "kick")


def load_config(path: Path = DEFAULT_CONFIG) -> dict[str, Any]:
    with path.open("rb") as f:
        return tomllib.load(f)


@dataclass
class Top:
    formation: str
    md: float
    tvd: float
    tvdss: float


@dataclass
class SurveyRow:
    md: float
    inc: float
    azi: float
    tvd: float
    tvdss: float
    north: float
    east: float


@dataclass
class Casing:
    kind: str
    od_in: str  # as the industry writes it: 13-3/8, 9-5/8, 7
    hole_in: str
    shoe_md: float
    shoe_tvd: float
    weight_ppf: float
    grade: str


@dataclass
class LotFit:
    test_type: str
    casing: str
    md: float
    tvd: float
    emw: float


@dataclass
class Event:
    id: str
    type: str
    formation: str
    rel_pos: float
    md: float
    tvd: float
    tvdss: float
    mw: float
    ecd: float
    npt_hr: float
    severity: int
    loss_bbl: float | None = None
    pit_gain_bbl: float | None = None
    root_cause: str | None = None
    root_cause_status: str = "unknown"
    mitigations: list[str] = field(default_factory=list)
    outcome: str = ""
    t_start: datetime | None = None
    t_end: datetime | None = None


@dataclass
class Activity:
    start: datetime
    end: datetime
    code: str
    npt: bool
    kind: str  # template key for the printed description
    depth_start: float
    depth_end: float
    hole_in: str = ""
    event_id: str | None = None

    @property
    def hours(self) -> float:
        return (self.end - self.start).total_seconds() / 3600


@dataclass
class Day:
    no: int
    date: date
    depth_start: float
    depth_end: float
    mw: float
    activities: list[Activity]
    event_ids: list[str]


@dataclass
class Well:
    name: str
    block: str
    field: str
    spud: datetime
    completed: datetime
    lat: float  # WGS 84
    lon: float
    coord_datum: str  # 'WGS84' | 'Kalianpur1975' (how reports print it)
    lat_printed_datum: float
    lon_printed_datum: float
    ground_elev: float
    datum: str  # 'KB' | 'RT'
    datum_elev: float  # above MSL
    depth_unit: str  # 'm' | 'ft'
    trajectory: str  # 'vertical' | 'J' | 'S'
    td_md: float
    td_tvd: float
    survey: list[SurveyRow]
    tops: list[Top]
    casing: list[Casing]
    lot_fit: list[LotFit]
    events: list[Event]
    days: list[Day]


def _u(rng: random.Random, lo_hi: list[float]) -> float:
    return rng.uniform(lo_hi[0], lo_hi[1])


def _stations(rng: random.Random, kind: str, step: float, md_max: float) -> list[Station]:
    azi = rng.uniform(0, 360)
    kop = rng.uniform(400, 1200)
    build = rng.uniform(1.5, 3.0)
    hold = rng.uniform(20, 45) if kind == "J" else rng.uniform(18, 30)
    drop_at = kop + rng.uniform(1200, 1800)
    inc, out, md = 0.0, [], 0.0
    while md <= md_max:
        if kind == "vertical":
            inc = min(2.0, max(0.0, inc + rng.uniform(-0.15, 0.2)))
        elif md > kop and (kind == "J" or md < drop_at):
            inc = min(hold, inc + build * step / 30)
        elif kind == "S" and md >= drop_at:
            inc = max(4.0, inc - 2.0 * step / 30)
        out.append(Station(md, round(inc, 2), round((azi + rng.uniform(-1, 1)) % 360, 2)))
        md += step
    return out


def build_well(index: int, seed: int, cfg: dict[str, Any]) -> Well:
    rng = random.Random(seed * 1000 + index)
    w, b = cfg["wells"], cfg["basin"]
    forms = cfg["formation"]
    name = f"SA-{index + 1:02d}"

    lon, lat = _u(rng, w["lon"]), _u(rng, w["lat"])
    span = (lon - w["lon"][0]) / (w["lon"][1] - w["lon"][0])  # 0 west .. 1 east
    block = w["blocks"][min(int(span * len(w["blocks"])), len(w["blocks"]) - 1)]
    spud_day = date(rng.randint(*w["spud_years"]), 1, 1) + timedelta(days=rng.randint(0, 364))
    spud = datetime.combine(spud_day, datetime.min.time()) + timedelta(hours=rng.randint(4, 20))
    legacy = spud.year < w["legacy_datum_before"] and rng.random() < w["legacy_datum_share"]
    plat, plon = wgs84_to_kalianpur1975(lat, lon) if legacy else (lat, lon)

    ground = _u(rng, b["ground_elevation_m"])
    datum = "KB" if rng.random() < w["kb_share"] else "RT"
    datum_elev = ground + rng.uniform(5.5, 9.5)
    unit = "ft" if rng.random() < w["feet_share"] else "m"
    r = rng.random()
    kind = (
        "vertical"
        if r < w["vertical_share"]
        else ("S" if r < w["vertical_share"] + w["s_type_share"] else "J")
    )

    # Stack formations from the ground down. Thickness trends thicker eastwards, plus noise, so
    # neighbouring wells look alike.
    tops_ss: list[tuple[dict[str, Any], float, float]] = []
    z = -ground
    for f in forms:
        lo, hi = f["thickness_m"]
        t = lo + (hi - lo) * min(1.0, max(0.0, 0.25 + 0.5 * span + rng.gauss(0, 0.15)))
        tops_ss.append((f, z, t))
        z += t
    td_f = next(x for x in tops_ss if x[0]["name"] == b["td_formation"])
    td_tvd = td_f[1] + _u(rng, b["td_rel_pos"]) * td_f[2] + datum_elev

    step = 30.48 if unit == "ft" else 30.0  # stations every 100 ft or 30 m
    path: list[Position] = []
    md_max = td_tvd * 1.6
    stations = _stations(rng, kind, step, md_max)
    path = minimum_curvature(stations)
    td = at_tvd(path, td_tvd)
    cut = next(i for i, p in enumerate(path) if p.md >= td.md)
    stations, path = (
        stations[:cut] + [Station(td.md, stations[cut].inc, stations[cut].azi)],
        (path[:cut] + [td]),
    )
    survey = [
        SurveyRow(s.md, s.inc, s.azi, p.tvd, p.tvd - datum_elev, p.north, p.east)
        for s, p in zip(stations, path, strict=True)
    ]

    tops = []
    for f, top_ss, _ in tops_ss:
        tvd = top_ss + datum_elev
        if tvd >= td_tvd:
            break
        p = at_tvd(path, max(tvd, 0.0))
        tops.append(Top(f["name"], p.md, p.tvd, p.tvd - datum_elev))
    top_by = {t.formation: t for t in tops}

    def formation_at(md: float) -> dict[str, Any]:
        current: dict[str, Any] = forms[0]
        for f in forms:
            if f["name"] in top_by and top_by[f["name"]].md <= md:
                current = f
        return current

    # Casing points.
    surf = at_tvd(path, _u(rng, b["surface_shoe_tvd_m"]))
    above = top_by[b["intermediate_shoe_above"]]
    inter = at_md(path, above.md - _u(rng, b["intermediate_shoe_offset_m"]))
    casing = [
        Casing("surface", "13-3/8", "17-1/2", surf.md, surf.tvd, 68.0, "K55"),
        Casing("intermediate", "9-5/8", "12-1/4", inter.md, inter.tvd, 47.0, "N80"),
        Casing("production", "7", "8-1/2", td.md, td.tvd, 29.0, "P110"),
    ]

    # Events: one Bernoulli draw per formation and type, placed at a relative position.
    events: list[Event] = []
    ev_cfg = cfg["events"]
    for f in forms:
        t = top_by.get(f["name"])
        if t is None:
            continue
        nxt = [x for x in tops if x.md > t.md]
        base_md = nxt[0].md if nxt else td.md
        for et in EVENT_TYPES:
            if rng.random() >= f["p"].get(et, 0.0):
                continue
            rel = rng.random() ** f.get("rel_pos_power", 1.0)
            md = t.md + rel * (base_md - t.md)
            p = at_md(path, md)
            mw = round(_u(rng, f["mw_sg"]), 2)
            e = Event(
                id=f"{name}-E{len(events) + 1}",
                type=et,
                formation=f["name"],
                rel_pos=round(rel, 3),
                md=p.md,
                tvd=p.tvd,
                tvdss=p.tvd - datum_elev,
                mw=mw,
                ecd=round(mw + _u(rng, ev_cfg[et]["ecd_over_mw"]), 2),
                npt_hr=round(_u(rng, ev_cfg[et]["npt_hr"]) * 4) / 4,
                severity=1,
            )
            _describe(e, rng, ev_cfg)
            events.append(e)
    events.sort(key=lambda e: e.md)

    lots: list[LotFit] = []
    acts = _timeline(rng, spud, path, casing, events, formation_at, lots, datum_elev)
    days = _days(acts, events, formation_at, td.md)
    return Well(
        name=name,
        block=block,
        field=rng.choice(w["fields"]),
        spud=spud,
        completed=acts[-1].end,
        lat=lat,
        lon=lon,
        coord_datum="Kalianpur1975" if legacy else "WGS84",
        lat_printed_datum=plat,
        lon_printed_datum=plon,
        ground_elev=ground,
        datum=datum,
        datum_elev=datum_elev,
        depth_unit=unit,
        trajectory=kind,
        td_md=td.md,
        td_tvd=td.tvd,
        survey=survey,
        tops=tops,
        casing=casing,
        lot_fit=lots,
        events=events,
        days=days,
    )


CAUSES = {
    "loss": [
        "Induced fractures; ECD above fracture gradient",
        "Natural fractures in coal/sand stringers",
        "Surge while running in hole",
    ],
    "stuck_pipe": [
        "Differential sticking across permeable sand",
        "Swelling clay (pack-off)",
        "Poor hole cleaning; cuttings bed",
    ],
    "kick": [
        "Swabbing on trip out",
        "Underbalanced after mud weight cut by gas",
        "Higher pore pressure than planned",
    ],
}
MITIGATIONS = {
    "loss": ["Pumped LCM pill", "Reduced flow rate", "Spotted cement plug across loss zone"],
    "stuck_pipe": ["Jarred down", "Spotted pipe-freeing pill", "Worked pipe with torque"],
    "kick": ["Shut in well", "Circulated out with driller's method", "Raised mud weight"],
}
OUTCOMES = {
    "loss": ["Losses cured; drilled ahead", "Partial cure; continued with seepage losses"],
    "stuck_pipe": ["Pipe freed", "Pipe freed after back-reaming"],
    "kick": ["Well killed; drilled ahead with higher mud weight"],
}


def _describe(e: Event, rng: random.Random, ev_cfg: dict[str, Any]) -> None:
    if e.type == "loss":
        e.loss_bbl = round(_u(rng, ev_cfg["loss"]["loss_bbl"]))
        e.severity = 1 + sum(e.loss_bbl > x for x in (50, 150, 300, 500))
    elif e.type == "kick":
        e.pit_gain_bbl = round(_u(rng, ev_cfg["kick"]["pit_gain_bbl"]), 1)
        e.severity = 4 if e.pit_gain_bbl < 15 else 5
    else:
        e.severity = 2 + (e.npt_hr > 24) + (e.npt_hr > 48)
    r = rng.random()
    if r < 0.5:
        e.root_cause, e.root_cause_status = rng.choice(CAUSES[e.type]), "recorded"
    elif r < 0.7:
        e.root_cause, e.root_cause_status = rng.choice(CAUSES[e.type]), "inferred"
    e.mitigations = rng.sample(MITIGATIONS[e.type], rng.randint(1, 2))
    e.outcome = rng.choice(OUTCOMES[e.type])


def _timeline(
    rng: random.Random,
    spud: datetime,
    path: list[Position],
    casing: list[Casing],
    events: list[Event],
    formation_at: Any,
    lots: list[LotFit],
    datum_elev: float,
) -> list[Activity]:
    acts: list[Activity] = []
    t = datetime.combine(spud.date(), datetime.min.time())

    def add(
        code: str,
        hours: float,
        kind: str,
        d0: float,
        d1: float | None = None,
        npt: bool = False,
        hole: str = "",
        event_id: str | None = None,
    ) -> None:
        nonlocal t
        end = t + timedelta(minutes=max(15, round(hours * 4) * 15))  # quarter hours, as on DDRs
        acts.append(Activity(t, end, code, npt, kind, d0, d0 if d1 is None else d1, hole, event_id))
        t = end

    add("RIG", (spud - t).total_seconds() / 3600, "rig_up", 0.0)
    depth, pending = 0.0, list(events)
    for c in casing:
        while depth < c.shoe_md - 0.01:
            # Drill in stretches of at most 8 h, stopping at the next event depth.
            f = formation_at(depth + 0.1)
            rop = rng.uniform(*f["rop_m_hr"])
            target = min(c.shoe_md, depth + rop * 8)
            if pending and pending[0].md <= target:
                target = pending[0].md
            add("DRL", (target - depth) / rop, "drill", depth, target, hole=c.hole_in)
            depth = target
            while pending and pending[0].md <= depth + 1e-6:
                e = pending.pop(0)
                e.t_start = t
                code = {"loss": "NPT-LOSS", "stuck_pipe": "NPT-STUCK", "kick": "NPT-KICK"}[e.type]
                add(code, e.npt_hr, e.type, depth, npt=True, event_id=e.id)
                e.t_end = t
        add("CIRC", rng.uniform(2, 4), "circulate", depth)
        add("TRIP", depth / rng.uniform(250, 350), "pooh", depth)
        if c.kind == "production":
            add("LOG", rng.uniform(18, 30), "logging", depth)
        add("CSG", depth / rng.uniform(150, 220), "run_casing", depth, hole=c.od_in)
        add("CMT", rng.uniform(4, 6), "cement", depth, hole=c.od_in)
        if c.kind == "production":
            add("RIG", rng.uniform(8, 16), "rig_down", depth)
            break
        add("WOC", rng.uniform(8, 12), "woc", depth)
        add("BOP", rng.uniform(6, 10), "bop", depth)
        add("TRIP", depth / rng.uniform(300, 400), "rih", depth)
        if rng.random() < 0.25:
            add("NPT-RR", rng.uniform(2, 8), "rig_repair", depth, npt=True)
        nxt_f = formation_at(depth + 50)
        emw = round(max(nxt_f["mw_sg"]) + rng.uniform(0.2, 0.35), 2)
        test = "lot" if rng.random() < 0.5 else "fit"
        p = at_md(path, depth + 3)
        lots.append(LotFit(test, c.od_in, p.md, p.tvd, emw))
        add("TEST", rng.uniform(2, 4), test, depth, depth + 3, hole=c.od_in)
        depth += 3
    return acts


def _days(acts: list[Activity], events: list[Event], formation_at: Any, td: float) -> list[Day]:
    """Cut the timeline at midnights into daily reports whose rows sum to 24 h."""
    start = acts[0].start
    end = datetime.combine(acts[-1].end.date() + timedelta(days=1), datetime.min.time())
    last = acts[-1]
    acts = acts + [
        Activity(last.end, end, "RIG", False, "released", last.depth_end, last.depth_end)
    ]
    days, day0, n = [], start, 1
    while day0 < end:
        day1 = day0 + timedelta(days=1)
        rows = []
        for a in acts:
            s, e = max(a.start, day0), min(a.end, day1)
            if s >= e:
                continue
            span = (a.end - a.start).total_seconds()

            def depth_at(x: datetime, a: Activity = a, span: float = span) -> float:
                f = (x - a.start).total_seconds() / span if span else 1.0
                return a.depth_start + f * (a.depth_end - a.depth_start)

            rows.append(
                Activity(
                    s, e, a.code, a.npt, a.kind, depth_at(s), depth_at(e), a.hole_in, a.event_id
                )
            )
        d_end = rows[-1].depth_end
        mw = round(sum(formation_at(max(d_end - 1, 0.1))["mw_sg"]) / 2, 2)
        ev = sorted({r.event_id for r in rows if r.event_id})
        days.append(Day(n, day0.date(), rows[0].depth_start, d_end, mw, rows, ev))
        day0, n = day1, n + 1
    return days
