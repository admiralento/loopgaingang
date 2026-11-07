"""Enable block tests: threshold / step analysis and the two procedures on the simulator."""

import numpy as np
import pytest

from ldo_bench.analysis import schmitt_analysis, step_metrics
from ldo_bench.bench import open_bench
from ldo_bench.config import BenchConfig
from ldo_bench.procedures import en_sine, startup


def schmitt(vt_hi=1.0, vt_lo=0.8, f=1e3, fs=2e6, cycles=4, vdd=1.8, chatter=False):
    t = np.arange(int(cycles / f * fs)) / fs
    ain = 0.9 + 0.9 * np.sin(2 * np.pi * f * t)
    out, state = np.zeros_like(t), False
    for k, a in enumerate(ain):
        if not state and a >= vt_hi:
            state = True
        elif state and a <= vt_lo:
            state = False
        out[k] = vdd if state else 0.0
    if chatter:  # a glitch right after each rising edge
        rise = np.nonzero(np.diff(out) > 0)[0]
        for r in rise:
            out[r + 3 : r + 6] = 0.0
    return ain, out, fs, f


def test_schmitt_thresholds_and_hysteresis():
    ain, out, fs, f = schmitt(1.0, 0.8)
    r = schmitt_analysis(ain, out, fs, f)
    # thresholds within one sample of slew (~3 mV at 2 MS/s on this sine)
    assert r.vt_rise == pytest.approx(1.0, abs=0.005) and r.vt_fall == pytest.approx(0.8, abs=0.005)
    assert r.hysteresis == pytest.approx(0.2, abs=0.01)
    assert r.voh == pytest.approx(1.8) and r.vol == pytest.approx(0.0)
    assert r.edges_per_cycle == pytest.approx(1.0, abs=0.3)


def test_schmitt_detects_chatter():
    ain, out, fs, f = schmitt(chatter=True)
    assert schmitt_analysis(ain, out, fs, f).edges_per_cycle > 1.5


def test_step_metrics_turn_on():
    t = np.linspace(-1e-4, 9e-4, 10001)
    x = np.where(t >= 20e-6, 1.2 * (1 - np.exp(-(np.clip(t, 0, None) - 20e-6) / 30e-6)), 0.0)
    x = np.clip(x, 0, None)
    m = step_metrics(t, x, 0.02)
    assert m.before == pytest.approx(0) and m.after == pytest.approx(1.2, abs=1e-3)
    assert m.t10 == pytest.approx(20e-6 + 30e-6 * np.log(1 / 0.9), abs=1e-6)
    assert m.t90 == pytest.approx(20e-6 + 30e-6 * np.log(10), abs=1e-6)
    assert m.t_settle == pytest.approx(20e-6 + 30e-6 * np.log(50), abs=2e-6)
    assert m.overshoot == pytest.approx(0, abs=1e-3)


def test_step_metrics_never_settles():
    t = np.linspace(-1e-4, 9e-4, 2001)
    x = np.where(t >= 0, t * 1000, 0.0)  # still ramping at the end
    assert np.isnan(step_metrics(t, x).t_settle)


def test_en_sine_on_sim(jafwak):
    with open_bench(jafwak, sim=True) as b:
        res = en_sine.run(b, jafwak.en_sine)
        m = b.sim_model
    assert not res.issues and len(res.captures) == jafwak.en_sine.captures
    vin = jafwak.en_sine.vin
    r = res.captures[0]
    assert r.vt_rise == pytest.approx(m.en_vt_rise * vin, abs=0.01)
    assert r.vt_fall == pytest.approx(m.en_vt_fall * vin, abs=0.01)
    assert r.edges_per_cycle == pytest.approx(1.0, abs=0.3)
    assert not m.awg_sine  # wavegen off afterwards
    en_sine.plot(res, "x")


def test_en_sine_output_not_switching_is_flagged(jafwak):
    with open_bench(jafwak, sim=True) as b:
        b.confirm_wiring = lambda *a, **k: setattr(b.sim_model, "ch1_probe", "vout")  # scope 1 not on EN_OUT
        res = en_sine.run(b, jafwak.en_sine)
    assert any("EN_OUT doesn't switch" in i for i in res.issues)
    assert "NOT A VALID MEASUREMENT" in res.report() and res.summary()["valid"] is False
    en_sine.plot(res, "x")


@pytest.mark.parametrize("ch2", ["vref", "iin", "iout"])
def test_startup_on_sim(jafwak, ch2):
    su = jafwak.startup.model_copy(update={"ch2": ch2, "averages": 2, "load_code": 2})
    with open_bench(jafwak, sim=True) as b:
        res = startup.run(b, su)
        m = b.sim_model
    assert not res.issues, res.issues
    on = res.edges[0]
    assert on.vout_m.after == pytest.approx(jafwak.dut.vout_nominal, abs=0.02)
    assert on.vout_m.t10 > m.vref_delay  # VOUT starts after the reference
    if ch2 == "vref":
        assert on.ch2_m.after == pytest.approx(m.vref_nominal, abs=0.01)
        assert on.ch2_m.t90 < on.vout_m.t90
    else:  # inrush: C dV/dt on top of the load current
        assert on.ch2_m.peak > on.ch2_m.after
    assert not m.dio.get(jafwak.enable.dio_pin) and getattr(b.scope, "vplus", None) is None  # off afterwards
    startup.plot(res, "x")


def test_startup_flags_enable_not_controlling_ldo(jafwak):
    su = jafwak.startup.model_copy(update={"averages": 2})
    with open_bench(jafwak, sim=True) as b:
        real = b.confirm_wiring

        def no_enable(*a, **k):  # the LDO ignores the enable path (e.g. EN_IN tied to VIN)
            real(*a, **{**k, "en_pin": None})

        b.confirm_wiring = no_enable
        res = startup.run(b, su)
    assert any("already" in i and "before the enable edge" in i for i in res.issues)


def test_enable_pins_must_not_clash_with_load_bank(jafwak):
    d = jafwak.model_dump()
    d["enable"]["dio_pin"] = 2  # a load bank pin
    with pytest.raises(ValueError, match="load bank pins"):
        BenchConfig.model_validate(d)


def test_sine_peak_limited_by_vin_max(jafwak):
    d = jafwak.model_dump()
    d["en_sine"]["high"] = 2.5  # > dut.vin_max 1.9
    with pytest.raises(ValueError, match="vin_max"):
        BenchConfig.model_validate(d)
