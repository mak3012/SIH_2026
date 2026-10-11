"""Hazard roadmap for an active wellbore, and time to each hazard band (lookahead).

For each formation along the planned trajectory and each hazard type, every selected offset
well is one of:

    event        it had that hazard in that formation
    none         the formation was drilled and its problems documented, with no such event
    unknown      drilled, but problems not documented there (no WCR problems table, no DDRs)
    not_reached  the offset never reached the formation

Only `event` and `none` are evidence. `unknown` is reported, never counted as "none".

Posterior, similarity-weighted Beta-Binomial:

    w'_j  = w_j * n_eff / sum(w)          weights rescaled to the Kish effective sample size
    n_eff = (sum w_j)^2 / sum w_j^2       over offsets with evidence
    alpha = a0 + sum w'_j y_j,  beta = b0 + sum w'_j (1 - y_j)
    mean  = alpha / (alpha + beta),  90 % interval = Beta quantiles 0.05 and 0.95

The prior (a0, b0) has strength `prior_strength` (default 2 wells) centred on the basin rate
m = (k + 0.5) / (n + 1) over every documented well in the database; with no basin data it is
Jeffreys (0.5, 0.5). Rescaling means twenty weakly similar wells cannot claim the certainty of
twenty identical ones.
"""

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Any

from api.intelligence.stats import beta_ppf, kish_n_eff, weighted_quantile

HAZARDS = (
    "loss",
    "kick",
    "stuck_pipe",
    "tight_hole",
    "pack_off",
    "torque_spike",
    "cement_issue",
    "washout",
    "fishing",
)
COVERAGE_MIN = 0.8  # share of a formation's drilled interval that must be documented for "none"
PRIOR_STRENGTH = 2.0


@dataclass
class OffsetEvent:
    id: int
    type: str
    md: float
    formation: str | None
    rel_pos: float | None
    severity: int | None
    mitigations: list[tuple[str, bool | None]] = field(default_factory=list)
    source: dict[str, Any] = field(default_factory=dict)  # document, page, source_ref_id


@dataclass
class OffsetWell:
    well_id: int
    well: str
    weight: float
    tops: dict[str, float]  # formation -> top MD
    top_order: list[str]  # formations in depth order
    td_md: float | None
    events: list[OffsetEvent]
    documented: list[tuple[float, float]]  # MD intervals with documented problems

    def interval(self, formation: str) -> tuple[float, float] | None:
        """Drilled MD interval of a formation (top to next top or TD); None if not reached."""
        if formation not in self.tops:
            return None
        top = self.tops[formation]
        deeper = [self.tops[f] for f in self.top_order if self.tops[f] > top]
        base = min(deeper) if deeper else (self.td_md if self.td_md is not None else top)
        if self.td_md is not None:
            base = min(base, self.td_md)
        return (top, base) if base > top else None


def status(o: OffsetWell, formation: str, hazard: str) -> tuple[str, list[OffsetEvent]]:
    iv = o.interval(formation)
    if iv is None:
        return "not_reached", []
    evs = [
        e
        for e in o.events
        if e.type == hazard and (e.formation == formation if e.formation else iv[0] <= e.md < iv[1])
    ]
    if evs:
        return "event", evs
    covered = _covered(o.documented, iv)
    return ("none", []) if covered >= COVERAGE_MIN else ("unknown", [])


def _covered(docs: list[tuple[float, float]], iv: tuple[float, float]) -> float:
    lo, hi = iv
    spans = sorted((max(lo, a), min(hi, b)) for a, b in docs if b > lo and a < hi)
    total, cur = 0.0, lo
    for a, b in spans:
        a = max(a, cur)
        if b > a:
            total += b - a
            cur = b
    return total / (hi - lo) if hi > lo else 0.0


@dataclass
class Cell:
    formation: str
    hazard: str
    mean: float
    ci90: tuple[float, float]
    n_eff: float
    prior: tuple[float, float]
    counts: dict[str, int]
    evidence_state: str  # evidence | no_evidence
    severity: dict[int, float]
    rel_position: dict[str, float] | None  # q10, q50, q90 inside the formation
    band_md: tuple[float, float] | None  # in the active well
    band_tvdss: tuple[float, float] | None
    mitigations: list[dict[str, Any]]
    evidence: list[dict[str, Any]]


def posterior(
    ys: list[int], ws: list[float], a0: float, b0: float
) -> tuple[float, tuple[float, float], float, float, float]:
    """(mean, (q05, q95), n_eff, alpha, beta)."""
    n_eff = kish_n_eff(ws)
    s = sum(ws)
    scale = n_eff / s if s > 0 else 0.0
    alpha = a0 + sum(w * scale * y for y, w in zip(ys, ws, strict=True))
    beta = b0 + sum(w * scale * (1 - y) for y, w in zip(ys, ws, strict=True))
    mean = alpha / (alpha + beta)
    return mean, (beta_ppf(0.05, alpha, beta), beta_ppf(0.95, alpha, beta)), n_eff, alpha, beta


def prior_for(rate: tuple[int, int] | None) -> tuple[float, float]:
    """(a0, b0) from a basin count (events, documented wells), or Jeffreys."""
    if rate is None or rate[1] == 0:
        return 0.5, 0.5
    m = (rate[0] + 0.5) / (rate[1] + 1)
    return PRIOR_STRENGTH * m, PRIOR_STRENGTH * (1 - m)


@dataclass
class ActiveFormation:
    formation: str
    top_md: float | None
    base_md: float | None
    top_tvdss: float | None
    base_tvdss: float | None


def roadmap(
    formations: list[ActiveFormation],
    offsets: list[OffsetWell],
    hazards: tuple[str, ...] = HAZARDS,
    basin_rates: dict[tuple[str, str], tuple[int, int]] | None = None,
) -> list[Cell]:
    cells = []
    for af in formations:
        for h in hazards:
            ys, ws = [], []
            counts = {"event": 0, "none": 0, "unknown": 0, "not_reached": 0}
            sev: dict[int, float] = {}
            rels, rel_w = [], []
            mits: dict[str, dict[str, float]] = {}
            evidence = []
            for o in offsets:
                st, evs = status(o, af.formation, h)
                counts[st] += 1
                if st in ("event", "none"):
                    ys.append(1 if st == "event" else 0)
                    ws.append(o.weight)
                for e in evs:
                    if e.severity is not None:
                        sev[e.severity] = sev.get(e.severity, 0.0) + o.weight
                    if e.rel_pos is not None:
                        rels.append(e.rel_pos)
                        rel_w.append(o.weight)
                    for action, eff in e.mitigations:
                        m = mits.setdefault(action, {"wells": 0.0, "effective": 0.0, "n": 0})
                        m["n"] += 1
                        m["wells"] += o.weight
                        m["effective"] += 1 if eff else 0
                    evidence.append(
                        {
                            "event_id": e.id,
                            "well": o.well,
                            "well_id": o.well_id,
                            "md": round(e.md, 1),
                            "severity": e.severity,
                            "rel_pos": e.rel_pos,
                            "weight": round(o.weight, 3),
                            **e.source,
                        }
                    )
            a0, b0 = prior_for((basin_rates or {}).get((af.formation, h)))
            mean, ci, n_eff, _, _ = posterior(ys, ws, a0, b0)
            total_sev = sum(sev.values())
            rel = None
            band_md = band_tvdss = None
            if rels:
                rel = {
                    q: round(weighted_quantile(rels, rel_w, v), 3)
                    for q, v in (("q10", 0.1), ("q50", 0.5), ("q90", 0.9))
                }
                band_md = _band(af.top_md, af.base_md, rel)
                band_tvdss = _band(af.top_tvdss, af.base_tvdss, rel)
            cells.append(
                Cell(
                    af.formation,
                    h,
                    round(mean, 4),
                    (round(ci[0], 4), round(ci[1], 4)),
                    round(n_eff, 2),
                    (round(a0, 3), round(b0, 3)),
                    counts,
                    "evidence" if ys else "no_evidence",
                    {k: round(v / total_sev, 3) for k, v in sorted(sev.items())}
                    if total_sev
                    else {},
                    rel,
                    band_md,
                    band_tvdss,
                    sorted(
                        (
                            {
                                "action": k,
                                "times_used": int(v["n"]),
                                "times_effective": int(v["effective"]),
                            }
                            for k, v in mits.items()
                        ),
                        key=lambda m: -m["times_used"],
                    ),
                    evidence,
                )
            )
    return cells


def _band(top: float | None, base: float | None, rel: dict[str, float]
          ) -> tuple[float, float] | None:  # fmt: skip
    """q10..q90 of the events' relative position, placed in the active well's formation."""
    if top is None or base is None:
        return None
    return round(top + rel["q10"] * (base - top), 1), round(top + rel["q90"] * (base - top), 1)


@dataclass
class Lookahead:
    current_md: float | None
    rate_m_per_hr: float | None
    rate_basis: str
    as_of: datetime | None
    bands: list[dict[str, Any]]


def lookahead(
    cells: list[Cell],
    current_md: float | None,
    rate: float | None,
    basis: str,
    as_of: datetime | None,
    min_mean: float = 0.1,
) -> Lookahead:
    """Hours and time until the bit reaches each hazard band (bands with posterior mean at or
    above `min_mean`), from the rolling rate of penetration."""
    bands = []
    for c in cells:
        if c.band_md is None or c.mean < min_mean:
            continue
        top, base = c.band_md
        entry: dict[str, Any] = {
            "formation": c.formation,
            "hazard": c.hazard,
            "mean": c.mean,
            "ci90": c.ci90,
            "band_md": c.band_md,
        }
        if current_md is None:
            entry["state"] = "no current depth"
        elif current_md >= base:
            entry["state"] = "passed"
        elif current_md >= top:
            entry["state"] = "in band"
        elif not rate or rate <= 0:
            entry.update(state="ahead", distance_m=round(top - current_md, 1), eta_hours=None)
        else:
            hours = (top - current_md) / rate
            entry.update(
                state="ahead",
                distance_m=round(top - current_md, 1),
                eta_hours=round(hours, 1),
                eta=(as_of + timedelta(hours=hours)).isoformat() if as_of else None,
            )
        bands.append(entry)
    bands.sort(key=lambda b: b["band_md"][0])
    return Lookahead(current_md, rate, basis, as_of, bands)


def cells_json(cells: list[Cell]) -> list[dict[str, Any]]:
    return [asdict(c) for c in cells]
