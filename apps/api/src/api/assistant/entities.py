"""Entity linking and intent, by rules.

Entities: well names (from the wells the caller can see), formations (names, synonyms,
abbreviations), hazards, casing sizes, shoe-test type, and which attribute is asked for. A
follow-up question inherits the previous turn's wells / formations / hazards when it names none
("and in SA-12?", "what about kicks?").

Intent:
- aggregate  how many, total, average, highest/lowest, most/least, which wells, list all;
- explain    why, what happened, how was it handled, cause, lessons;
- lookup     everything else (one value from one record).
"""

import re
from dataclasses import asdict, dataclass, field
from typing import Any

import psycopg

HAZARDS = {
    "loss": r"loss(es)?|lost (circulation|returns)|lost\b|losing|seepage|lcm",
    "kick": r"kicks?|influx|pit gain|shut[\s-]?in|well control",
    "stuck_pipe": r"stuck( pipe)?|differential(ly)? stick|jarr?(ed|ing)|fish(ing)?",
    "tight_hole": r"tight hole|overpull",
    "pack_off": r"pack[\s-]?off",
    "washout": r"wash[\s-]?out",
    "cement_issue": r"cement (issue|problem|failure)|poor cement",
}
ATTRIBUTES = {
    "shoe_test": r"\b(lot|fit|leak[\s-]?off|formation integrity|integrity test|shoe test|emw)\b",
    "shoe": r"\bshoes?\b|\bcasing (set|depth|point)\b|\bset (the )?casing\b",
    "casing": r"\bcasing\b|\bgrade\b|\bweight\b.{0,10}\b(lb|ppf|casing)\b",
    "top": r"\btops?\b|\bencounter(ed)?\b|\benter(ed)?\b|\bpenetrat\w+|\bwhere does\b.*\bstart",
    "mw": r"\bmud weight\b|\bmw\b|\bmud density\b",
    "ecd": r"\becd\b|\bequivalent circulating",
    "npt": r"\bnpt\b|\bhours? lost\b|\btime lost\b|\bnon[\s-]?productive|\bdowntime\b|\bhow long\b",
    "volume": r"\bbbl\b|\bbarrels?\b|\bvolume\b|\bhow much mud\b|\bpit gain\b",
    # "when" asks for a date only at the start ("When did..."), not in "the MW when it had losses"
    "date": r"^\s*when\b|\bdate\b|\bwhat day\b|\bwhich year\b|\bwhat time\b",
    "spud": r"\bspud",
    "td": r"\btotal depth\b|\btd\b|\bhow deep\b|\bfinal depth\b",
    "location": r"\bwhere is\b|\blocation\b|\bcoordinates?\b|\blatitude\b|\blongitude\b",
    "field": r"\bwhich field\b|\bfield\b|\bblock\b",
    "depth": r"\bdepth\b|\bat what md\b|\bhow deep\b|\bwhere\b|\bmd\b|\bat what\b",
    "cause": r"\bwhy\b|\bcause\w*\b|\breason\b",
    "mitigation": r"\bhow (was|were|did)\b.*\b(handled|cured|fixed|freed|controlled|solved)\b|"
    r"\bmitigation\w*\b|\bwhat (was|were) done\b|\bactions?\b|\bcured?\b|\bremed\w+",
    "severity": r"\bseverity\b|\bhow (bad|severe|serious)\b",
}
AGGREGATE = re.compile(
    r"\bhow many\b(?!\s+(hours?|barrels?|bbl|days?|metres?|meters?|feet|ft|m|tons?|minutes?))|"
    r"\bnumber of\b|\bcount\b|\btotal\b|\bin all\b|\bsum\b|\baverage\b|\bmean\b|"
    r"\b(highest|lowest|deepest|shallowest|largest|smallest|most|least|maximum|minimum|max|min|"
    r"heaviest|lightest|longest|shortest|biggest|worst)\b|\bwhich wells\b|\blist\b|\ball wells\b|"
    r"\bacross\b|\bevery well\b|\beach well\b|\bper well\b|\bcompare\b",
    re.I,
)
EXPLAIN = re.compile(
    r"\bwhy\b|\bexplain\b|\bwhat happened\b|\bhow (was|were|did)\b|\bcause\w*\b|\bdescribe\b|"
    r"\blessons?\b|\bwhat went wrong\b|\btell me about\b|\bsummar\w+\b|\bstory\b|"
    r"\b(voice|field) notes?\b|\bsaid\b|\bsays?\b",
    re.I,
)
# "9-5/8 in casing", "7 inch", '13 3/8"'; not "SA-16 in TVDSS".
SIZE = re.compile(
    r"(?<![\d.-])(\d{1,2})(?:[-\s](\d)/(\d{1,2}))?\s*"
    r"(?:inch(?:es)?\b|\"|''|in\.?(?=\s+(?:casing|liner|shoe|hole|string|csg|intermediate|"
    r"surface|production|conductor)))",
    re.I,
)
SIZE_BARE = re.compile(r"(?<![\d.])(\d{1,2})[-\s](\d)/(\d{1,2})(?![\d])")
# Something shaped like a well name (SA-07, DEMO-3, AB-12 ST1): an unknown one means "no such
# well in the records you can see", never "all wells".
WELL_LIKE = re.compile(r"\b[A-Za-z]{1,6}-\d{1,4}(?:\s?ST\d)?\b")
DEPTH = re.compile(r"(?<![\w.])(\d[\d,]*(?:\.\d+)?)\s*(m|metres?|meters?|ft|feet)\b", re.I)


@dataclass
class Entities:
    wells: list[str] = field(default_factory=list)
    formations: list[str] = field(default_factory=list)
    hazards: list[str] = field(default_factory=list)
    casing_in: list[float] = field(default_factory=list)
    attributes: list[str] = field(default_factory=list)
    depths_m: list[float] = field(default_factory=list)
    inherited: list[str] = field(default_factory=list)  # which came from the previous turn
    unknown_wells: list[str] = field(default_factory=list)  # well-like names not visible

    def any(self) -> bool:
        return bool(self.wells or self.formations or self.hazards or self.casing_in)

    def json(self) -> dict[str, Any]:
        return asdict(self)


class Lexicon:
    """Names the caller can see, loaded under their RLS scope."""

    def __init__(self, wells: list[str], formations: dict[str, list[str]]) -> None:
        self.wells = sorted(wells, key=len, reverse=True)
        self.formations = formations  # canonical name -> names to match (incl. itself)

    @classmethod
    def load(cls, conn: psycopg.Connection) -> "Lexicon":
        wells = [r[0] for r in conn.execute("SELECT name FROM well")]
        forms: dict[str, list[str]] = {}
        for name, syn, abbr in conn.execute("SELECT name, synonyms, abbreviations FROM formation"):
            forms.setdefault(name, []).extend([name, *(syn or []), *(abbr or [])])
        return cls(wells, forms)

    @staticmethod
    def _well_re(name: str) -> re.Pattern[str]:
        # "SA-07", "SA 07", "SA07", "sa-7" all mean SA-07
        m = re.fullmatch(r"([A-Za-z]+)[-\s]?0*(\d+)(.*)", name)
        if not m:
            return re.compile(rf"(?<![\w-]){re.escape(name)}(?![\w-])", re.I)
        prefix, num, rest = m.groups()
        return re.compile(
            rf"(?<![\w-]){re.escape(prefix)}[-\s]?0*{num}{re.escape(rest)}(?![\w-]|\s\d)", re.I
        )

    def parse(self, q: str) -> Entities:
        e = Entities()
        for w in self.wells:
            if self._well_re(w).search(q) and w not in e.wells:
                e.wells.append(w)
        for m in WELL_LIKE.finditer(q):
            if not any(self._well_re(w).fullmatch(m.group(0)) for w in self.wells):
                e.unknown_wells.append(m.group(0).upper())
        for f, names in self.formations.items():
            if any(re.search(rf"\b{re.escape(n)}\b", q, re.I) for n in names):
                e.formations.append(f)
        for h, pat in HAZARDS.items():
            if re.search(rf"\b({pat})", q, re.I):
                e.hazards.append(h)
        for m in SIZE.finditer(q):
            v = int(m[1]) + (int(m[2]) / int(m[3]) if m[2] else 0.0)
            if 4 <= v <= 30:
                e.casing_in.append(v)
        for m in SIZE_BARE.finditer(q):
            v = int(m[1]) + int(m[2]) / int(m[3])
            if v not in e.casing_in:
                e.casing_in.append(v)
        for a, pat in ATTRIBUTES.items():
            if re.search(pat, q, re.I):
                e.attributes.append(a)
        for m in DEPTH.finditer(q):
            v = float(m[1].replace(",", ""))
            e.depths_m.append(v * 0.3048 if m[2].lower().startswith(("ft", "feet")) else v)
        return e


FOLLOW_UP = re.compile(r"^\s*(and|what about|how about|same|also|then|for|in|now)\b", re.I)
ALL_WELLS = re.compile(r"\b(all|every|each|any|across|per)\s+wells?\b|\bacross\b", re.I)


def inherit(e: Entities, q: str, previous: dict[str, Any] | None) -> Entities:
    """Carry the previous turn's context into a follow-up ("and in SA-12?", "what about the
    Kopili?", "how many in total?"): a short question or one that starts like a follow-up
    keeps whatever it does not name itself."""
    if not previous or not (FOLLOW_UP.search(q) or len(q.split()) <= 6):
        return e
    for key in ("wells", "formations", "hazards", "casing_in", "attributes"):
        if key == "wells" and ALL_WELLS.search(q):
            continue
        if not getattr(e, key) and previous.get(key):
            setattr(e, key, list(previous[key]))
            e.inherited.append(key)
    return e


def intent(q: str) -> str:
    if AGGREGATE.search(q):
        return "aggregate"
    if EXPLAIN.search(q):
        return "explain"
    return "lookup"
