"""Minimum curvature against a published example and against exact circular arcs."""

from math import cos, degrees, isclose, sin, tan

from hypothesis import assume, given, settings
from hypothesis import strategies as st

from core.canonical.survey import (
    Position,
    Station,
    angles,
    dls,
    dogleg,
    md_at_tvd,
    minimum_curvature,
    position_at,
    station_at,
    tvd_at,
)


def test_published_example() -> None:
    """drillingformulas.com, 'Minimum Curvature Method': 3500 ft / 15 deg / 20 deg to
    3600 ft / 25 deg / 45 deg gives dogleg 0.22605 rad = 12.95 deg, dN 27.22, dE 19.45,
    dTVD 94.01 ft (the method is scale-free). The page prints RF = 1.00408, but its own dTVD
    needs 1.00428: dTVD = 50 * RF * (cos 15 + cos 25) = 93.612 * RF, and 93.612 * 1.00408 =
    93.99, not 94.01. We assert the value its results imply."""
    a, b = Station(3500, 15, 20), Station(3600, 25, 45)
    beta = dogleg(a, b)
    assert round(degrees(beta), 2) == 12.95
    assert round(beta, 5) == 0.22605
    assert round(2 / beta * tan(beta / 2), 5) == 1.00428
    p = minimum_curvature([a, b], tie_in=Position(3500, 3500, 0, 0))[-1]
    assert (round(p.north, 2), round(p.east, 2), round(p.tvd - 3500, 2)) == (27.22, 19.45, 94.01)


def test_quarter_circle_and_vertical() -> None:
    r = 2 * 100 / 3.141592653589793  # 0 -> 90 deg over 100 m: radius 2L/pi
    p = minimum_curvature([Station(0, 0, 0), Station(100, 90, 0)])[-1]
    assert isclose(p.tvd, r) and isclose(p.north, r)
    v = minimum_curvature([Station(0, 0, 0), Station(1000, 0, 0)])[-1]
    assert (v.tvd, v.north, v.east) == (1000, 0, 0)


def test_dls_per_30m() -> None:
    assert isclose(dls(Station(1000, 10, 0), Station(1030, 13, 0)), 3.0)


# ---------------------------------------------------------------- exact circular arcs


def _unit(v: tuple[float, float, float]) -> tuple[float, float, float]:
    n = sum(x * x for x in v) ** 0.5
    return (v[0] / n, v[1] / n, v[2] / n)


def arc(inc0: float, azi0: float, bend: tuple[float, float, float], k: float):  # type: ignore[no-untyped-def]
    """Exact circular arc from the origin with start direction (inc0, azi0), bending towards
    `bend` with curvature k (rad/m). Returns (position(s), tangent(s))."""
    from core.canonical.survey import tangent

    t0 = tangent(inc0, azi0)
    dot = sum(x * y for x, y in zip(bend, t0, strict=True))
    b = _unit(tuple(x - dot * y for x, y in zip(bend, t0, strict=True)))  # type: ignore[arg-type]

    def pos(s: float) -> tuple[float, float, float]:
        c1, c2 = sin(k * s) / k, (1 - cos(k * s)) / k
        return tuple(c1 * x + c2 * y for x, y in zip(t0, b, strict=True))  # type: ignore[return-value]

    def tan_(s: float) -> tuple[float, float, float]:
        return tuple(cos(k * s) * x + sin(k * s) * y for x, y in zip(t0, b, strict=True))  # type: ignore[return-value]

    return pos, tan_


angle = st.floats(min_value=0.5, max_value=89.5)
azimuth = st.floats(min_value=0, max_value=359.9)
vec = st.tuples(*[st.floats(min_value=-1, max_value=1)] * 3).filter(
    lambda v: sum(x * x for x in v) > 0.05
)
curvature = st.floats(min_value=1e-4, max_value=0.008)  # up to ~14 deg / 30 m
lengths = st.lists(st.floats(min_value=5, max_value=150), min_size=1, max_size=12)


@settings(max_examples=150, deadline=None)
@given(angle, azimuth, vec, curvature, lengths)
def test_minimum_curvature_is_exact_on_circular_arcs(inc0, azi0, bend, k, steps) -> None:  # type: ignore[no-untyped-def]
    pos, tan_ = arc(inc0, azi0, bend, k)
    t0 = tan_(0)
    dot = abs(sum(x * y for x, y in zip(_unit(bend), t0, strict=True)))
    assume(dot < 0.95)  # bend direction must not be (almost) along the hole
    s_values = [0.0]
    for step in steps:
        s_values.append(s_values[-1] + step)
    assume(all(k * (b - a) < 3.0 for a, b in zip(s_values, s_values[1:], strict=False)))
    stations = [Station(1000 + s, *angles(tan_(s))) for s in s_values]
    path = minimum_curvature(stations, tie_in=Position(1000, 0, 0, 0))
    for s, p in zip(s_values, path, strict=True):
        n, e, d = pos(s)
        assert isclose(p.north, n, abs_tol=1e-6) and isclose(p.east, e, abs_tol=1e-6)
        assert isclose(p.tvd, d, abs_tol=1e-6)


@settings(max_examples=150, deadline=None)
@given(angle, azimuth, vec, curvature, lengths, st.floats(min_value=0, max_value=1))
def test_interpolation_follows_the_arc(inc0, azi0, bend, k, steps, frac) -> None:  # type: ignore[no-untyped-def]
    pos, tan_ = arc(inc0, azi0, bend, k)
    assume(abs(sum(x * y for x, y in zip(_unit(bend), tan_(0), strict=True))) < 0.95)
    s_values = [0.0]
    for step in steps:
        s_values.append(s_values[-1] + step)
    assume(all(k * (b - a) < 3.0 for a, b in zip(s_values, s_values[1:], strict=False)))
    stations = [Station(s, *angles(tan_(s))) for s in s_values]
    path = minimum_curvature(stations, tie_in=Position(0, 0, 0, 0))
    s = frac * s_values[-1]
    p = position_at(stations, s, path)
    n, e, d = pos(s)
    assert isclose(p.north, n, abs_tol=1e-6) and isclose(p.east, e, abs_tol=1e-6)
    assert isclose(p.tvd, d, abs_tol=1e-6)
    inc, azi = angles(tan_(s))
    got = station_at(stations, s)
    assert isclose(got.inc, inc, abs_tol=1e-6)
    assert min(abs(got.azi - azi), 360 - abs(got.azi - azi)) < 1e-5 or inc < 1e-3


@settings(max_examples=100, deadline=None)
@given(st.floats(min_value=0, max_value=60), azimuth,
       st.lists(st.floats(min_value=10, max_value=100), min_size=2, max_size=10),
       st.floats(min_value=0.01, max_value=0.99))  # fmt: skip
def test_md_at_tvd_inverts_tvd_at(inc, azi, steps, frac) -> None:  # type: ignore[no-untyped-def]
    mds = [0.0]
    for s in steps:
        mds.append(mds[-1] + s)
    stations = [Station(0, 0, 0)] + [Station(m, inc * i / len(mds), azi) for i, m in
                                     enumerate(mds[1:], start=1)]  # fmt: skip
    md = frac * mds[-1]
    assert isclose(md_at_tvd(stations, tvd_at(stations, md)), md, abs_tol=1e-5)


def test_positions_at_stations_are_the_stations() -> None:
    stations = [Station(0, 0, 0), Station(500, 10, 45), Station(1200, 35, 60)]
    path = minimum_curvature(stations)
    for s, p in zip(stations, path, strict=True):
        assert position_at(stations, s.md, path) == p
