"""Rig-state detection: deterministic rules on hookload, bit vs hole depth, block movement, flow,
RPM and SPP. First matching rule wins; the rule id is returned so every state is explainable.

Thresholds are per rig (`RigParams`): the defaults suit a land rig with a ~250 kN travelling
block. Calibrate `slips_hookload_kn` from the hookload seen in slips on the first connections.
"""

from dataclasses import dataclass

STATES = (
    "drilling_rotary",
    "drilling_slide",
    "reaming",
    "circulating",
    "tripping_in",
    "tripping_out",
    "connection",
    "static",
    "unknown",
)


@dataclass(frozen=True)
class RigParams:
    on_bottom_m: float = 0.5  # hole_md - bit_md below this = bit on bottom
    slips_hookload_kn: float = 300.0  # hookload below this = string in slips (block only)
    wob_min_kn: float = 5.0
    rpm_min: float = 10.0
    flow_min_lpm: float = 100.0
    spp_min_bar: float = 5.0  # pumps on also needs pressure: a flow-in sensor can stick
    move_min_m_s: float = 0.03  # |bit velocity| above this = pipe moving


# Ordered rules: (id, state, condition in words) — the table in docs/realtime.md.
RULES = [
    ("RS-0", "unknown", "bit or hole depth missing"),
    ("RS-1", "connection", "hookload < slips threshold (string in slips)"),
    ("RS-2", "drilling_rotary", "on bottom, WOB > min, pumps on, RPM > min"),
    ("RS-3", "drilling_slide", "on bottom, WOB > min, pumps on, RPM <= min"),
    ("RS-4", "reaming", "off bottom, pumps on, RPM > min, pipe moving"),
    ("RS-5", "tripping_in", "off bottom, pumps off, pipe moving down"),
    ("RS-6", "tripping_out", "off bottom, pumps off, pipe moving up"),
    ("RS-7", "tripping_in", "off bottom, pumps on, RPM <= min, pipe moving down (washing)"),
    ("RS-8", "tripping_out", "off bottom, pumps on, RPM <= min, pipe moving up (pumping out)"),
    ("RS-9", "circulating", "pumps on, pipe not moving (rotating or not)"),
    ("RS-10", "static", "pumps off, pipe not moving"),
]


def detect(v: dict[str, float], bit_velocity: float | None, p: RigParams) -> tuple[str, str]:
    """(state, rule id). `bit_velocity` in m/s, positive = moving down (from bit depth or
    block position over the last seconds); None counts as not moving."""
    bit, hole = v.get("bit_md"), v.get("hole_md")
    if bit is None or hole is None:
        return "unknown", "RS-0"
    hookload = v.get("hookload")
    if hookload is not None and hookload < p.slips_hookload_kn:
        return "connection", "RS-1"
    on_bottom = hole - bit < p.on_bottom_m
    pumps = v.get("flow_in", 0.0) > p.flow_min_lpm and v.get("spp", p.spp_min_bar) >= p.spp_min_bar
    rotating = v.get("rpm", 0.0) > p.rpm_min
    vel = bit_velocity or 0.0
    moving = abs(vel) > p.move_min_m_s
    if on_bottom and pumps and v.get("wob", 0.0) > p.wob_min_kn:
        return ("drilling_rotary", "RS-2") if rotating else ("drilling_slide", "RS-3")
    if not on_bottom and moving:
        if pumps and rotating:
            return "reaming", "RS-4"
        if not pumps:
            return ("tripping_in", "RS-5") if vel > 0 else ("tripping_out", "RS-6")
        return ("tripping_in", "RS-7") if vel > 0 else ("tripping_out", "RS-8")
    if pumps:
        return "circulating", "RS-9"
    return "static", "RS-10"
