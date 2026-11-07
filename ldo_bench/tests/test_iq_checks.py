"""iq: the zero is real, overloads aren't passed off as readings, the floor follows the meter's settings."""

import numpy as np
import pytest
from pydantic import ValidationError

from ldo_bench.bench import open_bench
from ldo_bench.config import IqConfig
from ldo_bench.procedures import iq


def run_iq(cfg, setup=None, **upd):
    sec = IqConfig.model_validate(cfg.iq.model_dump() | upd)
    with open_bench(cfg, sim=True) as b:
        if setup:
            setup(b)
        return iq.run(b, sec), b


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
