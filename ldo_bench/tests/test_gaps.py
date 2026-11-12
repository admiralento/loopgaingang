"""Tests the review panel found missing: each would fail if the behaviour it names were wrong."""

import math

import numpy as np
import pytest

from ldo_bench.analysis import SettlingResult, load_regulation
from ldo_bench.bench import open_bench
from ldo_bench.instruments.base import Capture
from ldo_bench.procedures import iq, line_reg, line_tran, load_tran, psrr
from ldo_bench.procedures.psrr import PsrrPoint, Tone
from ldo_bench.procedures.transient import SWING_TOLERANCE, capture_issues
from ldo_bench.results import RunDir


# --- VOUT checks --------------------------------------------------------------------
def test_line_reg_flags_vout_off_nominal(jafwak):
    with open_bench(jafwak, sim=True) as b:
        b.confirm_wiring = lambda *a, **k: setattr(b.sim_model, "dmm_probe", "psu")  # DMM not on VOUT
        res = line_reg.run(b, jafwak.line_reg)
    assert any("VOUT out of range" in i for i in res.issues), res.issues


def test_vout_tolerance_boundary(jafwak):
    with open_bench(jafwak, sim=True) as b:
        assert b.vout_issue(0.95, source="DMM")  # 1.2 V - 21 %
        assert b.vout_issue(0.97, source="DMM") is None  # - 19 %
        b.cfg = b.cfg.model_copy(update={"dut": b.cfg.dut.model_copy(update={"vout_tolerance": 0.1})})
        assert b.vout_issue(1.07, source="DMM")  # - 11 % with a 10 % tolerance


# --- transient capture checks ----------------------------------------------------------------
def capture_with_step(jafwak, *, edge_found=True, vout_before=1.2):
    n, i0 = 1000, 200
    ref = np.where(np.arange(n) >= i0, 3.3, 0.0)
    vout = np.where(np.arange(n) >= i0, 1.2, vout_before)
    a = jafwak.ad2
    ch1, ch2 = (vout, ref) if a.vout_channel == 1 else (ref, vout)
    m = SettlingResult(1.2, 1.19, 0.001, 1.2, 1e-6, 1e-5, 1.2, 10e6, i0, "pre")
    return m, Capture(10e6, ch1, ch2, i0, edge_found=edge_found), ref


def test_capture_without_a_clean_edge_is_flagged(jafwak):
    m, cap, ref = capture_with_step(jafwak, edge_found=False)
    with open_bench(jafwak, sim=True) as b:
        issues = capture_issues(b, m, cap, ref, expected_swing=3.3, ref_desc="DIO 2")
    assert any("no clean edge" in i for i in issues)


def test_vout_before_the_step_is_checked_in_the_capture(jafwak):
    m, cap, ref = capture_with_step(jafwak, vout_before=0.0)  # DC check was fine, the capture isn't
    with open_bench(jafwak, sim=True) as b:
        issues = capture_issues(b, m, cap, ref, expected_swing=3.3, ref_desc="DIO 2")
    assert any("VOUT reads" in i for i in issues)


@pytest.mark.parametrize(
    ("scale", "flagged"), [(1 - SWING_TOLERANCE - 0.05, True), (1 - SWING_TOLERANCE + 0.05, False)]
)
def test_swing_tolerance_boundary(jafwak, scale, flagged):
    m, cap, ref = capture_with_step(jafwak)
    with open_bench(jafwak, sim=True) as b:
        issues = capture_issues(b, m, cap, ref, expected_swing=3.3 / scale, ref_desc="DIO 2")
    assert any("swings" in i for i in issues) == flagged


@pytest.mark.parametrize(
    ("vin_frac", "stops"),
    [(line_tran.INTERLOCK_FRACTION + 0.05, True), (line_tran.INTERLOCK_FRACTION - 0.05, False)],
)
def test_interlock_fraction_boundary(jafwak, vin_frac, stops):
    with open_bench(jafwak, sim=True) as b:
        b.confirm_wiring = lambda *a, **k: None
        reads = iter([vin_frac * line_tran.INTERLOCK_RAIL, 0.1, 0.3])  # stage 1, then a healthy proof
        b.scope.measure_dc = lambda d, setups: (
            {ch: type("R", (), {"mean": next(reads)})() for ch in [b.cfg.ad2.ref_channel]}
            | {b.cfg.ad2.vout_channel: type("R", (), {"mean": 1.2})()}
        )
        if stops:
            with pytest.raises(Exception, match="connected to VIN"):
                line_tran._interlock(b, 4.0)
        else:
            line_tran._interlock(b, 4.0)


@pytest.mark.parametrize(("seen_frac", "flagged"), [(0.8, True), (0.9, False)])
def test_iq_load_check_tolerance_boundary(jafwak, seen_frac, flagged):
    """check_tolerance is 15 %: the meter seeing 80 % of the load step fails, 90 % passes."""
    with open_bench(jafwak, sim=True) as b:
        read = b.dmm.read
        expected = {}

        def scaled():
            v = read()
            if b.loadbank.code:  # only the load's share is scaled: what the meter sees of the step
                base = expected.setdefault("base", None)
                g = b.sim_model.conductance()
                i_load = b.sim_model.vout_dc() * g * jafwak.iq.sense_ohm
                return v - (1 - seen_frac) * i_load if base is None else v
            return v

        b.dmm.read = scaled
        res = iq.run(b, jafwak.iq)
    assert any("load code" in i for i in res.issues) == flagged


# --- the cross-check scope is analysed from its own trace --------------------------------------
def test_xcheck_uses_the_54621a_trace(jafwak):
    an = jafwak.analysis.model_copy(update={"settle_reference": "post"})
    with open_bench(jafwak, sim=True, use_xscope=True) as b:
        collect = b.xscope.collect
        nom = jafwak.dut.vout_nominal

        def doubled(*a, **k):  # a scope that sees twice the dip: independent of the AD2's trace
            tr = collect(*a, **k)
            v = tr[jafwak.xscope.vout_channel]
            v.v[:] = nom + 2 * (v.v - nom)
            return tr

        b.xscope.collect = doubled
        res = load_tran.run(b, jafwak.load_tran.model_copy(update={"averages": 4}), an)
    for e in res.edges:
        assert e.xcheck.metrics.deviation == pytest.approx(2 * e.metrics.deviation, rel=0.15)


# --- VOUT really comes from the DMM -------------------------------------------------------------
def test_vout_comes_from_the_dmm_when_there_is_one(jafwak):
    with open_bench(jafwak, sim=True) as b:
        read = b.dmm.read_dcv
        b.dmm.read_dcv = lambda: read() + 5e-3  # the DMM reads 5 mV above the AD2
        b.vin_from_psu(1.8)
        p = b.measure_point(0.05)
    assert p.source == "dmm" and p.vout - p.vout_ad2 == pytest.approx(5e-3, abs=1e-3)


# --- PSRR amplitude levelling -------------------------------------------------------------------
def test_psrr_levels_w1_when_vin_carries_half_the_sine(jafwak):
    amps = []

    class HalfGain:
        name = "half"

        def __init__(self, b):
            self.b = b

        def measure(self, f):
            _, _, lo, hi = next(iter(self.b.sim_model.awg_wave.values()))
            return Tone(complex((hi - lo) / 4), complex(1e-5), 1e-6, 1e-6, 1.8)  # VIN = W1 / 2

    ps = jafwak.psrr.model_copy(update={"f_start": 1e3, "f_stop": 2e3, "points_per_decade": 1})
    with open_bench(jafwak, sim=True) as b:
        orig = b.vin_sine_from_wavegen
        b.vin_sine_from_wavegen = lambda **k: (amps.append(k["amplitude"]), orig(**k))
        psrr.run(b, ps, HalfGain(b))
    assert amps[0] == pytest.approx(ps.amplitude)
    # levelled up towards 2 x, but capped so the VIN peak stays inside vin_max less the AD2's margin
    assert ps.amplitude < amps[1] <= 2 * ps.amplitude
    assert 1.8 + amps[1] <= jafwak.dut.vin_max


# --- hand-computed numbers (not from the simulator) ----------------------------------------------
def test_line_tran_rejection_by_hand():
    m = SettlingResult(1.2, 1.205, 5e-4, 1.2, 1e-5, 1e-5, 1.2, 10e6, 100, "pre")  # +5 mV kick
    e = line_tran.EdgeResult("rising", 1.75, 1.85, m, np.zeros(1), np.zeros(1), np.zeros(1), np.zeros(1))
    assert e.rejection == pytest.approx(0.005 / 0.1)  # 50 mV per V of VIN step


def test_psrr_db_and_phase_by_hand():
    vout = 0.05e-3 * complex(math.cos(math.radians(30)), math.sin(math.radians(30)))
    p = PsrrPoint(1e3, Tone(complex(0.05), vout, 1e-7, 1e-7, 1.8), 0.05)
    assert p.psrr_db == pytest.approx(60.0) and p.phase_deg == pytest.approx(30.0)


def test_load_regulation_is_a_least_squares_fit_not_the_endpoints():
    i = np.array([0, 1, 2, 3, 4]) * 1e-3
    v = 1.2 - 3.0 * i + 1e5 * i**3  # curved, asymmetric: the endpoints' slope differs from the fit's
    r = load_regulation(i, v)
    assert r.slope == pytest.approx(np.polyfit(i, v, 1)[0])
    assert r.slope != pytest.approx((v[-1] - v[0]) / (i[-1] - i[0]))


# --- run folders --------------------------------------------------------------------------------
def test_two_runs_in_the_same_second_get_separate_folders(jafwak, tmp_path, monkeypatch):
    import ldo_bench.results as results

    fixed = results.datetime(2026, 11, 7, 12, 0, 0)
    monkeypatch.setattr(results, "datetime", type("D", (), {"now": staticmethod(lambda: fixed)}))
    a = RunDir(tmp_path, "load-reg", jafwak)
    b = RunDir(tmp_path, "load-reg", jafwak)
    assert a.path != b.path and a.path.exists() and b.path.exists()


# --- DMM range recorded per point -----------------------------------------------------------------
def test_dmm_range_change_inside_the_line_reg_fit_is_noted(jafwak):
    with open_bench(jafwak, sim=True) as b:
        # no load: the sim's VOUT rises from just under 1.2 V to just over it along the sweep
        res = line_reg.run(b, jafwak.line_reg.model_copy(update={"load_code": 0}))
    assert all("dmm_range_v" in r for r in res.rows)
    ranges = {r["dmm_range_v"] for r in res.rows if r["vin_v"] >= res.reg.fit_vin_min - 1e-9}
    assert len(ranges) == 2  # the sim's VOUT crosses 1.2 V, the top of the 1 V range
    assert any("range" in n for n in res.notes)
