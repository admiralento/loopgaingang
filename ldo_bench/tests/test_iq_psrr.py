"""Quiescent current and PSRR: the sine fit, both procedures on the simulator, and their setup checks."""

import math

import numpy as np
import pytest
from pydantic import ValidationError

from ldo_bench.analysis import sine_fit
from ldo_bench.bench import open_bench
from ldo_bench.config import IqConfig, PsrrConfig
from ldo_bench.instruments.base import InstrumentError
from ldo_bench.instruments.hp34401a import HP34401A
from ldo_bench.procedures import iq, psrr
from ldo_bench.procedures.psrr import Tone


# --- sine fit ------------------------------------------------------------------
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


# --- iq --------------------------------------------------------------------------
def run_iq(cfg, setup=None, **upd):
    sec = IqConfig.model_validate(cfg.iq.model_dump() | upd)
    with open_bench(cfg, sim=True) as b:
        if setup:
            setup(b)
        res = iq.run(b, sec)
    return res, b


def test_iq_shunt_on_sim(jafwak):
    res, b = run_iq(jafwak)
    m = b.sim_model
    assert not res.issues, res.issues
    assert res.zero == pytest.approx(m.dmm_offset_v / jafwak.iq.sense_ohm, rel=0.2)  # offset subtracted
    assert res.check["measured"] == pytest.approx(res.check["expected"], rel=0.01)
    on = [p for p in res.points if p.state == "enabled"]
    off = [p for p in res.points if p.state == "disabled"]
    assert len(on) == len(off) == 6
    for p in on:
        expect = m.iq_on * (1 + m.iq_vin_slope * (p.vin - 1.8))
        assert p.current == pytest.approx(expect, abs=res.floor) and not p.below_floor
    assert all(p.below_floor for p in off)  # 0.3 uA shutdown current: under the shunt method's ~4 uA floor
    assert any("below what this method resolves" in n for n in res.notes)
    assert not m.psu_on and b.loadbank.code == 0
    iq.plot(res, "x")
    assert "enabled" in res.report() and res.summary()["valid"]


def test_iq_series_resolves_shutdown_current(jafwak):
    res, b = run_iq(jafwak, method="series", rail="total", check_code=0)
    m = b.sim_model
    assert not res.issues and res.floor < 100e-9
    off = [p for p in res.points if p.state == "disabled"]
    for p in off:
        assert not p.below_floor
        assert p.current == pytest.approx(m.iq_off * (1 + m.iq_vin_slope * (p.vin - 1.8)), abs=3 * res.floor)


def test_iq_series_one_rail(jafwak):
    res, b = run_iq(jafwak, method="series", rail="bgr", states=["enabled"], vin_start=1.8, vin_stop=1.8)
    (p,) = res.points
    assert p.current == pytest.approx(0.15 * b.sim_model.iq_on, rel=0.01)
    assert "BGR_VDD" in res.what and "J1 1-3" in res.what


def test_iq_dmm_not_on_r13_is_flagged(jafwak):
    def loose(b):
        orig = b.confirm_wiring

        def cw(*a, **k):
            orig(*a, **k)
            b.sim_model.dmm_probe = "open"

        b.confirm_wiring = cw

    res, _ = run_iq(jafwak, setup=loose)
    assert any("load code 1 should add" in i for i in res.issues)
    assert any("below the DMM floor" in i for i in res.issues)


def test_iq_reversed_leads_are_an_issue_and_not_flipped(jafwak):
    def reverse(b):
        read = b.dmm.read
        b.dmm.read = lambda: -read()

    res, _ = run_iq(jafwak, setup=reverse)
    assert any("reversed" in i for i in res.issues)
    assert all(p.current < -100e-6 for p in res.points if p.state == "enabled")  # as measured


def test_iq_enable_path_not_working_is_flagged(jafwak):
    def no_enable(b):
        orig = b.confirm_wiring
        b.confirm_wiring = lambda *a, **k: orig(*a, **(k | {"en_pin": None}))

    res, _ = run_iq(jafwak, setup=no_enable)
    assert any("while disabled" in i for i in res.issues)


def test_iq_needs_the_dmm(jafwak):
    with (
        open_bench(jafwak, sim=True, use_dmm=False) as b,
        pytest.raises(InstrumentError, match="needs the DMM"),
    ):
        iq.run(b, jafwak.iq)


def test_iq_config_checks():
    with pytest.raises(ValidationError, match="shunt"):
        IqConfig(method="shunt", rail="bgr", vin_start=1.8, vin_stop=1.8)
    with pytest.raises(ValidationError, match="vin_stop"):
        IqConfig(vin_start=1.9, vin_stop=1.4)


def test_34401a_dci_and_overload():
    sent = []

    class T:
        name, kind = "fake", "gpib"

        def write(self, c):
            sent.append(c)

        def query(self, c):
            sent.append(c)
            return {"*IDN?": "HEWLETT-PACKARD,34401A,0,11-5-2", "SYST:ERR?": '+0,"No error"'}.get(
                c, "+9.9E+37"
            )

    d = HP34401A(T())
    d.configure_dci(range=0.01, nplc=10)
    assert "CONF:CURR:DC 0.01" in sent and "CURR:DC:NPLC 10" in sent
    assert d.read() == math.inf


# --- psrr ------------------------------------------------------------------------
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


def test_psrr_interlock(jafwak):
    def miswired(b):
        b.confirm_wiring = lambda *a, **k: setattr(b.sim_model, "psu_to", "vin")

    with pytest.raises(InstrumentError, match="looks connected to VIN"):
        run_psrr(jafwak, setup=miswired)


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
