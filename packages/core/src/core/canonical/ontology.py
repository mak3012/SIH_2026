"""Formation ontology and name matching: an LLM may suggest, a rule or a human confirms.

The ontology is a tree: basin -> sub-basin -> group -> formation -> member. Each unit has a
canonical name, synonyms (other spellings, older names) and abbreviations (BRL, TPM). Reports
write "Barail Fm.", "BARAIL SST", "Brl" and "Barail Coal Shale" for the same rock; the
database stores one formation id.

Matching a printed name:
1. exact, synonym or abbreviation match after normalisation -> method `exact` / `synonym` /
   `abbreviation`;
2. otherwise edit distance (Damerau-Levenshtein) to every name, synonym and abbreviation in
   the basin -> method `fuzzy`.

Confirming a suggestion (from the LLM, the fuzzy matcher, or anyone) is a rule:
- the candidate is in the ontology, in the expected basin;
- the name is close: exact/synonym/abbreviation, or edit distance <= max(1, len // 6);
- depth plausible: when a depth is given and the candidate has an expected TVDSS range, the depth
  lies inside it (with tolerance).
All three pass -> `confirmed` (by rule). Name close but depth implausible, or name not close
-> `needs_human`. Candidate not in the ontology/basin -> `rejected`. The LLM's own confidence
never confirms anything. Well names follow the same pattern (normalise, edit distance, same
block, depth <= TD).
"""

import re
from dataclasses import dataclass, field

GENERIC = {
    "FORMATION",
    "FM",
    "FMN",
    "FM.",
    "GROUP",
    "GP",
    "GRP",
    "MEMBER",
    "MBR",
    "SST",
    "SANDSTONE",
    "SH",
    "SHALE",
    "LST",
    "LIMESTONE",
    "CLAY",
    "COAL",
    "UNIT",
    "SERIES",
    "THE",
    "OF",
}
RANKS = ("basin", "sub_basin", "group", "formation", "member")


@dataclass
class Unit:
    id: str
    name: str
    rank: str
    parent: str | None = None
    synonyms: list[str] = field(default_factory=list)
    abbreviations: list[str] = field(default_factory=list)
    expected_tvdss: tuple[float, float] | None = None  # typical range in the basin, m

    def __post_init__(self) -> None:
        if self.rank not in RANKS:
            raise ValueError(f"rank must be one of {RANKS}")


def normalise(name: str) -> str:
    """'Barail Fm.' / 'BARAIL SST' / 'barail' -> 'BARAIL'. Keeps words that carry meaning."""
    words = re.findall(r"[A-Z0-9]+", name.upper().replace("&", " AND "))
    kept = [w for w in words if w not in GENERIC]
    return " ".join(kept or words)


def _rank_hint(printed: str) -> str:
    """'Barail Group' names the group; 'Barail' or 'Barail Fm' the formation of the same name."""
    words = set(re.findall(r"[A-Z]+", printed.upper()))
    if words & {"GROUP", "GP", "GRP", "SERIES"}:
        return "group"
    if words & {"MEMBER", "MBR"}:
        return "member"
    return "formation"


def edit_distance(a: str, b: str) -> int:
    """Damerau-Levenshtein (optimal string alignment): insert, delete, substitute, transpose."""
    d = [[0] * (len(b) + 1) for _ in range(len(a) + 1)]
    for i in range(len(a) + 1):
        d[i][0] = i
    for j in range(len(b) + 1):
        d[0][j] = j
    for i in range(1, len(a) + 1):
        for j in range(1, len(b) + 1):
            cost = a[i - 1] != b[j - 1]
            d[i][j] = min(d[i - 1][j] + 1, d[i][j - 1] + 1, d[i - 1][j - 1] + cost)
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                d[i][j] = min(d[i][j], d[i - 2][j - 2] + 1)
    return d[len(a)][len(b)]


def close_enough(a: str, b: str) -> bool:
    return edit_distance(a, b) <= max(1, min(len(a), len(b)) // 6)


@dataclass(frozen=True)
class Candidate:
    unit_id: str
    method: str  # exact | synonym | abbreviation | fuzzy
    distance: int


@dataclass(frozen=True)
class Decision:
    status: str  # confirmed | needs_human | rejected
    unit_id: str | None
    confirmed_by: str | None  # 'rule' | human name | None
    checks: dict[str, object]
    reason: str


class Ontology:
    def __init__(self, units: list[Unit]) -> None:
        self.units = {u.id: u for u in units}
        for u in units:
            if u.parent is not None and u.parent not in self.units:
                raise ValueError(f"{u.id}: unknown parent {u.parent}")

    def ancestors(self, unit_id: str) -> list[str]:
        out, cur = [], self.units[unit_id].parent
        while cur is not None:
            out.append(cur)
            cur = self.units[cur].parent
        return out

    def in_basin(self, unit_id: str, basin: str | None) -> bool:
        return basin is None or basin == unit_id or basin in self.ancestors(unit_id)

    def formations(self, basin: str | None = None) -> list[Unit]:
        return [
            u
            for u in self.units.values()
            if u.rank in ("formation", "member", "group") and self.in_basin(u.id, basin)
        ]

    def candidates(self, printed: str, basin: str | None = None, limit: int = 3) -> list[Candidate]:
        """Best matches for a printed name, closest first."""
        raw = printed.strip().upper().rstrip(".")
        key = normalise(printed)
        found: dict[str, Candidate] = {}
        for u in self.formations(basin):
            if key == normalise(u.name):
                found[u.id] = Candidate(u.id, "exact", 0)
                continue
            if any(key == normalise(s) for s in u.synonyms):
                found[u.id] = Candidate(u.id, "synonym", 0)
                continue
            if any(raw == a.upper() for a in u.abbreviations):
                found[u.id] = Candidate(u.id, "abbreviation", 0)
                continue
            names = [normalise(u.name), *(normalise(s) for s in u.synonyms)]
            dist = min(edit_distance(key, n) for n in names)
            found[u.id] = Candidate(u.id, "fuzzy", dist)
        rank = _rank_hint(printed)
        ranked = sorted(
            found.values(),
            key=lambda c: (c.method == "fuzzy", c.distance, self.units[c.unit_id].rank != rank),
        )
        return ranked[:limit]

    def confirm(
        self,
        printed: str,
        unit_id: str | None,
        *,
        basin: str | None = None,
        tvdss: float | None = None,
        tolerance_m: float = 150.0,
    ) -> Decision:
        """Rule check of a suggested match (from an LLM, the fuzzy matcher, or a person)."""
        if unit_id is None or unit_id not in self.units:
            return Decision(
                "rejected",
                None,
                None,
                {"in_ontology": False},
                "suggested unit is not in the ontology",
            )
        if not self.in_basin(unit_id, basin):
            return Decision(
                "rejected",
                unit_id,
                None,
                {"in_ontology": True, "in_basin": False},
                f"{unit_id} is not in basin {basin}",
            )
        u = self.units[unit_id]
        key = normalise(printed)
        names = [normalise(u.name), *(normalise(s) for s in u.synonyms)]
        exact = key in names or printed.strip().upper().rstrip(".") in (
            a.upper() for a in u.abbreviations
        )
        dist = min(edit_distance(key, n) for n in names)
        name_ok = exact or any(close_enough(key, n) for n in names)
        depth_ok: bool | None = None
        if tvdss is not None and u.expected_tvdss is not None:
            lo, hi = u.expected_tvdss
            depth_ok = lo - tolerance_m <= tvdss <= hi + tolerance_m
        checks: dict[str, object] = {
            "in_ontology": True,
            "in_basin": True,
            "exact": exact,
            "edit_distance": dist,
            "name_close": name_ok,
            "depth_plausible": depth_ok,
        }
        if name_ok and depth_ok is not False:
            return Decision("confirmed", unit_id, "rule", checks, "name and depth consistent")
        why = "name not close enough" if not name_ok else "depth outside the formation's range"
        return Decision("needs_human", unit_id, None, checks, why)

    def resolve(
        self,
        printed: str,
        *,
        basin: str | None = None,
        tvdss: float | None = None,
        llm_suggestion: str | None = None,
    ) -> Decision:
        """Rule matcher first; an LLM suggestion is only considered when the rules found
        nothing confirmable, and is itself checked by the same rule."""
        rule: Decision | None = None
        for c in self.candidates(printed, basin, limit=1):
            rule = self.confirm(printed, c.unit_id, basin=basin, tvdss=tvdss)
            if rule.status == "confirmed":
                return rule
        if llm_suggestion is not None:
            d = self.confirm(printed, llm_suggestion, basin=basin, tvdss=tvdss)
            if d.status == "confirmed":
                return Decision(
                    "confirmed", d.unit_id, "rule", {**d.checks, "suggested_by": "llm"}, d.reason
                )
            return Decision(
                "needs_human", d.unit_id, None, {**d.checks, "suggested_by": "llm"}, d.reason
            )
        if rule is None:
            return Decision("needs_human", None, None, {}, "no candidate in the ontology")
        return Decision(
            "needs_human", rule.unit_id, None, {**rule.checks, "suggested_by": "fuzzy"}, rule.reason
        )

    def human_confirm(
        self, decision: Decision, person: str, unit_id: str | None = None
    ) -> Decision:
        """A reviewer accepts (or overrides) a match; recorded as confirmed by that person."""
        uid = unit_id or decision.unit_id
        if uid is None or uid not in self.units:
            raise ValueError("a human confirmation needs a unit in the ontology")
        return Decision(
            "confirmed", uid, person, {**decision.checks, "human": True}, f"confirmed by {person}"
        )


def normalise_well(name: str) -> str:
    """'SA-01', 'SA 1', 'sa#001', 'SA-1 ' -> 'SA-1'."""
    m = re.match(r"^\s*([A-Za-z]+)[\s#_-]*0*(\d+)\s*([A-Za-z]*)\s*$", name)
    if not m:
        return re.sub(r"\s+", " ", name.strip().upper())
    return f"{m.group(1).upper()}-{int(m.group(2))}{m.group(3).upper()}"


@dataclass(frozen=True)
class WellRecord:
    id: int
    name: str
    block: str
    td_md: float | None = None


def match_well(
    printed: str,
    wells: list[WellRecord],
    *,
    block: str | None = None,
    depth_md: float | None = None,
    llm_suggestion: int | None = None,
) -> Decision:
    """Well-name match: normalised equality or edit distance <= 1, same block, depth <= TD."""
    key = normalise_well(printed)

    def check(w: WellRecord) -> tuple[bool, dict[str, object]]:
        dist = edit_distance(key, normalise_well(w.name))
        same_block = block is None or w.block == block
        depth_ok = depth_md is None or w.td_md is None or depth_md <= w.td_md + 1.0
        checks: dict[str, object] = {
            "edit_distance": dist,
            "same_block": same_block,
            "depth_plausible": depth_ok,
        }
        return dist <= 1 and same_block and depth_ok, checks

    ranked = sorted(wells, key=lambda w: edit_distance(key, normalise_well(w.name)))
    for w in ranked[:1]:
        ok, checks = check(w)
        if ok and checks["edit_distance"] == 0:
            return Decision("confirmed", str(w.id), "rule", checks, "same normalised name")
    if llm_suggestion is not None:
        suggested = next((x for x in wells if x.id == llm_suggestion), None)
        if suggested is None:
            return Decision("rejected", None, None, {}, "suggested well does not exist")
        ok, checks = check(suggested)
        checks["suggested_by"] = "llm"
        return Decision(
            "confirmed" if ok else "needs_human",
            str(suggested.id),
            "rule" if ok else None,
            checks,
            "rule checks passed" if ok else "rule checks failed",
        )
    if ranked:
        ok, checks = check(ranked[0])
        return Decision(
            "confirmed" if ok else "needs_human",
            str(ranked[0].id),
            "rule" if ok else None,
            checks,
            "edit distance 1, same block" if ok else "no confirmable match",
        )
    return Decision("needs_human", None, None, {}, "no wells to match")
