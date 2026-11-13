"""psrr and the sine fit it measures with."""

import math

import numpy as np
import pytest
from pydantic import ValidationError

from ldo_bench.analysis import sine_fit
from ldo_bench.bench import open_bench
from ldo_bench.config import PsrrConfig
from ldo_bench.instruments.base import InstrumentError
from ldo_bench.procedures import psrr
from ldo_bench.procedures.psrr import PsrrPoint, Tone


def test_sine_fit_amplitude_phase_non_integer_cycles():
    fs, f, n = 1e6, 1234.5, 8192  # 10.1 cycles: no whole number in the window
    t = (np.arange(n) - (n - 1) / 2) / fs
    rng = np.random.default_rng(0)
    x = 1.8 + 0.05 * np.cos(2 * np.pi * f * t + 0.7) + 1e-3 * t + rng.normal(0, 0.5e-3, n)
    r = sine_fit(x, fs, f)
    assert r.amplitude == pytest.approx(0.05, rel=2e-3)
    assert np.angle(r.phasor) == pytest.approx(0.7, abs=5e-3)
    assert r.dc == pytest.approx(1.8, abs=1e-4)
    assert r.resid_rms == pytest.approx(0.5e-3, rel=0.05)


def test_sine_fit_ratio_is_the_transfer():
    fs, f, n = 10e6, 100e3, 8192
    t = np.arange(n) / fs
    a = np.sin(2 * np.pi * f * t + 1.0)
    b = 0.01 * np.sin(2 * np.pi * f * t + 1.0 + math.radians(30))
    h = sine_fit(b, fs, f).phasor / sine_fit(a, fs, f).phasor
    assert abs(h) == pytest.approx(0.01, rel=1e-6) and math.degrees(np.angle(h)) == pytest.approx(
        30, abs=1e-4
    )


def run_psrr(cfg, setup=None, meter=None, **upd):
    sec = PsrrConfig.model_validate(cfg.psrr.model_dump() | {"averages": 2} | upd)
    with open_bench(cfg, sim=True) as b:
        if setup:
            setup(b)
        res = psrr.run(b, sec, meter)
    return res, b


def test_psrr_on_sim_matches_the_model(cfg):
    res, b = run_psrr(cfg)
    m = b.sim_model

    def model_h(f):  # the sim's transfer while the LDO is on (the bench is shut down by now)
        return m.psrr_h_dc * (1 + 1j * f / m.psrr_fz) / (1 + 1j * f / m.psrr_fp)

    assert not res.issues, res.issues
    assert res.vin_dc == pytest.approx(cfg.psrr.vin, abs=0.005)  # buffer offset calibrated out
    for p in res.points:
        assert p.vin_amp == pytest.approx(cfg.psrr.amplitude, rel=0.02)
        if not p.limited:
            want = -20 * math.log10(abs(model_h(p.frequency)))
            assert p.psrr_db == pytest.approx(want, abs=1.5), p.frequency
            if p.frequency >= 10e3:
                assert p.phase_deg == pytest.approx(math.degrees(np.angle(model_h(p.frequency))), abs=5)
    assert res.points[0].frequency == cfg.psrr.f_start and res.points[-1].frequency == cfg.psrr.f_stop
    assert not m.psu_on and not m.awg_wave and not m.awg_v and b.loadbank.code == 0
    psrr.plot(res, "x")
    assert "PSRR" in res.report() and res.summary()["psrr_at"]


def test_psrr_beyond_the_floor_is_a_bound(jafwak):
    def quiet(b):
        b.sim_model.psrr_h_dc = 1e-5  # 100 dB: far below what the AD2 resolves

    res, _ = run_psrr(jafwak, setup=quiet, f_stop=1e3)
    assert not res.issues and all(p.limited for p in res.points)
    for p in res.points:
        assert 60 < p.psrr_db < 100 and p.psrr_db == p.floor_db and math.isnan(p.phase_deg)
    assert any("at least" in n for n in res.notes)
    assert ">=" in res.report()


class FakeMeter:
    name = "fake"

    def __init__(self, vin_amp):
        self.vin_amp = vin_amp

    def measure(self, f):
        return Tone(complex(self.vin_amp), complex(1e-4), 1e-5, 1e-5, 1.8)


def test_psrr_stops_if_vin_peaks_above_vin_max(jafwak):
    holder = {}

    def keep(b):
        holder["b"] = b

    with pytest.raises(InstrumentError, match=r"above dut\.vin_max"):
        run_psrr(jafwak, setup=keep, meter=FakeMeter(0.2))  # 1.8 + 0.2 > 1.9
    assert not holder["b"].sim_model.awg_wave


def test_psrr_no_injection_is_flagged(jafwak):
    res, _ = run_psrr(jafwak, meter=FakeMeter(1e-3), f_stop=1e3)
    assert any("only carries" in i for i in res.issues)


def test_bench_refuses_sine_over_vin_max(jafwak):
    with open_bench(jafwak, sim=True) as b, pytest.raises(InstrumentError, match="refusing"):
        b.vin_sine_from_wavegen(dc=1.8, amplitude=0.2, frequency=1e3)


def test_psrr_config_checks(jafwak):
    from ldo_bench.config import BenchConfig

    data = jafwak.model_dump()
    data["psrr"] |= {"amplitude": 0.2}
    with pytest.raises(ValidationError, match="vin_max"):
        BenchConfig.model_validate(data)
    ps = PsrrConfig(psu_voltage=4, vin=1.8, f_start=100, f_stop=1e6, points_per_decade=3)
    assert ps.frequencies()[:4] == [100, 215, 464, 1000] and ps.frequencies()[-1] == 1e6


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


def test_psrr_db_and_phase_by_hand():
    vout = 0.05e-3 * complex(math.cos(math.radians(30)), math.sin(math.radians(30)))
    p = PsrrPoint(1e3, Tone(complex(0.05), vout, 1e-7, 1e-7, 1.8), 0.05)
    assert p.psrr_db == pytest.approx(60.0) and p.phase_deg == pytest.approx(30.0)
