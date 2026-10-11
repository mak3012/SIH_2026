"""Analogue scoring and program-linter rules, briefing verification, PDF."""

import json
from typing import Any

import pytest

from api.intelligence.analogues import DEFAULT_WEIGHTS, Active, Features, rank, score
from api.intelligence.hazards import Cell
from api.intelligence.linter import (
    BriefingDraft,
    EvidenceSet,
    Paragraph,
    Program,
    brief,
    lint,
    pdf,
    verify,
)

ACTIVE = Active(1, 10, "FB-N", 5, 2760.0, [13.375, 9.625, 7.0], "wbm", 2026)


def feat(i: int, formation_m: float | None, **kw: Any) -> Features:
    base = dict(
        well_id=i,
        well=f"W-{i}",
        block="B",
        fault_block="FB-N",
        spud_year=2016,
        surface_m=1000.0,
        formation_m=formation_m,
        top_tvdss=2740.0,
        casing_ods=[13.375, 9.625, 7.0],
        mud_types=["wbm"],
        data_quality=0.8,
    )
    return Features(**{**base, **kw})


def test_score_is_a_transparent_weighted_mean() -> None:
    s, factors = score(ACTIVE, feat(1, 1500.0), {})
    assert abs(sum(f.contribution for f in factors) - s) < 1e-3
    by = {f.name: f for f in factors}
    assert by["fault_block"].score == 1.0 and by["design"].score == 1.0
    assert abs(by["top_depth_delta"].score - 0.8187) < 1e-3  # exp(-20/100)
    # An unknown factor scores 0.5 and says so.
    _, factors = score(ACTIVE, feat(2, 1500.0, fault_block=None), {})
    fb = next(f for f in factors if f.name == "fault_block")
    assert fb.unknown and fb.score == 0.5


def test_rank_honours_pins_exclusions_and_radii() -> None:
    cands = [
        feat(1, 1000.0),
        feat(2, 1200.0),
        feat(3, 9000.0),
        feat(4, None),
        feat(5, 800.0, fault_block="FB-S"),
    ]
    r = rank(
        ACTIVE,
        cands,
        {"fault_block": 5.0},
        subsurface_m=5000,
        pinned={3},
        excluded={1},
        max_offsets=2,
    )
    got = {x.features.well_id: x for x in r}
    assert r[0].features.well_id == 3 and got[3].included  # pinned: first, outside radius
    assert not got[1].included and got[1].excluded
    assert got[2].included and got[5].included  # the two best remaining inside the radii
    assert not got[4].included and not got[4].within_radii  # no crossing point
    assert set(DEFAULT_WEIGHTS) == {f.name for f in got[2].factors}


def cell(f: str, h: str, mean: float, band: tuple[float, float] | None = None) -> Cell:
    return Cell(
        f,
        h,
        mean,
        (max(0, mean - 0.1), min(1, mean + 0.1)),
        10.0,
        (0.5, 0.5),
        {"event": 4, "none": 6, "unknown": 0, "not_reached": 0},
        "evidence",
        {3: 1.0},
        None,
        band,
        None,
        [],
        [],
    )


FORMS = {"Tipam": (1500.0, 2200.0), "Barail": (2200.0, 2900.0), "Kopili": (2900.0, 3300.0)}
EVIDENCE = [
    {
        "kind": "fit",
        "well": "W-1",
        "formation": "Tipam",
        "md": 2150.0,
        "value": 1.45,
        "unit": "sg",
        "source": {"document": "W-1 WCR", "page": 3},
    },
    {
        "kind": "loss",
        "well": "W-2",
        "formation": "Barail",
        "md": 2230.0,
        "value": 1.36,
        "unit": "sg",
        "source": {"document": "W-2 WCR", "page": 4},
    },
    {
        "kind": "kick",
        "well": "W-3",
        "formation": "Kopili",
        "md": 2950.0,
        "value": 1.34,
        "unit": "sg",
        "source": {"document": "W-3 DDR 12", "page": 1},
    },
]


def program(**over: Any) -> Program:
    p = {
        "wellbore_id": 1,
        "td_md": 3300.0,
        "ecd_margin_sg": 0.05,
        "sections": [
            {
                "name": "17-1/2 in surface",
                "md_top": 0,
                "md_base": 600,
                "hole_in": 17.5,
                "casing_od_in": 13.375,
                "mud_type": "wbm",
                "mw_sg_min": 1.05,
                "mw_sg_max": 1.10,
            },
            {
                "name": "12-1/4 in intermediate",
                "md_top": 600,
                "md_base": 2180,
                "hole_in": 12.25,
                "casing_od_in": 9.625,
                "mud_type": "wbm",
                "mw_sg_min": 1.12,
                "mw_sg_max": 1.20,
            },
            {
                "name": "8-1/2 in production",
                "md_top": 2180,
                "md_base": 3300,
                "hole_in": 8.5,
                "casing_od_in": 7.0,
                "mud_type": "wbm",
                "mw_sg_min": 1.30,
                "mw_sg_max": 1.42,
            },
        ],
        "cement": [{"casing_od_in": 7.0, "toc_md": 2150.0, "slurry_density_sg": 1.90}],
    }
    p.update(over)
    return Program(**p)


def test_lint_rules_fire_with_evidence() -> None:
    cells = [
        cell("Barail", "loss", 0.4),
        cell("Kopili", "kick", 0.2),
        cell("Kopili", "stuck_pipe", 0.3),
    ]
    fs = lint(program(), cells, EvidenceSet(EVIDENCE), FORMS)
    rules = {f.rule: f for f in fs}
    assert rules["L-MW-FRAC"].severity == "block"  # 1.47 ECD vs 1.45 FIT at the 9-5/8 shoe
    assert rules["L-MW-FRAC"].evidence == ["E1"]
    assert rules["L-SEAT-LOSS"].severity == "block" and rules["L-SEAT-LOSS"].evidence[0] == "E2"
    assert rules["L-MW-KICK"].numbers["required_sg"] == 1.39
    assert rules["L-MUD-STUCK"].severity == "warn"
    assert rules["L-CMT-DENSITY"].severity == "block"  # 1.90 slurry vs 1.45 FIT
    assert rules["L-CMT-TOC"].severity == "warn"  # 2150 is only 30 m above the 2180 shoe
    assert [f.severity for f in fs] == sorted(
        (f.severity for f in fs), key=["block", "warn", "info"].index
    )


def test_lint_structure_rules() -> None:
    bad = program(
        td_md=3500.0,
        sections=[
            {
                "name": "a",
                "md_top": 0,
                "md_base": 600,
                "hole_in": 12.25,
                "casing_od_in": 13.375,
                "mw_sg_min": 1.05,
                "mw_sg_max": 1.1,
            },
            {
                "name": "b",
                "md_top": 650,
                "md_base": 3300,
                "hole_in": 8.5,
                "casing_od_in": 9.75,
                "mw_sg_min": 1.1,
                "mw_sg_max": 1.2,
            },
        ],
        cement=[],
    )
    rules = {f.rule for f in lint(bad, [], EvidenceSet([]), FORMS)}
    assert {"L-CLEARANCE", "L-STD-CASING", "L-CONTIGUOUS", "L-TD"} <= rules


@pytest.fixture
def lint_result() -> tuple[list[Any], EvidenceSet, list[Cell]]:
    cells = [cell("Barail", "loss", 0.4, (2210.0, 2280.0))]
    ev = EvidenceSet(EVIDENCE)
    return lint(program(), cells, ev, FORMS), ev, cells


def test_verify_catches_invented_numbers_and_bad_citations(lint_result: Any) -> None:
    fs, ev, cells = lint_result
    good = BriefingDraft(
        title="t",
        paragraphs=[
            Paragraph(
                text="In the Barail, ECD 1.47 sg exceeds the 1.36 sg at which W-2 lost returns "
                "(loss probability 40%).",
                citations=["E2"],
            )
        ],
    )
    assert verify(good, fs, ev, cells, ["W-2"]) == []
    invented = BriefingDraft(
        title="t",
        paragraphs=[
            Paragraph(text="Losses start at 1.31 sg.", citations=["E2"]),
            Paragraph(text="The FIT was 1.45 sg.", citations=[]),
            Paragraph(text="See also the kick.", citations=["E9"]),
        ],
    )
    problems = verify(invented, fs, ev, cells, [])
    assert any("1.31" in p for p in problems)
    assert any("cites no evidence" in p for p in problems)
    # A number from the program itself needs no offset citation.
    toc = BriefingDraft(
        title="t",
        paragraphs=[
            Paragraph(
                text="Top of cement at 2150 m is less than 150 m above the shoe at 2180 m.",
                citations=[],
            )
        ],
    )
    assert verify(toc, fs, ev, cells, []) == []
    assert any("E9" in p for p in problems)


class FakeLLM:
    enabled = True

    class cfg:  # noqa: N801
        model = "fake-model"

    def __init__(self, replies: list[dict[str, Any]]) -> None:
        self.replies, self.prompts = replies, []

    def complete_json(
        self, name: str, variables: dict[str, Any], schema: Any, validate: Any = None, **_: Any
    ) -> Any:
        self.prompts.append(variables)
        return validate(self.replies.pop(0)) if self.replies else None


def test_brief_retries_then_verifies(lint_result: Any) -> None:
    fs, ev, cells = lint_result
    bad = {"title": "t", "paragraphs": [{"text": "Losses at 1.99 sg.", "citations": ["E2"]}]}
    ok = {
        "title": "t",
        "paragraphs": [{"text": "W-2 lost returns at 1.36 sg.", "citations": ["E2"]}],
    }
    llm = FakeLLM([bad, ok])
    b = brief(llm, "ACTIVE-1", fs, ev, cells, ["W-2"])
    assert b.status == "verified" and b.model == "fake-model"
    assert len(b.problems) == 2 and b.problems[1] == []
    assert "1.99" in llm.prompts[1]["feedback"]  # the retry is told what failed


def test_brief_falls_back_to_rule_text(lint_result: Any) -> None:
    fs, ev, cells = lint_result
    bad = {"title": "t", "paragraphs": [{"text": "Losses at 1.99 sg.", "citations": []}]}
    b = brief(FakeLLM([bad, bad]), "ACTIVE-1", fs, ev, cells, [])
    assert b.status == "fallback" and b.problems[-1] == []  # rule text passes its own checks
    assert len(b.draft.paragraphs) == len(fs)
    none = brief(None, "ACTIVE-1", fs, ev, cells, [])
    assert none.status == "fallback"
    data = pdf(none, fs, ev, "tester")
    assert data[:4] == b"%PDF" and len(data) > 2000
    assert json.dumps(none.json())


def test_protected_sizes_do_not_corrupt_numbers(lint_result: Any) -> None:
    """Regression: removing the casing size "7" must not turn "2150" or "2180" into garbage."""
    fs, ev, cells = lint_result
    toc = BriefingDraft(
        title="t",
        paragraphs=[
            Paragraph(
                text="The 7 in liner: top of cement at 2150 m, less than 150 m above the "
                "shoe at 2180 m.",
                citations=[],
            )
        ],
    )
    assert verify(toc, fs, ev, cells, ["7", "8-1/2 in production"]) == []
