"""Units, with pint, and an explicit log of every conversion applied to a fact.

Canonical units (what the database stores): length m, density sg (specific gravity), pressure
gradient as sg-equivalent (EMW), volume bbl where a column says so, else m3.

Drilling conventions made explicit here rather than left to a library default:
- ft is the international foot (0.3048 m). US survey feet differ by 2 ppm; say so if a source uses
  them (unit `survey_foot`).
- sg is relative to fresh water at 1000 kg/m3 (the oilfield convention; pure water at 4 °C is
  999.97 kg/m3, a 0.003 % difference).
- ppg is pounds per US gallon; pcf pounds per cubic foot.
- A pressure gradient (psi/ft, kPa/m) converts to a density through standard gravity
  (9.80665 m/s2): 1 sg = 0.433 psi/ft. The log says "via g0" when this happens.
- bbl is the oil barrel, 42 US gallons = 0.158987 m3.
"""

from dataclasses import asdict, dataclass, field
from typing import Any

import pint

ureg = pint.UnitRegistry(cache_folder=None)
ureg.define("sg = 1000 * kilogram / meter ** 3 = SG")
ureg.define("ppg = pound / gallon")
ureg.define("pcf = pound / foot ** 3")
ureg.define("bbl = oil_barrel")
PINT_VERSION = pint.__version__

# Printed spellings -> pint expressions.
ALIASES = {
    "ft": "foot",
    "feet": "foot",
    "'": "foot",
    "ftah": "foot",
    "m": "meter",
    "metre": "meter",
    "metres": "meter",
    "meters": "meter",
    "sg": "sg",
    "s.g.": "sg",
    "g/cc": "g/cm**3",
    "g/cm3": "g/cm**3",
    "kg/m3": "kg/m**3",
    "ppg": "ppg",
    "lb/gal": "ppg",
    "pcf": "pcf",
    "lb/ft3": "pcf",
    "psi/ft": "psi/foot",
    "kpa/m": "kPa/meter",
    "bar/m": "bar/meter",
    "bbl": "bbl",
    "bbls": "bbl",
    "m3": "meter**3",
    "in": "inch",
    '"': "inch",
}

DIMENSION_OF_CANONICAL = {"length": "meter", "density": "sg", "gradient": "sg", "volume": "bbl"}


def unit(name: str) -> pint.Unit:
    return ureg.Unit(ALIASES.get(name.strip().lower(), name))


@dataclass(frozen=True)
class Conversion:
    """One step applied to one value: enough to redo it by hand."""

    value_in: float
    unit_in: str
    value_out: float
    unit_out: str
    factor: float
    via: str
    library: str = f"pint {PINT_VERSION}"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ConversionLog:
    """Every conversion applied to one fact, in order. Stored with its source_ref."""

    steps: list[Conversion] = field(default_factory=list)

    def as_list(self) -> list[dict[str, Any]]:
        return [s.as_dict() for s in self.steps]


def _is_gradient(u: pint.Unit) -> bool:
    return ureg.Quantity(1, u).dimensionality == ureg.Quantity(1, "psi/foot").dimensionality


def convert(value: float, from_unit: str, to_unit: str, log: ConversionLog | None = None) -> float:
    """Convert and (optionally) record the step. Pressure gradients convert to and from
    densities via standard gravity; any other change of dimension is an error."""
    a, b = unit(from_unit), unit(to_unit)
    q = ureg.Quantity(value, a)
    via = f"{a:~P} -> {b:~P}"
    if _is_gradient(a) and not _is_gradient(b):
        q, via = q / ureg.standard_gravity, f"{via} via g0 = 9.80665 m/s2"
    elif _is_gradient(b) and not _is_gradient(a):
        q, via = q * ureg.standard_gravity, f"{via} via g0 = 9.80665 m/s2"
    out = float(q.to(b).magnitude)
    if log is not None:
        factor = out / value if value else convert(1.0, from_unit, to_unit)
        log.steps.append(Conversion(value, str(from_unit), out, str(to_unit), factor, via))
    return out


def to_canonical(
    value: float, printed_unit: str, kind: str, log: ConversionLog | None = None
) -> float:
    """Value in the canonical unit for its kind: length -> m, density/gradient -> sg,
    volume -> bbl. Identity conversions are not logged."""
    target = DIMENSION_OF_CANONICAL[kind]
    if unit(printed_unit) == unit(target):
        return float(value)
    return convert(value, printed_unit, target, log)
