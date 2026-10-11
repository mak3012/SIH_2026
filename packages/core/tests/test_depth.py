from math import degrees, isclose, pi, radians, sin

from core.depth import Station, at_md, at_tvd, minimum_curvature, offset_lonlat, tvdss
from core.units import ft_to_m, m_to_ft


def test_vertical_well_tvd_equals_md() -> None:
    pos = minimum_curvature([Station(0, 0, 0), Station(1000, 0, 0)])
    assert isclose(pos[-1].tvd, 1000)
    assert isclose(pos[-1].north, 0, abs_tol=1e-9)


def test_quarter_circle_build() -> None:
    # 0 -> 90 deg over an arc of length L: radius R = 2L/pi, TVD gain = R, horizontal = R.
    arc = 100.0
    r = 2 * arc / pi
    pos = minimum_curvature([Station(0, 0, 0), Station(arc, 90, 0)])[-1]
    assert isclose(pos.tvd, r, rel_tol=1e-9)
    assert isclose(pos.north, r, rel_tol=1e-9)


def test_offset_lonlat_is_geodesic() -> None:
    # Independent of pyproj: WGS 84 meridian radius of curvature M(phi) = a(1-e2)/(1-e2 sin2)^1.5
    # gives the length of a small step north; at the equator a step east of d is d/a radians.
    a, f = 6378137.0, 1 / 298.257223563
    e2 = f * (2 - f)
    m = a * (1 - e2) / (1 - e2 * sin(radians(27.0)) ** 2) ** 1.5
    lon, lat = offset_lonlat(95.0, 27.0, north=100.0, east=0.0)
    assert isclose(lat - 27.0, degrees(100.0 / m), rel_tol=1e-4) and isclose(lon, 95.0)
    lon, _ = offset_lonlat(95.0, 0.0, north=0.0, east=100.0)
    assert isclose(lon - 95.0, degrees(100.0 / a), rel_tol=1e-6)


def test_tvdss_and_units() -> None:
    assert tvdss(2500, 30) == 2470
    assert isclose(m_to_ft(ft_to_m(1234.5)), 1234.5)


def test_at_tvd_and_at_md_invert_each_other() -> None:
    path = minimum_curvature([Station(0, 0, 0), Station(500, 0, 0), Station(1500, 40, 90)])
    p = at_tvd(path, 900)
    assert isclose(p.tvd, 900) and p.md > 900
    assert isclose(at_md(path, p.md).tvd, 900, rel_tol=1e-9)
