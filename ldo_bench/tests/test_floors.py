"""Readings at or below what an instrument resolves are bounds; timings need a settled signal."""

import numpy as np
import pytest
from helpers import miswire

from ldo_bench.analysis import schmitt_analysis, settling_analysis
from ldo_bench.bench import open_bench
from ldo_bench.config import PsrrConfig
from ldo_bench.procedures import en_sine, psrr, startup


# --- PSRR floor -----------------------------------------------------------------------
def test_mains_hum_near_the_test_frequency_is_not_reported_as_psrr(jafwak):
    ps = PsrrConfig.model_validate(jafwak.psrr.model_dump() | {"f_start": 215, "f_stop": 216})
    with open_bench(jafwak, sim=True) as b:
        b.sim_model.psrr_h_dc = 1e-5  # the LDO rejects 100 dB: VOUT's tone is far below the AD2
        b.sim_model.psrr_fz = 1e9
        b.sim_model.vout_hum = (1e-3, 240.0)  # 1 mV of 240 Hz on VOUT
        res = psrr.run(b, ps)
    assert all(p.limited for p in res.points), [(p.frequency, p.psrr_db, p.limited) for p in res.points]


def test_psrr_still_measures_a_clean_tone(jafwak):
    ps = PsrrConfig.model_validate(jafwak.psrr.model_dump() | {"f_start": 1e4, "f_stop": 1e5})
    with open_bench(jafwak, sim=True) as b:
        b.sim_model.vout_hum = (1e-3, 240.0)  # hum far from these frequencies
        res = psrr.run(b, ps)
    assert not any(p.limited for p in res.points)


# --- startup -------------------------------------------------------------------------
def run_startup(cfg, setup=None, **upd):
    su = cfg.startup.model_copy(update={"averages": 2, **upd})
    with open_bench(cfg, sim=True) as b:
        if setup:
            setup(b)
        return startup.run(b, su)


def test_input_current_under_the_ad2_resolution_is_a_bound(jafwak):
    res = run_startup(jafwak, ch2="iin", load_code=0)  # ~150 uA of Iq: 0.05 counts on 0.1 ohm
    for e in res.edges:
        assert not e.ch2_resolved
        assert np.isnan(e.ch2_m.t10) and np.isnan(e.ch2_m.t90)
    assert "resolve" in res.report()


def test_output_current_step_above_the_resolution_is_measured(jafwak):
    res = run_startup(jafwak, ch2="iout", load_code=4)  # 19.2 mA: ~6 counts
    assert all(e.ch2_resolved for e in res.edges)


def test_unsettled_edge_is_flagged(jafwak):
    res = run_startup(jafwak, window=40e-6)  # VOUT is still rising at the end
    assert any("settle" in i for i in res.issues), res.issues


def test_disable_edge_checks_vout_turned_off(jafwak):
    res = run_startup(jafwak, lambda b: miswire(b, wiring_kw={"en_pin": None}), edges=["falling"])
    assert any("still" in i for i in res.issues), res.issues


# --- enable-sine checks every capture ------------------------------------------------------
def test_en_sine_checks_every_capture(jafwak):
    with open_bench(jafwak, sim=True) as b:
        orig = b.scope.capture
        n = {"k": 0}

        def capture(**kw):
            cap = orig(**kw)
            n["k"] += 1
            if n["k"] == 3:  # EN_OUT stops switching in the third capture (probe slips)
                cap.ch1[:] = 0.0 if b.cfg.ad2.vout_channel == 1 else cap.ch1
                cap.ch2[:] = 0.0 if b.cfg.ad2.vout_channel == 2 else cap.ch2
            return cap

        b.scope.capture = capture
        res = en_sine.run(b, jafwak.en_sine)
    assert any("capture 3" in i for i in res.issues), res.issues


# --- analysis edge cases -------------------------------------------------------------------
def test_settling_is_nan_when_the_step_is_at_the_very_end():
    n, fs = 2000, 10e6
    ref = np.zeros(n)
    ref[n - 4 :] = 3.3
    vout = np.full(n, 1.0)
    m, _ = settling_analysis(vout, ref, fs, "rising")
    assert np.isnan(m.settling_time)


def test_schmitt_levels_with_a_short_high_time():
    fs, f = 1e6, 1e3
    t = np.arange(int(4 / f * fs)) / fs
    ain = 0.9 + 0.9 * np.sin(2 * np.pi * f * t)
    out = np.where(ain > 1.79, 1.8, 0.0)  # high for under 5 % of each cycle
    r = schmitt_analysis(ain, out, fs, f)
    assert r.voh == pytest.approx(1.8) and r.vol == pytest.approx(0.0)
