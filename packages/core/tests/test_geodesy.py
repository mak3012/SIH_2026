from math import isclose

from core.geodesy import kalianpur1975_to_wgs84, to_dms, wgs84_to_kalianpur1975


def test_round_trip_and_shift_size() -> None:
    lat, lon = 27.1, 94.9
    klat, klon = wgs84_to_kalianpur1975(lat, lon)
    back = kalianpur1975_to_wgs84(klat, klon)
    assert isclose(back[0], lat, abs_tol=1e-9) and isclose(back[1], lon, abs_tol=1e-9)
    # The datums differ by hundreds of metres in Assam: large enough to matter, not degrees.
    dlat_m, dlon_m = (klat - lat) * 111_000, (klon - lon) * 99_000
    assert 50 < (dlat_m**2 + dlon_m**2) ** 0.5 < 2000


def test_dms() -> None:
    assert to_dms(27.1034, "N", "S") == "27°06'12.24\"N"
    assert to_dms(-0.5, "N", "S") == "0°30'00.00\"S"
