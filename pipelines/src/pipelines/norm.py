"""Printed strings -> canonical values (metres, sg, ISO dates, enum names).

Both extraction passes (template parser and LLM) return what is printed; this module is the one
place that interprets it, so the two passes can only disagree about what they read.
"""

import re
from datetime import date, datetime
from fractions import Fraction
from typing import Any

from core.canonical.units import ConversionLog, to_canonical

NUM = re.compile(r"[-+]?\d[\d,]*(?:\.\d+)?|[-+]?\.\d+")
DMS = re.compile(
    r"(\d{1,3})\s*[°º:]\s*(\d{1,2})\s*['′:]\s*(\d{1,2}(?:\.\d+)?)\s*[\"″]?\s*([NSEW])?", re.I
)
DATE_FORMATS = ("%d.%m.%Y", "%d-%b-%Y", "%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d %b %Y", "%d.%m.%y")

EVENT_WORDS = {
    "loss": ("lost circulation", "losses", "loss", "lost returns"),
    "stuck_pipe": ("stuck pipe", "pipe stuck", "stuck"),
    "kick": ("kick", "well control", "pit gain"),
}
DATUMS = {
    "wgs 84": "WGS84",
    "wgs84": "WGS84",
    "kalianpur 1975": "Kalianpur1975",
    "kalianpur1975": "Kalianpur1975",
    "kalianpur": "Kalianpur1975",
}
PROFILES = {"vertical": "vertical", "j": "J", "s": "S"}

DEPTH_FIELDS = {
    "md",
    "tvd",
    "tvdss",
    "td_md",
    "td_tvd",
    "datum_elevation",
    "ground_elevation",
    "shoe_md",
    "shoe_tvd",
    "depth_md",
    "progress",
    "north",
    "east",
}
SG_FIELDS = {"mw", "emw", "mud_weight"}
FLOAT_FIELDS = {
    "inc",
    "azi",
    "npt_hr",
    "hours",
    "total_hours",
    "npt_hours",
    "weight_ppf",
    "volume_bbl",
    "loss_bbl",
    "pit_gain_bbl",
}
DATE_FIELDS = {"date", "report_date", "spud_date", "completion_date"}


def leaf(name: str) -> str:
    """'tops[2].md' -> 'md'; 'time_breakdown[0].hours' -> 'hours'."""
    return re.sub(r"\[\d+\]", "", name).split(".")[-1]


def number(s: str) -> float:
    m = NUM.search(s)
    if not m:
        raise ValueError(f"no number in {s!r}")
    return float(m.group().replace(",", ""))


def unit_of(s: str, default: str | None) -> str | None:
    t = s.lower()
    if re.search(r"\d\s*(ft|feet|')\b|\bft\b|\(ft\)", t):
        return "ft"
    if re.search(r"\d\s*m\b|\bm\b|\(m\)|metres|meters", t):
        return "m"
    return default


def depth_m(s: str, unit: str | None, log: ConversionLog | None = None) -> float:
    v = number(s)
    u = unit_of(s, unit)
    if u is None:
        raise ValueError(f"unknown depth unit for {s!r}")
    return round(to_canonical(v, u, "length", log), 3)


# Density / gradient spellings seen on reports, and pint units for them (core.canonical.units).
DENSITY_UNITS = [(r"ppg|lb/gal|#/gal", "ppg"), (r"psi/ft", "psi/ft"), (r"kpa/m", "kpa/m"),
                 (r"kg/m3|kg/m\^?3", "kg/m3"), (r"g/cc|g/cm3|gm/cc", "g/cc"),
                 (r"pcf|lb/ft3", "pcf"), (r"\bsg\b|s\.g\.", "sg")]  # fmt: skip


def density_sg(s: str, log: ConversionLog | None = None) -> float:
    """'1.19 sg' / '10.2 ppg' / '0.53 psi/ft' / '1190 kg/m3' -> sg. A bare number is taken as
    sg when it is below 3 (no other unit gives drilling densities that small), else refused."""
    v = number(s)
    t = s.lower()
    for pat, u in DENSITY_UNITS:
        if re.search(pat, t):
            kind = "gradient" if u in ("psi/ft", "kpa/m") else "density"
            return round(to_canonical(v, u, kind, log), 4)
    if v < 3:
        return v
    raise ValueError(f"density {s!r} has no unit and is not plausibly sg")


def parse_date(s: str) -> str:
    t = s.strip().rstrip(".")
    for f in DATE_FORMATS:
        try:
            return datetime.strptime(t, f).date().isoformat()
        except ValueError:
            continue
    m = re.search(r"\d{1,4}[./-]\w{1,3}[./-]\d{2,4}", t)
    if m and m.group() != t:
        return parse_date(m.group())
    raise ValueError(f"unrecognised date {s!r}")


def coordinate(s: str) -> float:
    m = DMS.search(s)
    if m:
        d, mi, se, hemi = m.groups()
        v = int(d) + int(mi) / 60 + float(se) / 3600
    else:
        v = number(s)
        hemi = next((h for h in "NSEW" if re.search(rf"\b{h}\b", s.upper())), None)
    return round(-v if hemi and hemi.upper() in "SW" else v, 6)


def event_type(s: str) -> str:
    t = s.lower()
    for kind, words in EVENT_WORDS.items():
        if any(w in t for w in words):
            return kind
    raise ValueError(f"no event type in {s!r}")


def pipe_size(s: str) -> str:
    """'13-3/8"' / '13 3/8 in' -> '13-3/8'."""
    t = s.replace('"', "").replace("in", "").strip()
    m = re.match(r"(\d+)(?:[\s-]+(\d+/\d+))?$", t)
    if not m:
        raise ValueError(f"not a pipe size {s!r}")
    return m.group(1) + (f"-{m.group(2)}" if m.group(2) else "")


def size_inches(s: str) -> float:
    whole, _, frac = s.partition("-")
    return float(int(whole) + (Fraction(frac) if frac else 0))


def canon(name: str, printed: str, unit: str | None, log: ConversionLog | None = None) -> Any:
    """Canonical value of a printed field. Raises ValueError when it cannot be read. Unit
    conversions applied are appended to `log`."""
    key = leaf(name)
    if key == "mitigation":
        items = printed if isinstance(printed, list) else re.split(r"[;,]\s*", str(printed))
        return [re.sub(r"^action:\s*", "", str(i).strip(), flags=re.I) for i in items if i]
    s = printed.strip()
    if s.lower() in ("", "-", "—", "n/a", "na", "nil"):
        return None
    if key == "well_name":
        return re.sub(r"^well(\s+name)?[:\s]+", "", s, flags=re.I).strip()
    if key == "formation":
        return re.sub(r"^in\s+", "", s, flags=re.I).strip()
    if key in DEPTH_FIELDS:
        return depth_m(s, unit, log)
    if key in SG_FIELDS:
        return density_sg(s, log)
    if key in FLOAT_FIELDS:
        return number(s)
    if key in DATE_FIELDS:
        return parse_date(s)
    if key in ("latitude", "longitude"):
        return coordinate(s)
    if key == "report_no":
        return int(number(s))
    if key in ("od_in", "hole_in", "hole_size", "casing"):
        return pipe_size(s)
    if key == "coordinate_datum":
        v = DATUMS.get(re.sub(r"\s+", " ", s.lower()))
        if v is None:
            raise ValueError(f"unknown datum {s!r}")
        return v
    if key == "depth_reference":
        v = s.upper().split()[0].strip(":")
        if v not in ("KB", "RT", "DF", "GL", "MSL"):
            raise ValueError(f"unknown depth reference {s!r}")
        return v
    if key == "trajectory_type":
        t = s.lower()
        if "vertical" in t:
            return "vertical"
        m = re.search(r"\b([js])[- ]type|\(([js])", t)
        if not m:
            raise ValueError(f"unknown profile {s!r}")
        return PROFILES[(m.group(1) or m.group(2))]
    if key in ("type", "event_type") or key.startswith("remarks"):
        return event_type(s)
    if key == "test_type":
        t = s.lower()
        if t in ("lot", "fit"):
            return t
        raise ValueError(f"unknown test type {s!r}")
    if key in ("from", "to"):
        m = re.fullmatch(r"(\d{1,2})[:.](\d{2})", s)
        if not m or int(m.group(1)) > 24:
            raise ValueError(f"not a time {s!r}")
        return f"{int(m.group(1)):02d}:{m.group(2)}"
    return s


def same(name: str, a: Any, b: Any) -> bool:
    """Do two canonical values agree, within the precision reports print at?"""
    if a is None or b is None:
        return a is b
    key = leaf(name)
    if isinstance(a, float | int) and isinstance(b, float | int):
        tol = 0.16 if key in DEPTH_FIELDS else 1e-5 if key in ("latitude", "longitude") else 0.006
        return abs(float(a) - float(b)) <= tol
    if isinstance(a, list) and isinstance(b, list):
        return sorted(map(_norm_text, a)) == sorted(map(_norm_text, b))
    return _norm_text(str(a)) == _norm_text(str(b))


def _norm_text(s: str) -> str:
    return re.sub(r"[^a-z0-9./-]+", " ", s.lower()).strip()


def as_date(v: str) -> date:
    return date.fromisoformat(v)
