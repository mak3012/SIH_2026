"""Unit round trips and reference values; datum transforms against control points."""

from math import isclose

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pyproj import Transformer

from core.canonical.crs import OPERATIONS, distance_m, from_wgs84, to_wgs84, transformer
from core.canonical.units import ConversionLog, convert, to_canonical
from core.geodesy import kalianpur1975_to_wgs84

FAMILIES = {
    "length": ["ft", "m", "in"],
    "density": ["sg", "ppg", "kg/m3", "pcf", "g/cc"],
    "gradient_or_density": ["psi/ft", "kpa/m", "sg", "ppg"],
    "volume": ["bbl", "m3"],
}


@settings(max_examples=300)
@given(st.sampled_from(sorted(FAMILIES)), st.data(),
       st.floats(min_value=1e-6, max_value=1e7, allow_nan=False))  # fmt: skip
def test_round_trip(family: str, data: st.DataObject, value: float) -> None:
    a = data.draw(st.sampled_from(FAMILIES[family]))
    b = data.draw(st.sampled_from(FAMILIES[family]))
    assert isclose(convert(convert(value, a, b), b, a), value, rel_tol=1e-12)


@pytest.mark.parametrize(
    ("value", "a", "b", "want", "tol"),
    [
        (1, "ft", "m", 0.3048, 0),  # international foot, exact
        (1, "bbl", "m3", 0.158987294928, 1e-12),  # 42 US gal
        (1, "sg", "ppg", 8.345404, 1e-6),  # fresh water
        (1, "sg", "psi/ft", 0.4335275, 1e-7),  # via g0
        (1, "sg", "pcf", 62.42796, 1e-5),
        (10, "ppg", "psi/ft", 0.5194805, 1e-7),  # "0.052 x ppg" rule of thumb
    ],
)
def test_reference_values(value: float, a: str, b: str, want: float, tol: float) -> None:
    assert isclose(convert(value, a, b), want, abs_tol=tol)


def test_conversion_log_per_fact() -> None:
    log = ConversionLog()
    assert isclose(to_canonical(9851, "ft", "length", log), 3002.5848)
    assert to_canonical(2634.5, "m", "length", log) == 2634.5  # identity: not logged
    assert isclose(to_canonical(0.52, "psi/ft", "gradient", log), 1.19947, abs_tol=1e-5)
    steps = log.as_list()
    assert len(steps) == 2
    assert steps[0]["value_in"] == 9851 and steps[0]["factor"] == pytest.approx(0.3048)
    assert "via g0" in steps[1]["via"] and steps[1]["library"].startswith("pint")


def test_incompatible_units_raise() -> None:
    with pytest.raises(Exception, match="(?i)cannot convert|dimensional"):
        convert(1, "ft", "sg")


# ---------------------------------------------------------------- datums


def test_pinned_operation_parameters_are_epsg_1156() -> None:
    from pyproj.crs import CoordinateOperation

    code, _ = OPERATIONS["EPSG:4146"]
    op = CoordinateOperation.from_epsg(code)
    assert op.name == "Kalianpur 1975 to WGS 84 (1)"
    assert [p.value for p in op.params] == [295.0, 736.0, 257.0]
    assert transformer("EPSG:4146").accuracy == 22.0


@settings(max_examples=200, deadline=None)
@given(st.floats(min_value=8, max_value=35), st.floats(min_value=68, max_value=97))
def test_pinned_transform_matches_independent_helmert(lat: float, lon: float) -> None:
    """Control: pyproj's EPSG:1156 and core.geodesy's own geocentric-translation code (no
    pyproj) agree to 1e-9 deg (0.1 mm) anywhere in India."""
    p = to_wgs84(lat, lon, "Kalianpur1975", "known")
    la, lo = kalianpur1975_to_wgs84(lat, lon)
    assert isclose(p.lat, la, abs_tol=1e-9) and isclose(p.lon, lo, abs_tol=1e-9)
    back = from_wgs84(p.lat, p.lon, "EPSG:4146")
    # 2D Helmert drops the ellipsoidal height, so the round trip is exact to ~1 mm, not bits.
    assert isclose(back[0], lat, abs_tol=1e-7) and isclose(back[1], lon, abs_tol=1e-7)


def test_projection_control_point_snyder() -> None:
    """Snyder (1987), Map Projections - A Working Manual, USGS PP 1395, Transverse Mercator
    example: 40 30'N 73 30'W, Clarke 1866, central meridian 75 W, k0 0.9996 ->
    x = 127,106.5 m, y = 4,484,124.4 m. Checks the PROJ installation the CRS code relies on."""
    t = Transformer.from_pipeline(
        "+proj=tmerc +lat_0=0 +lon_0=-75 +k=0.9996 +x_0=0 +y_0=0 +ellps=clrk66"
    )
    x, y = t.transform(-73.5, 40.5)
    assert abs(x - 127106.5) < 0.1 and abs(y - 4484124.4) < 0.1


def test_datum_confidence_and_uncertainty() -> None:
    lat, lon = 27.1, 94.9  # Upper Assam
    known = to_wgs84(lat, lon, "Kalianpur 1975", "known")
    assert known.datum_confidence == "known" and known.uncertainty_m == 22.0
    wgs = to_wgs84(lat, lon, "WGS 84", "known")
    assert wgs.uncertainty_m == 0.0 and (wgs.lat, wgs.lon) == (lat, lon)
    shift = distance_m((known.lat, known.lon), (lat, lon))
    assert 300 < shift < 700  # the datum difference is hundreds of metres here
    inferred = to_wgs84(lat, lon, "Kalianpur 1975", "inferred")
    unknown = to_wgs84(lat, lon, None, "unknown")
    assert inferred.uncertainty_m > shift and unknown.uncertainty_m >= shift
    assert unknown.source_crs == "EPSG:4326" and unknown.notes


def test_bad_confidence_rejected() -> None:
    with pytest.raises(ValueError):
        to_wgs84(1, 1, None, "maybe")
