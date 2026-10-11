"""Derived channels: MSE, corrected d-exponent, ECD (Herschel-Bulkley annular hydraulics),
torque & drag (soft-string), offset-calibrated friction factor.

Pure functions, SI inside. Pit trend, flow difference and the differential-sticking timer
need history and live in pipeline.py / features.py.
"""

from dataclasses import dataclass
from math import cos, log10, pi, sin, sqrt

import numpy as np

G0 = 9.80665
STEEL_SG = 7.85
IN = 0.0254
LB = 4.4482216152605  # N per lbf
FT = 0.3048


# ------------------------------------------------------------------ MSE, d-exponent


def mse_mpa(wob_kn: float, rpm: float, torque_knm: float, rop_m_h: float, bit_in: float) -> float:
    """Mechanical specific energy (Teale 1965) in MPa:
    MSE = WOB/A + 2 pi N T / (A ROP), N in rev/s, ROP in m/s."""
    a = pi * (bit_in * IN) ** 2 / 4
    rop = rop_m_h / 3600
    return (wob_kn * 1e3 / a + 2 * pi * (rpm / 60) * torque_knm * 1e3 / (a * rop)) / 1e6


def dxc(
    rop_m_h: float, rpm: float, wob_kn: float, bit_in: float, mud_sg: float, normal_sg: float = 1.03
) -> float | None:
    """Corrected d-exponent (Jorden & Shirley 1966; Rehm & McClendon 1971):
    d = log10(R / 60N) / log10(12 W / 10^6 D)  (R ft/h, N rpm, W lbf, D in),
    dxc = d * normal pore-fluid density / mud density. None outside the formula's domain."""
    r = rop_m_h / FT
    w = wob_kn * 1e3 / LB
    x, y = r / (60 * rpm), 12 * w / (1e6 * bit_in)
    if not (0 < x < 1 and 0 < y < 1):
        return None
    return log10(x) / log10(y) * normal_sg / mud_sg


# ------------------------------------------------------------------ Herschel-Bulkley hydraulics


@dataclass(frozen=True)
class Rheology:
    """Herschel-Bulkley tau = tau_y + K gamma^n (Pa, Pa.s^n)."""

    tau_y: float
    k: float
    n: float

    @classmethod
    def from_fann(cls, r600: float, r300: float, r6: float, r3: float) -> "Rheology":
        """API RP 13D style fit from viscometer dial readings (1 reading = 0.511 Pa at the
        bob, shear rate 1.703 x rpm)."""
        ty = max(2 * r3 - r6, 0.0)
        n = 3.32 * log10((r600 - ty) / (r300 - ty))
        k = (r300 - ty) / 511**n
        return cls(ty * 0.511, k * 0.511, n)

    @classmethod
    def from_pv_yp(cls, pv_cp: float, yp_lbf100ft2: float) -> "Rheology":
        """Only PV/YP known: Bingham plastic, the n = 1 case of Herschel-Bulkley."""
        return cls(yp_lbf100ft2 * 0.4788, pv_cp * 1e-3, 1.0)


DEFAULT_RHEOLOGY = Rheology(tau_y=4.0, k=0.25, n=0.75)


def _slot_flow(g: float, h: float, width: float, r: Rheology) -> float:
    """Flow rate (m3/s) of a HB fluid through a slot of gap h at pressure gradient g (Pa/m)."""
    tw = g * h / 2
    if tw <= r.tau_y:
        return 0.0
    n, k = r.n, r.k
    c = (k / g) * n / (n + 1)
    f_wall = ((tw - r.tau_y) / k) ** ((n + 1) / n)
    integral = (k / g) * n / (2 * n + 1) * ((tw - r.tau_y) / k) ** ((2 * n + 1) / n)
    return float(2 * width * c * (h / 2 * f_wall - integral))


def hb_gradient(q_lpm: float, d_hole_m: float, d_pipe_m: float, r: Rheology) -> float:
    """Laminar frictional pressure gradient (Pa/m) in a concentric annulus, narrow-slot
    approximation: gap (Dh - Dp)/2, width pi (Dh + Dp)/2. Solved by bisection on the gradient.

    ponytail: laminar only; annular Reynolds numbers in 8-1/2 in and larger hole at normal
    rates are laminar. Add a turbulent friction-factor branch for slim hole / high rates."""
    q = q_lpm / 60000
    h, width = (d_hole_m - d_pipe_m) / 2, pi * (d_hole_m + d_pipe_m) / 2
    if q <= 0 or h <= 0:
        return 0.0
    lo, hi = 2 * r.tau_y / h, 2 * r.tau_y / h + 1.0
    while _slot_flow(hi, h, width, r) < q:
        hi *= 2
    for _ in range(100):
        mid = sqrt(lo * hi) if lo > 0 else hi / 2
        if _slot_flow(mid, h, width, r) < q:
            lo = mid
        else:
            hi = mid
        if hi - lo <= 1e-9 * hi:
            break
    return (lo + hi) / 2


@dataclass(frozen=True)
class Annulus:
    """Annular sections from surface down: (length m, hole or casing ID m, pipe OD m)."""

    sections: tuple[tuple[float, float, float], ...]


def annulus_for(
    bit_md: float,
    hole_in: float,
    shoe_md: float | None,
    casing_id_in: float | None,
    pipe_od_in: float = 5.0,
    bha_od_in: float = 6.5,
    bha_len_m: float = 150.0,
) -> Annulus:
    """Cased hole above the last shoe, open hole below; BHA OD over its length above the bit."""
    secs: list[tuple[float, float, float]] = []
    bha_top = max(bit_md - bha_len_m, 0.0)
    marks = sorted({0.0, bha_top, bit_md, *([shoe_md] if shoe_md and shoe_md < bit_md else [])})
    for a, b in zip(marks, marks[1:], strict=False):
        mid = (a + b) / 2
        cased = shoe_md is not None and casing_id_in is not None and mid < shoe_md
        d_out = (casing_id_in if cased else hole_in) * IN  # type: ignore[operator]
        d_in = (bha_od_in if mid > bha_top else pipe_od_in) * IN
        secs.append((b - a, d_out, d_in))
    return Annulus(tuple(secs))


def ecd_sg(
    mw_sg: float, q_lpm: float, tvd_m: float, annulus: Annulus, r: Rheology
) -> tuple[float, float]:
    """(ECD at the bit in sg, annular friction pressure in bar):
    ECD = MW + dP_annulus / (rho_water g TVD)."""
    dp = sum(length * hb_gradient(q_lpm, dh, dp_, r) for length, dh, dp_ in annulus.sections)
    return mw_sg + dp / (1000 * G0 * tvd_m) if tvd_m > 0 else mw_sg, dp / 1e5


# ------------------------------------------------------------------ torque & drag


@dataclass(frozen=True)
class DrillString:
    """From the bit up: BHA, then drill pipe to surface. Air weights."""

    pipe_kn_m: float = 0.285  # 5 in 19.5 ppf with tool joints
    pipe_od_in: float = 5.0
    bha_kn_m: float = 1.2  # 6-1/2 in drill collars
    bha_od_in: float = 6.5
    bha_len_m: float = 150.0
    block_kn: float = 250.0  # travelling block, included in the hookload reading


DEFAULT_STRING = DrillString()


@dataclass(frozen=True)
class Trajectory:
    """Survey as arrays (md m, inc rad, azi rad unwrapped); vertical if empty."""

    md: np.ndarray
    inc: np.ndarray
    azi: np.ndarray

    @classmethod
    def from_stations(cls, stations: list[tuple[float, float, float]]) -> "Trajectory":
        """(md, inc deg, azi deg) rows."""
        if not stations:
            return cls(np.array([0.0]), np.array([0.0]), np.array([0.0]))
        a = np.array(sorted(stations), dtype=float)
        return cls(a[:, 0], np.radians(a[:, 1]), np.unwrap(np.radians(a[:, 2])))

    def at(self, md: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        return np.interp(md, self.md, self.inc), np.interp(md, self.md, self.azi)

    def tvd(self, md: float) -> float:
        """TVD by the average-angle method on 10 m steps (soft-string accuracy is enough)."""
        if md <= 0:
            return 0.0
        s = np.linspace(0, md, max(int(md / 10), 2))
        inc, _ = self.at(s)
        return float(np.sum(np.cos((inc[1:] + inc[:-1]) / 2) * np.diff(s)))


def torque_drag(
    traj: Trajectory,
    bit_md: float,
    mw_sg: float,
    mu: float,
    mode: str,
    string: DrillString = DEFAULT_STRING,
    wob_kn: float = 0.0,
    step_m: float = 30.0,
) -> tuple[float, float]:
    """(hookload kN incl. block, surface torque kN.m from string friction) by the Johancsik
    (1984) soft-string model, integrated from the bit up in `step_m` elements.

    mode: 'pickup' (tripping out), 'slackoff' (tripping in), 'rotating' (rotating off bottom
    or drilling with `wob_kn`: no axial drag, torque = sum mu N r)."""
    if bit_md <= 0:
        return string.block_kn, 0.0
    bf = 1 - mw_sg / STEEL_SG
    nodes = np.unique(np.concatenate([np.arange(bit_md, 0, -step_m), [0.0]]))[::-1]
    inc, azi = traj.at(nodes)
    t, m = -wob_kn, 0.0
    sign = {"pickup": 1.0, "slackoff": -1.0, "rotating": 0.0}[mode]
    for i in range(len(nodes) - 1):
        bot, top = nodes[i], nodes[i + 1]
        ds = bot - top
        mid = (bot + top) / 2
        in_bha = mid > bit_md - string.bha_len_m
        w = (string.bha_kn_m if in_bha else string.pipe_kn_m) * bf * ds
        r = (string.bha_od_in if in_bha else string.pipe_od_in) * IN / 2
        th = (inc[i] + inc[i + 1]) / 2
        d_inc, d_azi = inc[i] - inc[i + 1], azi[i] - azi[i + 1]
        normal = sqrt((t * d_azi * sin(th)) ** 2 + (t * d_inc + w * sin(th)) ** 2)
        t += w * cos(th) + sign * mu * normal
        if mode == "rotating":
            m += mu * normal * r
    return t + string.block_kn, m


def calibrate_friction(
    traj: Trajectory,
    points: list[tuple[float, str, float, float]],
    string: DrillString = DEFAULT_STRING,
    lo: float = 0.05,
    hi: float = 0.6,
) -> float | None:
    """Friction factor minimising the squared hookload misfit over tripping points
    (bit_md, 'pickup'|'slackoff', measured hookload kN, mud sg), by golden-section search.
    None without points."""
    if not points:
        return None

    def cost(mu: float) -> float:
        return sum(
            (hl - torque_drag(traj, md, mw, mu, mode, string)[0]) ** 2
            for md, mode, hl, mw in points
        )

    gr = (sqrt(5) - 1) / 2
    a, b = lo, hi
    c, d = b - gr * (b - a), a + gr * (b - a)
    fc, fd = cost(c), cost(d)
    for _ in range(40):
        if fc < fd:
            b, d, fd = d, c, fc
            c = b - gr * (b - a)
            fc = cost(c)
        else:
            a, c, fc = c, d, fd
            d = a + gr * (b - a)
            fd = cost(d)
    return (a + b) / 2
