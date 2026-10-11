"""Fusion and alarm policy. Rules decide (ADR-0002); nothing here calls a model.

    posterior odds = prior odds x LR

The prior comes from the hazard roadmap cell at the bit's current formation, converted from
"the hazard happens somewhere in this formation" to "a given moment is inside an episode":

    p_window = p_formation x episode_h / formation_h      (capped at p_formation)

formation_h = hours the bit spends in the formation (thickness / rolling ROP, at least 1 h;
10 h when the formation's base is unknown);
episode_h = typical episode length (training labels cover whole episodes). The LR comes from the
calibrated detector (detectors.py), which never sees the prior.

Alarm policy (ISA-18.2 principles):
- tiers from the posterior: advisory >= 0.20, warning >= 0.50, critical >= 0.80;
- on-delay: a tier must hold for `on_delay_s` of data time before it is raised or escalated;
- deadband + off-delay: an alert returns to normal only after the posterior stays below
  `clear_ratio` x the advisory threshold for `off_delay_s`;
- deduplication: one alert per wellbore and hazard while active; a re-raise within `rearm_s`
  of returning to normal reopens the same alert instead of creating a new one;
- shelving: a shelved hazard's advisory and warning alerts are recorded (`suppressed =
  'shelved'`) but not delivered; critical alerts are never shelved;
- per-shift budget: beyond `advisory_budget` delivered advisories per wellbore per 12 h shift,
  further advisories are recorded as `suppressed = 'budget'`. Warnings and criticals are never
  budgeted.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

TIER_NAMES = {1: "advisory", 2: "warning", 3: "critical"}


@dataclass(frozen=True)
class AlarmPolicy:
    thresholds: tuple[float, float, float] = (0.20, 0.50, 0.80)
    on_delay_s: float = 15.0
    off_delay_s: float = 120.0
    clear_ratio: float = 0.5
    rearm_s: float = 900.0
    advisory_budget: int = 10
    shift_start_hours: tuple[int, int] = (6, 18)  # local
    utc_offset_min: int = 330  # IST
    episode_h: float = 0.25
    min_formation_h: float = 1.0
    unknown_formation_h: float = 10.0  # formation base not known (last top, no TD)
    default_p_formation: float = 0.10  # no roadmap: a stated, conservative default


def window_prior(p_formation: float, formation_h: float | None, policy: AlarmPolicy) -> float:
    hours = max(formation_h or policy.min_formation_h, policy.min_formation_h)
    return min(p_formation, p_formation * policy.episode_h / hours)


def posterior(prior: float, lr: float) -> float:
    return prior * lr / (prior * lr + 1 - prior)


def tier_of(p: float, policy: AlarmPolicy) -> int:
    return sum(p >= t for t in policy.thresholds)


@dataclass
class Action:
    kind: str  # raise | reopen | escalate | update | clear
    key: int
    hazard: str
    ts: datetime
    tier: int = 0
    prior: float = 0.0
    lr: float = 1.0
    posterior: float = 0.0
    suppressed: str | None = None
    shelf_id: int | None = None
    evidence: dict[str, Any] = field(default_factory=dict)

    @property
    def delivered(self) -> bool:
        return self.suppressed is None


@dataclass
class _Track:
    key: int | None = None
    tier: int = 0
    peak: float = 0.0
    suppressed: str | None = None
    cand_since: datetime | None = None
    cand_tier: int = 0
    below_since: datetime | None = None
    cleared: tuple[int, datetime] | None = None


class AlarmManager:
    """Alarm state for one wellbore. Feed it every evaluation; it returns what to write."""

    def __init__(self, policy: AlarmPolicy | None = None) -> None:
        self.policy = policy or AlarmPolicy()
        self.tracks: dict[str, _Track] = {}
        self.shelves: dict[str, tuple[int, datetime]] = {}  # hazard -> (shelf id, expires)
        self.budget: tuple[datetime, int] | None = None
        self._next = 0

    def shift_start(self, ts: datetime) -> datetime:
        tz = timezone(timedelta(minutes=self.policy.utc_offset_min))
        local = ts.astimezone(tz)
        starts = [
            local.replace(hour=h, minute=0, second=0, microsecond=0)
            for h in self.policy.shift_start_hours
        ]
        starts += [s - timedelta(days=1) for s in starts]
        return max(s for s in starts if s <= local)

    def _suppress(self, hazard: str, tier: int, ts: datetime) -> tuple[str | None, int | None]:
        shelf = self.shelves.get(hazard)
        if shelf and shelf[1] > ts and tier < 3:
            return "shelved", shelf[0]
        if tier == 1:
            start = self.shift_start(ts)
            if self.budget is None or self.budget[0] != start:
                self.budget = (start, 0)
            if self.budget[1] >= self.policy.advisory_budget:
                return "budget", None
            self.budget = (start, self.budget[1] + 1)
        return None, None

    def update(
        self,
        ts: datetime,
        hazard: str,
        prior: float,
        lr: float,
        evidence: dict[str, Any] | None = None,
    ) -> list[Action]:
        pol = self.policy
        tr = self.tracks.setdefault(hazard, _Track())
        post = posterior(prior, lr)
        tier = tier_of(post, pol)
        out: list[Action] = []
        mk: dict[str, Any] = {
            "hazard": hazard,
            "ts": ts,
            "prior": prior,
            "lr": lr,
            "posterior": post,
        }

        if tier > tr.tier:
            if tr.cand_since is None:
                tr.cand_since, tr.cand_tier = ts, tier
            tr.cand_tier = min(tr.cand_tier, tier)
            if (ts - tr.cand_since).total_seconds() >= pol.on_delay_s:
                new = tr.cand_tier
                sup, shelf = self._suppress(hazard, new, ts)
                if tr.key is not None:
                    kind = "escalate"
                elif tr.cleared and (ts - tr.cleared[1]).total_seconds() <= pol.rearm_s:
                    tr.key, kind = tr.cleared[0], "reopen"
                else:
                    self._next += 1
                    tr.key, kind = self._next, "raise"
                tr.tier, tr.suppressed, tr.cand_since = new, sup, None
                tr.peak = max(tr.peak if kind != "raise" else 0.0, post)
                tr.below_since = None
                out.append(
                    Action(
                        kind,
                        tr.key,
                        tier=new,
                        suppressed=sup,
                        shelf_id=shelf,
                        evidence=evidence or {},
                        **mk,
                    )
                )
                return out
        else:
            tr.cand_since = None

        if tr.key is None:
            return out
        if post < pol.thresholds[0] * pol.clear_ratio:
            tr.below_since = tr.below_since or ts
            if (ts - tr.below_since).total_seconds() >= pol.off_delay_s:
                out.append(Action("clear", tr.key, tier=tr.tier, **mk))
                tr.cleared = (tr.key, ts)
                tr.key, tr.tier, tr.peak, tr.below_since = None, 0, 0.0, None
        else:
            tr.below_since = None
            if post >= tr.peak + 0.05:
                tr.peak = post
                out.append(Action("update", tr.key, tier=tr.tier, evidence=evidence or {}, **mk))
        return out

    def adopt(self, hazard: str, tier: int, peak: float) -> int:
        """Continue an alert that is still open from before a restart (deduplication across
        restarts). Returns the local key to map to its database id."""
        self._next += 1
        self.tracks[hazard] = _Track(key=self._next, tier=tier, peak=peak)
        return self._next
