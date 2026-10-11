"""Coordinates: explicit datum transformations (pyproj), datum confidence, uncertainty radius.

Every position is stored in WGS 84 (EPSG:4326) with:

- `source_crs`: the CRS the report used, e.g. EPSG:4146 (Kalianpur 1975).
- `datum_confidence`:
    known     the report states the datum; transformed with the pinned operation.
    inferred  the report does not state it, but context does (vintage, operator practice).
    unknown   nothing says which; every candidate datum for the region is possible.
- `uncertainty_m`: radius, in metres, within which the true position lies with the published
  accuracy of the transformation(s):
    known     accuracy of the pinned operation (e.g. 22 m for EPSG:1156)
    inferred  that, plus the distance to where the point would be if the inference were wrong
    unknown   the spread of all candidate interpretations plus the worst operation accuracy

Transformations are pinned by EPSG operation code, never left to "best available": PROJ picks
by area of use and grid availability, which changes between machines and PROJ versions, and a
reprocessed well would silently move.
"""

from dataclasses import dataclass, field
from functools import cache
from math import atan2, degrees, hypot

from pyproj import Geod, Transformer

WGS84 = "EPSG:4326"
GEOD = Geod(ellps="WGS84")

# source CRS -> (EPSG operation code, note). Kalianpur 1937's published operations are for
# Bangladesh and Myanmar; 1155 (Bangladesh) is the nearest to Assam and is used outside its area.
OPERATIONS: dict[str, tuple[int, str]] = {
    "EPSG:4146": (1156, "Kalianpur 1975 to WGS 84 (1), India mainland, 22 m"),
    "EPSG:4144": (1155, "Kalianpur 1937 to WGS 84 (1), Bangladesh, 18 m; used outside its area"),
}
# Datums a report from this region may silently be in, when it does not say.
CANDIDATES: dict[str, tuple[str, ...]] = {
    "india": (WGS84, "EPSG:4146", "EPSG:4144"),
}
NAMES = {
    "WGS84": WGS84,
    "WGS 84": WGS84,
    "Kalianpur1975": "EPSG:4146",
    "Kalianpur 1975": "EPSG:4146",
    "Kalianpur1937": "EPSG:4144",
    "Kalianpur 1937": "EPSG:4144",
}
OUTSIDE_AREA_PENALTY_M = 10.0


@dataclass(frozen=True)
class GeoPosition:
    lat: float  # WGS 84
    lon: float
    source_crs: str
    datum_confidence: str  # known | inferred | unknown
    uncertainty_m: float
    operation: str
    notes: tuple[str, ...] = field(default=())


@cache
def transformer(source_crs: str) -> Transformer:
    """Pinned operation from `source_crs` to WGS 84 (lat, lon order in and out)."""
    code, _ = OPERATIONS[source_crs]
    return Transformer.from_pipeline(f"urn:ogc:def:coordinateOperation:EPSG::{code}")


def accuracy_m(source_crs: str) -> float:
    if source_crs == WGS84:
        return 0.0
    extra = OUTSIDE_AREA_PENALTY_M if "outside its area" in OPERATIONS[source_crs][1] else 0.0
    return float(transformer(source_crs).accuracy) + extra


def _to_wgs84(lat: float, lon: float, source_crs: str) -> tuple[float, float]:
    if source_crs == WGS84:
        return lat, lon
    la, lo = transformer(source_crs).transform(lat, lon)
    return float(la), float(lo)


def from_wgs84(lat: float, lon: float, target_crs: str) -> tuple[float, float]:
    la, lo = transformer(target_crs).transform(lat, lon, direction="INVERSE")
    return float(la), float(lo)


def distance_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Geodesic distance on WGS 84 between (lat, lon) points."""
    return float(GEOD.inv(a[1], a[0], b[1], b[0])[2])


def to_wgs84(
    lat: float, lon: float, crs: str | None, confidence: str = "known", region: str = "india"
) -> GeoPosition:
    """Position in WGS 84 with its datum confidence and uncertainty radius.

    `crs` is the stated (known) or assumed (inferred) CRS; with confidence 'unknown' it is
    ignored and the first candidate of the region (WGS 84) is reported."""
    source = NAMES.get(crs or "", crs or WGS84)
    if confidence not in ("known", "inferred", "unknown"):
        raise ValueError(f"datum confidence must be known/inferred/unknown, not {confidence!r}")
    if confidence == "unknown":
        source = CANDIDATES[region][0]
    point = _to_wgs84(lat, lon, source)
    op = "none (already WGS 84)" if source == WGS84 else OPERATIONS[source][1]
    notes = []
    radius = accuracy_m(source)
    if confidence != "known":
        others = [c for c in CANDIDATES[region] if c != source]
        spread = max(distance_m(point, _to_wgs84(lat, lon, c)) for c in others)
        worst = max(accuracy_m(c) for c in CANDIDATES[region])
        radius = spread + max(radius, worst)
        notes.append(f"{confidence} datum: other candidates up to {spread:.0f} m away")
    return GeoPosition(point[0], point[1], source, confidence, round(radius, 1), op, tuple(notes))


def offset_to_lonlat(lon: float, lat: float, north: float, east: float) -> tuple[float, float]:
    """Surface (WGS 84) + survey offsets in metres (true-north azimuths) -> (lon, lat), along the
    geodesic. Grid-north surveys must be rotated by the grid convergence first."""
    dist = hypot(north, east)
    if dist == 0:
        return lon, lat
    lon2, lat2, _ = GEOD.fwd(lon, lat, degrees(atan2(east, north)), dist)
    return float(lon2), float(lat2)
