"""iq: quiescent and shutdown current, the zero, the floors and the setup checks."""

import numpy as np
import pytest
from helpers import miswire
from pydantic import ValidationError

from ldo_bench.bench import open_bench
from ldo_bench.config import IqConfig
from ldo_bench.instruments.base import InstrumentError
from ldo_bench.procedures import iq


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
    res, _ = run_iq(jafwak, setup=lambda b: miswire(b, dmm_probe="open"))
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
    res, _ = run_iq(jafwak, setup=lambda b: miswire(b, wiring_kw={"en_pin": None}))
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


def test_zero_is_read_with_the_supply_output_off_even_if_it_was_left_on(jafwak):
    def left_on(b):
        b.sim_model.psu_on, b.sim_model.psu_v = True, 1.8  # from the front panel, before the run
        b.dio.write_pins({b.cfg.enable.dio_pin: True})  # and the LDO enabled: ~150 uA flowing

    res, b = run_iq(jafwak, left_on)
    assert res.zero == pytest.approx(b.sim_model.dmm_offset_v / jafwak.iq.sense_ohm, abs=5e-6)
    assert not res.issues


def test_overload_during_the_load_check_is_an_issue(jafwak):
    def overload_with_load(b):
        read = b.dmm.read
        b.dmm.read = lambda: float("inf") if b.loadbank.code else read()

    res, _ = run_iq(jafwak, overload_with_load)
    assert any("load check" in i for i in res.issues), res.issues


def test_zero_overload_gives_no_currents_not_a_zero_of_0(jafwak):
    def overload_when_off(b):
        read = b.dmm.read
        b.dmm.read = lambda: float("inf") if not b.sim_model.psu_on else read()

    res, _ = run_iq(jafwak, overload_when_off)
    assert np.isnan(res.zero)
    assert all(np.isnan(p.current) for p in res.points)
    assert any("PSU off" in i for i in res.issues)


def test_floor_follows_the_integration_time(jafwak):
    res, _ = run_iq(jafwak, nplc=0.2, states=["enabled"], vin_start=1.8, vin_stop=1.8)
    # 0.2 NPLC: one count is 1e-5 of the 100 mV range = 1 uV = 10 uA across 0.1 ohm
    assert res.floor >= 10e-6 - 1e-12


def test_series_method_uses_a_fixed_range(jafwak):
    ranges = []

    def spy(b):
        cfg = b.dmm.configure_dci
        b.dmm.configure_dci = lambda **k: (ranges.append(k["range"]), cfg(**k))

    run_iq(jafwak, spy, method="series", rail="total", check_code=0)
    assert ranges == [0.01]  # 10 mA range: the zero and every point on the same range


def test_zero_needs_enough_readings_for_a_spread():
    with pytest.raises(ValidationError):
        IqConfig(vin_start=1.8, vin_stop=1.8, readings=1)


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
