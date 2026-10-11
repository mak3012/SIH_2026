from math import isclose

import pytest
from hypothesis import given
from hypothesis import strategies as st

from core.canonical.datum import DatumType, DepthDatum, rebase, tvd_from_tvdss, tvdss
from core.canonical.ontology import (
    Ontology,
    Unit,
    WellRecord,
    edit_distance,
    match_well,
    normalise,
    normalise_well,
)
from core.canonical.position import Top, locate, project, rel_position
from core.canonical.reanchor import Hazard, pick, reproject
from core.canonical.survey import Station, md_at_tvd, minimum_curvature, tvd_at

ONT = Ontology([
    Unit("assam-arakan", "Assam-Arakan", "basin"),
    Unit("upper-assam", "Upper Assam Shelf", "sub_basin", "assam-arakan"),
    Unit("barail-gp", "Barail Group", "group", "upper-assam", ["Barail Series"], ["BRL GP"]),
    Unit("barail", "Barail", "formation", "barail-gp", ["Barail Coal Shale", "Coal Measures"],
         ["BRL"], (2200, 3300)),
    Unit("tipam", "Tipam", "formation", "upper-assam", ["Tipam Sandstone"], ["TPM"], (1500, 2700)),
    Unit("kopili", "Kopili", "formation", "upper-assam", ["Kopili Shale"], ["KPL"], (2800, 3800)),
    Unit("other-basin", "Other", "basin"),
    Unit("bokabil", "Bokabil", "formation", "other-basin"),
])  # fmt: skip


def test_normalise_and_edit_distance() -> None:
    assert normalise("Barail Fm.") == normalise("BARAIL SST") == "BARAIL"
    assert edit_distance("BARAIL", "BARIAL") == 1  # transposition counts once
    assert edit_distance("KOPILI", "KAPILI") == 1


@pytest.mark.parametrize(
    ("printed", "method"),
    [("Barail", "exact"), ("Barail Fm.", "exact"), ("coal measures", "synonym"),
     ("BRL", "abbreviation"),
     ("Barial", "fuzzy")],
)  # fmt: skip
def test_candidates(printed: str, method: str) -> None:
    best = ONT.candidates(printed, basin="upper-assam")[0]
    assert (best.unit_id, best.method) == ("barail", method)


def test_group_and_formation_of_the_same_name() -> None:
    assert ONT.candidates("Barail Group", basin="upper-assam")[0].unit_id == "barail-gp"
    assert ONT.candidates("Barail", basin="upper-assam")[0].unit_id == "barail"
    assert ONT.ancestors("barail") == ["barail-gp", "upper-assam", "assam-arakan"]


def test_rules_confirm_llm_only_suggests() -> None:
    assert ONT.resolve("Barial", basin="upper-assam", tvdss=2600).status == "confirmed"
    # Name close but depth far outside the Barail's range: a person must look.
    d = ONT.resolve("Barial", basin="upper-assam", tvdss=900)
    assert d.status == "needs_human" and d.checks["depth_plausible"] is False
    # An LLM suggestion that fails the rule is not accepted on the LLM's word.
    d = ONT.resolve("Upper sand unit", basin="upper-assam", tvdss=2000, llm_suggestion="tipam")
    assert d.status == "needs_human" and d.checks["suggested_by"] == "llm"
    # Outside the basin: rejected outright.
    assert ONT.confirm("Bokabil", "bokabil", basin="upper-assam").status == "rejected"
    # A human can confirm what the rules could not.
    h = ONT.human_confirm(d, "geologist.a", "tipam")
    assert (h.status, h.unit_id, h.confirmed_by) == ("confirmed", "tipam", "geologist.a")


def test_well_names() -> None:
    assert normalise_well("SA-01") == normalise_well("sa 1") == normalise_well("SA#001") == "SA-1"
    wells = [WellRecord(1, "SA-01", "SA-BLK-1", 3000), WellRecord(2, "SA-11", "SA-BLK-2", 3100)]
    assert match_well("SA 1", wells).status == "confirmed"
    assert match_well("SA-01", wells, depth_md=3500).status == "needs_human"  # below TD
    d = match_well("S.A. one", wells, llm_suggestion=2, block="SA-BLK-1")
    assert d.status == "needs_human" and d.checks["same_block"] is False


# ---------------------------------------------------------------- datums


@given(st.floats(-1000, 9000), st.floats(-100, 3000), st.floats(-100, 3000))
def test_rebase_and_tvdss_round_trip(depth: float, e1: float, e2: float) -> None:
    kb, gl = DepthDatum(DatumType.KB, e1), DepthDatum(DatumType.GL, e2)
    assert isclose(rebase(rebase(depth, kb, gl), gl, kb), depth, abs_tol=1e-9)
    assert isclose(tvd_from_tvdss(tvdss(depth, kb), kb), depth, abs_tol=1e-9)
    # Same point, same TVDSS, whichever datum the depth was given from.
    assert isclose(tvdss(depth, kb), tvdss(rebase(depth, kb, gl), gl), abs_tol=1e-9)


def test_msl_has_zero_elevation() -> None:
    with pytest.raises(ValueError):
        DepthDatum(DatumType.MSL, 5.0)


# ---------------------------------------------------------------- formation-relative


OFFSET = [Top("Tipam", 1900.0), Top("Barail", 2600.0), Top("Kopili", 3250.0)]
ACTIVE = [Top("Tipam", 2050.0), Top("Barail", 2760.0), Top("Kopili", 3480.0)]


def test_rel_position() -> None:
    assert rel_position(2600, 2600, 3250) == 0.0
    assert isclose(rel_position(2665, 2600, 3250), 0.1)
    iv, rel = locate(2665, OFFSET)
    assert iv is not None and iv.formation == "Barail" and isclose(rel or 0, 0.1)


def test_same_depth_wrong_rock_vs_same_formation() -> None:
    p = project(2665.0, OFFSET, ACTIVE)
    assert p.naive_formation == "Tipam"  # the same TVDSS in the active well is still Tipam
    assert p.formation == "Barail" and p.method == "relative"
    assert isclose(p.tvdss or 0, 2760 + 0.1 * (3480 - 2760))  # 2,832 m: 10 % into the Barail


def test_projection_md_and_edge_cases() -> None:
    datum = DepthDatum(DatumType.RT, 110.0)
    survey = [Station(0, 0, 0), Station(1000, 0, 0), Station(4000, 30, 90)]
    p = project(2665.0, OFFSET, ACTIVE, survey, datum)
    assert p.md is not None and isclose(tvd_at(survey, p.md), (p.tvdss or 0) + 110.0, abs_tol=1e-4)
    deepest = project(3300.0, OFFSET, ACTIVE)
    assert deepest.method == "below_top" and isclose(deepest.tvdss or 0, 3480 + 50)
    absent = project(2665.0, OFFSET, [Top("Tipam", 2050.0)])
    assert absent.method == "absent" and absent.tvdss is None
    assert project(1000.0, OFFSET, ACTIVE).method == "above_first_top"


def test_reanchor_moves_hazards_with_a_new_pick() -> None:
    datum = DepthDatum(DatumType.RT, 110.0)
    survey = [Station(0, 0, 0), Station(1000, 0, 0), Station(4500, 25, 120)]
    hz = [Hazard(1, "Barail", 0.0, 0.1, 2760.0, 2832.0), Hazard(2, "Laisong", 0.2, 0.3)]
    tops = pick(ACTIVE, "Barail", 2800.0)  # geologist picks the Barail 40 m deeper
    (moved, kept) = reproject(hz, tops, survey, datum)
    assert moved.after == (2800.0, 2800.0 + 0.1 * (3480 - 2800))
    assert moved.moved_m == 40.0
    assert moved.md[0] is not None and isclose(
        md_at_tvd(survey, 2800.0 + 110.0), moved.md[0], abs_tol=1e-6
    )
    assert kept.after == kept.before and kept.notes == ["kept: no top for its formation"]


def test_minimum_curvature_reexport() -> None:
    from core.depth import minimum_curvature as legacy

    s = [Station(0, 0, 0), Station(900, 12, 30)]
    assert legacy(s) == minimum_curvature(s)
