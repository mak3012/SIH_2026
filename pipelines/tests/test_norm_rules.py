import pytest

from pipelines.extract import Value
from pipelines.norm import canon, same
from pipelines.rules import validate


@pytest.mark.parametrize(
    ("name", "printed", "unit", "want"),
    [
        ("td_md", "9,851 ft", None, 3002.585),
        ("tops[0].md", "2,634.5", "m", 2634.5),
        ("tops[0].md", "8,642", "ft", 2634.082),
        ("spud_date", "20/07/1988", None, "1988-07-20"),
        ("spud_date", "14-Mar-1996", None, "1996-03-14"),
        ("report_date", "1996-03-14", None, "1996-03-14"),
        ("latitude", "27°14'03.91\"N", None, 27.234419),
        ("latitude", "27.10342° N", None, 27.10342),
        ("coordinate_datum", "Kalianpur 1975", None, "Kalianpur1975"),
        ("casing[0].od_in", '13-3/8"', None, "13-3/8"),
        ("events[0].type", "Lost circulation", None, "loss"),
        ("events[0].volume_bbl", "-", None, None),
        ("trajectory_type", "Deviated (J-type)", None, "J"),
        ("time_breakdown[0].to", "24:00", None, "24:00"),
        ("well_name", "Well SA-01", None, "SA-01"),
        ("formation", "in Sylhet", None, "Sylhet"),
        (
            "mitigation",
            "Action: Pumped LCM pill; Reduced flow rate",
            None,
            ["Pumped LCM pill", "Reduced flow rate"],
        ),
    ],
)
def test_canon(name: str, printed: str, unit: str | None, want: object) -> None:
    got = canon(name, printed, unit)
    assert same(name, got, want), (got, want)


def test_depth_without_unit_is_an_error() -> None:
    with pytest.raises(ValueError):
        canon("td_md", "9,851", None)


def v(name: str, value: object) -> Value:
    return Value(name, value, str(value), 1, [0, 0, 1, 1], 1.0)


def test_ddr_hours_must_sum_to_24() -> None:
    vals = {
        f"time_breakdown[{i}].{k}": v(f"time_breakdown[{i}].{k}", x)
        for i, (f, t, h) in enumerate([("00:00", "08:00", 8.0), ("08:00", "20:00", 12.0)])
        for k, x in (("from", f), ("to", t), ("hours", h))
    }
    score, fails = validate(vals, {"doc_type": "ddr"})
    assert any(f.rule == "R-DDR-24H" for f in fails)
    assert score["time_breakdown[0].hours"] == 0.0


def test_monotonic_standard_sizes_and_td() -> None:
    vals = {
        "td_md": v("td_md", 3000.0),
        "tops[0].md": v("tops[0].md", 2000.0),
        "tops[1].md": v("tops[1].md", 1900.0),
        "casing[0].od_in": v("casing[0].od_in", "9-3/4"),
        "casing[0].shoe_md": v("casing[0].shoe_md", 3500.0),
        "casing[0].grade": v("casing[0].grade", "X99"),
    }
    score, fails = validate(vals, {"doc_type": "wcr"})
    rules = {f.rule for f in fails}
    assert {"R-MONOTONIC", "R-CASING-STD", "R-DEPTH-LE-TD", "R-GRADE"} <= rules
    assert score["casing[0].grade"] == 0.5  # soft rule
    assert score["tops[1].md"] == 0.0 and score["td_md"] == 1.0
