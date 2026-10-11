"""Hazard roadmap: statistics, and recovery of seeded hazard probabilities from synthetic wells."""

import random
from math import comb, isclose

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from api.intelligence.hazards import (
    ActiveFormation,
    OffsetEvent,
    OffsetWell,
    lookahead,
    posterior,
    roadmap,
)
from api.intelligence.stats import beta_cdf, beta_ppf, kish_n_eff, weighted_quantile


@given(st.integers(1, 30), st.integers(1, 30), st.floats(0.001, 0.999))
def test_beta_cdf_matches_binomial_identity(a: int, b: int, x: float) -> None:
    """For integer a, b: I_x(a, b) = P(Binomial(a + b - 1, x) >= a) (exact, independent)."""
    n = a + b - 1
    exact = sum(comb(n, j) * x**j * (1 - x) ** (n - j) for j in range(a, n + 1))
    assert isclose(beta_cdf(x, a, b), exact, abs_tol=1e-10)


@settings(max_examples=100)
@given(st.floats(0.3, 40), st.floats(0.3, 40), st.floats(0.01, 0.99))
def test_beta_ppf_inverts_cdf(a: float, b: float, q: float) -> None:
    assert isclose(beta_cdf(beta_ppf(q, a, b), a, b), q, abs_tol=1e-7)


def test_kish_and_weighted_quantile() -> None:
    assert kish_n_eff([1, 1, 1, 1]) == 4.0
    assert isclose(kish_n_eff([1.0, 0.5, 0.25]), 1.75**2 / 1.3125)
    assert weighted_quantile([0.0, 1.0], [1, 1], 0.5) == 0.5
    assert weighted_quantile([0.2], [3], 0.9) == 0.2


def test_weights_rescale_to_n_eff() -> None:
    """Twenty weakly similar wells must not count as twenty identical ones."""
    ys = [1] * 10 + [0] * 10
    _, _, n_full, a1, b1 = posterior(ys, [1.0] * 20, 0.5, 0.5)
    _, _, n_mixed, a2, b2 = posterior(ys, [1.0] * 2 + [0.05] * 18, 0.5, 0.5)
    assert n_full == 20 and isclose(a1 + b1, 21)
    # (1 + 1 + 18 * 0.05)^2 / (1 + 1 + 18 * 0.0025) = 2.9^2 / 2.045 = 4.11 wells, not 20
    assert isclose(n_mixed, 2.9**2 / 2.045) and isclose(a2 + b2 - 1, n_mixed)


def test_jeffreys_interval_coverage_by_simulation() -> None:
    """With equal weights and a Jeffreys prior, the 90 % interval covers the true p about 90 %
    of the time (checked over a grid of p and 400 replications each)."""
    rng = random.Random(4)
    for p in (0.05, 0.2, 0.5):
        hits = 0
        for _ in range(400):
            ys = [1 if rng.random() < p else 0 for _ in range(30)]
            _, (lo, hi), *_ = posterior(ys, [1.0] * 30, 0.5, 0.5)
            hits += lo <= p <= hi
        assert 0.84 <= hits / 400 <= 0.96, (p, hits / 400)


def _offsets(seed: int, n: int):  # type: ignore[no-untyped-def]
    """Synthetic Upper Assam wells: each formation x hazard is drawn with the probability in
    data/synthetic_assam/stratigraphy.toml, so the truth is known."""
    from synthetic_assam.model import build_well, load_config

    cfg = load_config()
    offs = []
    for i in range(n):
        w = build_well(i, seed, cfg)
        events = [
            OffsetEvent(
                k,
                e.type,
                e.md,
                e.formation,
                e.rel_pos,
                e.severity,
                [(m, None) for m in e.mitigations],
            )
            for k, e in enumerate(w.events)
        ]
        offs.append(
            OffsetWell(
                i,
                w.name,
                1.0,
                {t.formation: t.md for t in w.tops},
                [t.formation for t in w.tops],
                w.td_md,
                events,
                [(0.0, w.td_md)],
            )
        )
    return cfg, offs


def test_roadmap_recovers_seeded_probabilities() -> None:
    covered = total = 0
    for seed in (1, 2, 3, 4, 5):
        cfg, offs = _offsets(seed, 60)
        forms = [ActiveFormation(f["name"], None, None, None, None) for f in cfg["formation"]]
        for c in roadmap(forms, offs, ("loss", "stuck_pipe", "kick")):
            p = next(f for f in cfg["formation"] if f["name"] == c.formation)["p"][c.hazard]
            if c.evidence_state == "no_evidence":
                assert c.counts["not_reached"] == 60  # the Langpar: below every TD
                continue
            if p == 0:
                assert c.mean < 0.03 and c.counts["event"] == 0
                continue
            total += 1
            covered += c.ci90[0] <= p <= c.ci90[1]
            # Nothing may be far off: inside a 99.9 % interval with the same posterior.
            n = c.counts["event"] + c.counts["none"]
            k = c.counts["event"]
            assert (
                beta_ppf(0.0005, k + 0.5, n - k + 0.5)
                <= p
                <= beta_ppf(0.9995, k + 0.5, n - k + 0.5)
            )
    assert total >= 60 and covered / total >= 0.8, (covered, total)


def test_unknown_is_not_none() -> None:
    """Undocumented offsets are reported as unknown and are not evidence of no hazard."""
    tops = {"Barail": 2000.0, "Kopili": 2600.0}
    order = ["Barail", "Kopili"]
    loss = [OffsetEvent(1, "loss", 2010.0, "Barail", 0.02, 3)]
    wells = [
        OffsetWell(1, "A", 1.0, tops, order, 3000.0, loss, [(0.0, 3000.0)]),  # event
        OffsetWell(2, "B", 1.0, tops, order, 3000.0, [], [(0.0, 3000.0)]),  # documented: none
        OffsetWell(3, "C", 1.0, tops, order, 3000.0, [], []),  # undocumented: unknown
        OffsetWell(4, "D", 1.0, tops, order, 3000.0, [], [(2000.0, 2100.0)]),  # 17 %: unknown
        OffsetWell(5, "E", 1.0, {"Tipam": 1000.0}, ["Tipam"], 1500.0, [], [(0, 1500)]),
    ]
    af = [ActiveFormation("Barail", 2100.0, 2700.0, 2000.0, 2600.0)]
    (cell,) = roadmap(af, wells, ("loss",))
    assert cell.counts == {"event": 1, "none": 1, "unknown": 2, "not_reached": 1}
    assert cell.n_eff == 2  # only A and B are evidence
    assert cell.band_md == (2112.0, 2112.0)  # rel 0.02 of the active Barail


def test_lookahead_eta() -> None:
    af = [ActiveFormation("Barail", 2100.0, 2700.0, 2000.0, 2600.0)]
    wells = [
        OffsetWell(
            i,
            str(i),
            1.0,
            {"Barail": 2000.0},
            ["Barail"],
            2500.0,
            [OffsetEvent(i, "loss", 2000.0 + 50 * i, "Barail", 0.1 * i, 2)],
            [(0.0, 2500.0)],
        )
        for i in range(1, 4)
    ]
    cells = roadmap(af, wells, ("loss",))
    from datetime import datetime

    la = lookahead(cells, 1900.0, 12.5, "test", datetime(2026, 1, 1))
    (band,) = la.bands
    assert band["state"] == "ahead"
    assert isclose(band["eta_hours"], (band["band_md"][0] - 1900.0) / 12.5, abs_tol=0.05)
    assert lookahead(cells, 2800.0, 12.5, "t", None).bands[0]["state"] == "passed"


@pytest.mark.parametrize("weights", [[1.0] * 5, [0.2, 0.4, 0.6, 0.8, 1.0]])
def test_posterior_mean_within_interval(weights: list[float]) -> None:
    mean, (lo, hi), *_ = posterior([1, 0, 1, 0, 0], weights, 0.5, 0.5)
    assert lo < mean < hi
