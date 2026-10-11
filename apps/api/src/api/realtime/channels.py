"""Canonical real-time channels, and mnemonic / unit mapping from what feeds send.

Canonical units (what `rt_measurement` stores and every model reads):

    bit_md, hole_md, block_pos   m              hookload, wob   kN
    rpm                          rev/min        torque          kN.m
    spp                          bar            flow_in/out     L/min
    pit_vol                      m3             mw_in           sg
    rop                          m/h
"""

from dataclasses import dataclass, field
from datetime import datetime
from functools import cache

import pint

from core.canonical.units import ureg

# canonical name -> (unit, rt_measurement column or None for the jsonb `channels`)
CHANNELS: dict[str, tuple[str, str | None]] = {
    "bit_md": ("m", "bit_md"),
    "hole_md": ("m", "hole_md"),
    "block_pos": ("m", None),
    "hookload": ("kN", "hookload_kn"),
    "wob": ("kN", "wob_kn"),
    "rpm": ("rpm", "rpm"),
    "torque": ("kN.m", "torque_knm"),
    "spp": ("bar", "spp_bar"),
    "flow_in": ("L/min", "flow_in_lpm"),
    "flow_out": ("L/min", None),
    "pit_vol": ("m3", None),
    "mw_in": ("sg", "mw_in"),
    "rop": ("m/h", "rop_m_hr"),
}

# WITSML 1.4.1.1 log mnemonics seen in practice (Volve, Pason, common service companies).
WITSML_MNEMONICS: dict[str, str] = {
    **dict.fromkeys(["DBTM", "BITDEP", "BIT_DEPTH", "DMEA_BIT"], "bit_md"),
    **dict.fromkeys(["DMEA", "DEPTH", "HDTH", "HOLE_DEPTH"], "hole_md"),
    **dict.fromkeys(["BPOS", "BLKPOS", "BLOCK_POS"], "block_pos"),
    **dict.fromkeys(["HKLA", "HKLD", "HKL", "HOOKLOAD"], "hookload"),
    **dict.fromkeys(["WOBA", "SWOB", "WOB"], "wob"),
    **dict.fromkeys(["RPMA", "RPM", "SRPM", "TRPM"], "rpm"),
    **dict.fromkeys(["TQA", "STOR", "TORQUE", "TQ"], "torque"),
    **dict.fromkeys(["SPPA", "SPP", "STPPRS"], "spp"),
    **dict.fromkeys(["TFLO", "MFIA", "FLOWIN", "MFI"], "flow_in"),
    **dict.fromkeys(["MFOA", "FLOWOUT", "MFO"], "flow_out"),
    **dict.fromkeys(["TVA", "TVT", "PITVOL", "TV_ACT"], "pit_vol"),
    **dict.fromkeys(["MDIA", "MWIN", "MDI"], "mw_in"),
    **dict.fromkeys(["ROPA", "ROP", "ROP5"], "rop"),
}

# WITS level 0, record 01 (time-based drilling), item -> canonical. Units are fixed per rig,
# not sent: see wits0.WITS0Listener(units=...).
WITS0_ITEMS: dict[str, str] = {
    "0108": "bit_md",
    "0110": "hole_md",
    "0112": "block_pos",
    "0113": "rop",
    "0114": "hookload",
    "0116": "wob",
    "0118": "torque",
    "0120": "rpm",
    "0121": "spp",  # some rigs send SPP at 0121, most at 0122; both accepted
    "0122": "spp",
    "0127": "pit_vol",
    "0130": "flow_out",
    "0131": "flow_in",
    "0133": "mw_in",
}

# Unit spellings in feeds -> pint expressions understood by core.canonical.units.
UNIT_ALIASES = {
    "m": "meter",
    "ft": "foot",
    "kn": "kilonewton",
    "klbf": "kilopound_force",
    "1000 lbf": "kilopound_force",
    "klb": "kilopound_force",
    "lbf": "pound_force",
    "t": "metric_ton_force",
    "tonf": "metric_ton_force",
    "rpm": "revolutions_per_minute",
    "c/min": "revolutions_per_minute",
    "kn.m": "kilonewton * meter",
    "knm": "kilonewton * meter",
    "n.m": "newton * meter",
    "kft.lbf": "1000 * foot * pound_force",
    "ft.lbf": "foot * pound_force",
    "bar": "bar",
    "kpa": "kilopascal",
    "psi": "psi",
    "mpa": "megapascal",
    "l/min": "liter / minute",
    "lpm": "liter / minute",
    "gal/min": "gallon / minute",
    "gpm": "gallon / minute",
    "m3/min": "meter ** 3 / minute",
    "m3/s": "meter ** 3 / second",
    "m3": "meter ** 3",
    "bbl": "oil_barrel",
    "g/cm3": "gram / centimeter ** 3",
    "kg/m3": "kilogram / meter ** 3",
    "sg": "sg",
    "ppg": "ppg",
    "lbm/galus": "ppg",
    "m/h": "meter / hour",
    "ft/h": "foot / hour",
}
CANONICAL_PINT = {
    "m": "meter",
    "kN": "kilonewton",
    "rpm": "revolutions_per_minute",
    "kN.m": "kilonewton * meter",
    "bar": "bar",
    "L/min": "liter / minute",
    "m3": "meter ** 3",
    "sg": "sg",
    "m/h": "meter / hour",
}


@cache
def factor(unit: str | None, channel: str) -> float:
    """Multiplier from a feed's unit to the channel's canonical unit (1 when unit is unknown or
    already canonical). Raises ValueError for a unit that cannot convert (wrong dimension)."""
    target = CANONICAL_PINT[CHANNELS[channel][0]]
    if not unit:
        return 1.0
    src = UNIT_ALIASES.get(unit.strip().lower())
    if src is None:
        raise ValueError(f"unknown unit {unit!r} for {channel}")
    try:
        return float(ureg.parse_expression(src).to(target).magnitude)
    except pint.DimensionalityError as e:
        raise ValueError(f"unit {unit!r} cannot measure {channel}") from e


@dataclass
class Sample:
    """One timestamp of one wellbore: canonical channel values (missing = absent)."""

    wellbore_id: int
    ts: datetime
    values: dict[str, float] = field(default_factory=dict)
