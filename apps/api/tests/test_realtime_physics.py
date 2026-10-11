"""Real-time building blocks without a database: units, parsers, rig state, physics, alarms."""

from datetime import UTC, datetime, timedelta
from math import pi

import pytest

from api.realtime import derived as dv
from api.realtime.channels import Sample, factor
from api.realtime.connectors import parse_logs, parse_wits0
from api.realtime.fusion import AlarmManager, AlarmPolicy, posterior, window_prior
from api.realtime.rigstate import RULES, RigParams, detect
from api.realtime.simulator import Simulator
from api.realtime.store import TYPED, row

T0 = datetime(2026, 3, 1, 6, 30, tzinfo=UTC)  # 12:00 IST: inside the day shift


def test_unit_factors() -> None:
    assert factor("ft", "bit_md") == pytest.approx(0.3048)
    assert factor("klbf", "hookload") == pytest.approx(4.44822, rel=1e-5)
    assert factor("psi", "spp") == pytest.approx(0.0689476, rel=1e-5)
    assert factor("ppg", "mw_in") == pytest.approx(0.119826, rel=1e-5)
    assert factor("gal/min", "flow_in") == pytest.approx(3.78541, rel=1e-5)
    with pytest.raises(ValueError):
        factor("ft", "spp")  # wrong dimension
    with pytest.raises(ValueError):
        factor("furlong/fortnight", "rop")


# ------------------------------------------------------------------ parsers


def test_wits0_frame() -> None:
    lines = ["0105260301", "0106063000", "0108 1500.5", "0110 1501.0", "0114 900", "0122 15000"]
    frame = "\r\n" + "\r\n".join([*lines, "9999 x"]) + "\r\n"
    ts, vals = parse_wits0(frame, {"spp": "kPa", "hookload": "kN", "bit_md": "m", "hole_md": "m"})
    assert ts == T0
    assert vals == {"bit_md": 1500.5, "hole_md": 1501.0, "hookload": 900.0, "spp": 150.0}


def test_simulator_witsml_round_trip() -> None:
    src = [
        (T0 + timedelta(seconds=5 * i), {"bit_md": 1000.0 + i, "spp": 150.0, "mw_in": 1.2})
        for i in range(5)
    ]
    sim = Simulator(iter(()), speed=600, rebase=False)
    sim.times, sim.rows = [t for t, _ in src], [v for _, v in src]
    rows = parse_logs(sim.log_xml(src[1][0]))
    assert [r[0] for r in rows] == [t for t, _ in src[2:]]  # strictly after the start index
    assert rows[0][1] == pytest.approx({"bit_md": 1002.0, "spp": 150.0, "mw_in": 1.2})
    with pytest.raises(ValueError):
        Simulator(iter(()), speed=601)


def test_witsml_imperial_units() -> None:
    xml = (
        '<logs xmlns="http://www.witsml.org/schemas/1series" version="1.4.1.1"><log uid="L">'
        "<indexType>date time</indexType><indexCurve>TIME</indexCurve><logData>"
        "<mnemonicList>TIME,DBTM,HKLA,SPPA,XYZ</mnemonicList><unitList>s,ft,klbf,psi,m</unitList>"
        "<data>2026-03-01T06:30:00Z,1000,200,2000,1</data><data>2026-03-01T06:30:05Z,-999.25,,,</data>"
        "</logData></log></logs>"
    )
    rows = parse_logs(xml)
    assert rows[0][1] == pytest.approx(
        {"bit_md": 304.8, "hookload": 889.644, "spp": 137.895}, rel=1e-4
    )
    assert rows[1][1] == {}  # null value and empty cells are absent, not zero


def test_store_row_keeps_absent_channels_null() -> None:
    r = row(Sample(1, T0, {"bit_md": 10.0, "pit_vol": 50.0}), "static", {"ecd": 1.3, "mse": 80.0})
    typed = dict(zip(TYPED, r[3:-1], strict=True))
    assert typed["bit_md"] == 10.0 and typed["spp_bar"] is None and typed["ecd"] == 1.3
    assert r[-1].obj == {"pit_vol": 50.0, "mse": 80.0}


# ------------------------------------------------------------------ rig state


@pytest.mark.parametrize(
    ("values", "vel", "state", "rule"),
    [
        ({"bit_md": 100.0}, 0.0, "unknown", "RS-0"),
        ({"bit_md": 1000, "hole_md": 1000, "hookload": 250}, 0.0, "connection", "RS-1"),
        ({"bit_md": 1000, "hole_md": 1000.1, "hookload": 800, "flow_in": 2000, "spp": 150, "wob": 80, "rpm": 120}, 0.004, "drilling_rotary", "RS-2"),  # noqa: E501
        ({"bit_md": 1000, "hole_md": 1000.1, "hookload": 800, "flow_in": 2000, "spp": 150, "wob": 80, "rpm": 0}, 0.004, "drilling_slide", "RS-3"),  # noqa: E501
        ({"bit_md": 990, "hole_md": 1000, "hookload": 800, "flow_in": 2000, "spp": 150, "rpm": 60}, -0.3, "reaming", "RS-4"),  # noqa: E501
        ({"bit_md": 900, "hole_md": 1000, "hookload": 700}, 0.4, "tripping_in", "RS-5"),
        ({"bit_md": 900, "hole_md": 1000, "hookload": 900}, -0.4, "tripping_out", "RS-6"),
        ({"bit_md": 900, "hole_md": 1000, "hookload": 900, "flow_in": 1500, "spp": 80}, -0.4, "tripping_out", "RS-8"),  # noqa: E501
        ({"bit_md": 995, "hole_md": 1000, "hookload": 800, "flow_in": 2000, "spp": 150, "rpm": 60}, 0.0, "circulating", "RS-9"),  # noqa: E501
        ({"bit_md": 995, "hole_md": 1000, "hookload": 800, "flow_in": 2000, "spp": 0}, 0.0, "static", "RS-10"),  # noqa: E501
    ],
)  # fmt: skip
def test_rig_state_rules(values: dict[str, float], vel: float, state: str, rule: str) -> None:
    assert detect(values, vel, RigParams()) == (state, rule)
    assert any(r[0] == rule and r[1] == state for r in RULES)


# ------------------------------------------------------------------ physics


def test_herschel_bulkley_newtonian_limit_matches_slot_formula() -> None:
    mu = 0.02
    g = dv.hb_gradient(2000, 8.5 * dv.IN, 5 * dv.IN, dv.Rheology(0, mu, 1.0))
    h, w = 1.75 * dv.IN, pi * 13.5 * dv.IN / 2
    assert g == pytest.approx(12 * mu * (2000 / 60000) / (w * h**3), rel=1e-6)


def test_yield_stress_and_ecd() -> None:
    bingham = dv.Rheology.from_pv_yp(20, 15)
    newtonian = dv.Rheology(0, 0.02, 1.0)
    assert dv.hb_gradient(2000, 0.2159, 0.127, bingham) > dv.hb_gradient(
        2000, 0.2159, 0.127, newtonian
    )
    fann = dv.Rheology.from_fann(60, 40, 8, 6)  # PV 20, YP 20
    assert fann.tau_y == pytest.approx(4 * 0.511) and 0 < fann.n < 1
    ann = dv.annulus_for(2500, 8.5, 1500, 8.681)
    assert [s[0] for s in ann.sections] == [1500, 850, 150]
    ecd, dp = dv.ecd_sg(1.2, 2000, 2400, ann, bingham)
    assert 1.2 < ecd < 1.4 and dp > 0
    assert dv.ecd_sg(1.2, 0, 2400, ann, bingham)[0] == 1.2


def test_mse_and_dxc() -> None:
    # Pure rotary term check: 2 pi N T / (A ROP) with N 2 rev/s, T 10 kN.m, ROP 36 m/h.
    a = pi * (8.5 * dv.IN) ** 2 / 4
    assert dv.mse_mpa(0, 120, 10, 36, 8.5) == pytest.approx(2 * pi * 2 * 1e4 / (a * 0.01) / 1e6)
    d = dv.dxc(15, 120, 100, 8.5, 1.03, normal_sg=1.03)
    assert d is not None and 1.0 < d < 2.0
    assert dv.dxc(15, 120, 100, 8.5, 1.5) < d  # heavier mud lowers dxc
    assert dv.dxc(0, 120, 100, 8.5, 1.2) is None


def test_soft_string_vertical_and_friction_recovered() -> None:
    vertical = dv.Trajectory.from_stations([])
    s = dv.DrillString()
    hl, tq = dv.torque_drag(vertical, 2500, 1.2, 0.3, "pickup", s)
    weight = ((2500 - s.bha_len_m) * s.pipe_kn_m + s.bha_len_m * s.bha_kn_m) * (1 - 1.2 / 7.85)
    assert hl == pytest.approx(weight + s.block_kn, rel=1e-6) and tq == 0  # no drag when vertical
    j = dv.Trajectory.from_stations([(0, 0, 0), (500, 0, 0), (1500, 40, 90), (3000, 40, 90)])
    up = dv.torque_drag(j, 2500, 1.2, 0.3, "pickup")[0]
    down = dv.torque_drag(j, 2500, 1.2, 0.3, "slackoff")[0]
    rot, torque = dv.torque_drag(j, 2500, 1.2, 0.3, "rotating")
    assert down < rot < up and torque > 0
    pts = [
        (md, "pickup", dv.torque_drag(j, md, 1.2, 0.27, "pickup")[0], 1.2)
        for md in (1200, 1800, 2400)
    ]
    pts += [
        (md, "slackoff", dv.torque_drag(j, md, 1.2, 0.27, "slackoff")[0], 1.2)
        for md in (1500, 2100)
    ]
    assert dv.calibrate_friction(j, pts) == pytest.approx(0.27, abs=0.005)
    assert dv.calibrate_friction(j, []) is None


# ------------------------------------------------------------------ fusion and alarm policy


def feed(
    m: AlarmManager, posts: list[float], prior: float = 0.01, start: datetime = T0, dt: float = 5
):
    """Drive the manager with posteriors (converted to LRs for the fixed prior)."""
    out = []
    for i, p in enumerate(posts):
        lr = (p / (1 - p)) / (prior / (1 - prior))
        out += [(i, a) for a in m.update(start + timedelta(seconds=dt * i), "kick", prior, lr)]
    return out


def test_posterior_odds_and_window_prior() -> None:
    assert posterior(0.01, 99) == pytest.approx(0.5)
    pol = AlarmPolicy()
    assert window_prior(0.4, 20, pol) == pytest.approx(0.4 * 0.25 / 20)
    assert window_prior(0.4, 0.1, pol) == pytest.approx(0.4 * 0.25 / 1.0)  # at least 1 h
    assert window_prior(0.4, None, pol) == pytest.approx(0.4 * 0.25)
    assert window_prior(0.1, 0.0001, AlarmPolicy(episode_h=5)) <= 0.1


def test_on_delay_suppresses_a_single_spike() -> None:
    m = AlarmManager()
    assert feed(m, [0.01, 0.9, 0.01, 0.01, 0.9, 0.01]) == []
    acts = feed(m, [0.6] * 5, start=T0 + timedelta(minutes=5))
    assert [(i, a.kind, a.tier) for i, a in acts] == [(3, "raise", 2)]  # held 15 s


def test_escalation_hysteresis_clear_and_dedup() -> None:
    m = AlarmManager()
    posts = [0.3] * 4 + [0.85] * 4 + [0.15] * 30 + [0.05] * 30
    acts = feed(m, posts)
    kinds = [(a.kind, a.tier) for _, a in acts]
    assert kinds[0] == ("raise", 1) and ("escalate", 3) in kinds
    # 0.15 sits inside the deadband (clear below 0.10): no clear, no new alert
    clear_at = [i for i, a in acts if a.kind == "clear"]
    assert clear_at and clear_at[0] >= 38 + 24  # 120 s below 0.10
    assert len({a.key for _, a in acts}) == 1
    again = feed(m, [0.6] * 5, start=T0 + timedelta(minutes=8))  # within rearm: same alert
    assert [(a.kind, a.key) for _, a in again] == [("reopen", acts[0][1].key)]
    later = feed(m, [0.05] * 30, start=T0 + timedelta(minutes=12))  # clears again
    later += feed(m, [0.6] * 5, start=T0 + timedelta(minutes=40))  # after rearm: new alert
    assert any(a.kind == "raise" and a.key != acts[0][1].key for _, a in later)


def test_shelving_never_hides_critical() -> None:
    m = AlarmManager()
    m.shelves = {"kick": (7, T0 + timedelta(hours=1))}
    acts = feed(m, [0.6] * 4 + [0.95] * 4)
    assert [(a.kind, a.tier, a.suppressed, a.shelf_id) for _, a in acts if a.kind != "update"] == [
        ("raise", 2, "shelved", 7),
        ("escalate", 3, None, None),
    ]
    expired = AlarmManager()
    expired.shelves = {"kick": (7, T0 - timedelta(minutes=1))}
    assert feed(expired, [0.6] * 4)[0][1].suppressed is None


def test_advisory_budget_per_shift() -> None:
    m = AlarmManager(AlarmPolicy(advisory_budget=2))
    sup = []
    for k in range(4):  # four separate advisories, far apart
        acts = feed(m, [0.3] * 4 + [0.01] * 30, start=T0 + timedelta(minutes=30 * k))
        sup += [a.suppressed for _, a in acts if a.kind == "raise"]
    assert sup == [None, None, "budget", "budget"]
    w = feed(m, [0.6] * 4, start=T0 + timedelta(hours=3))
    assert w[0][1].suppressed is None  # warnings are never budgeted
    night = feed(m, [0.01] * 40 + [0.3] * 4, start=T0 + timedelta(hours=6, minutes=40))
    # 18:00 IST started a new shift: the budget is back
    assert [a.suppressed for _, a in night if a.kind in ("raise", "reopen")] == [None]
