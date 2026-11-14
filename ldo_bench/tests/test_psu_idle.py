"""After shutdown the supply's output is off, with its setpoint left at psu.idle_voltage and the limit at
psu.current_limit (JAFWAK: 1.8 V, 10 mA), so turning it on by hand gives the normal VIN. Without
psu.idle_voltage the setpoint goes to 0 V as before."""

import pytest
from helpers import CONFIG_DIR, spy
from pydantic import ValidationError

from ldo_bench.bench import open_bench
from ldo_bench.config import BenchConfig, load_config
from ldo_bench.procedures import load_reg


def test_jafwak_idle_setting(jafwak):
    assert jafwak.psu.idle_voltage == pytest.approx(1.8)


def test_shutdown_leaves_1v8_10ma_output_off(jafwak):
    calls = []
    with open_bench(jafwak, sim=True) as b:
        load_reg.run(b, jafwak.load_reg)  # runs on the 60 mA high limit
        spy(b.psu, "output", calls)
        spy(b.psu, "setup", calls)
        spy(b.psu, "set_voltage", calls)
    m = b.sim_model
    assert not m.psu_on
    assert m.psu_v == pytest.approx(1.8)
    off = next(i for i, (n, a, _) in enumerate(calls) if n == "output" and a == (False,))
    setups = [(i, k) for i, (n, _, k) in enumerate(calls) if n == "setup"]
    assert setups and setups[-1][1]["current_limit"] == pytest.approx(0.010)  # back from 60 mA
    assert all(i > off for i, _ in setups)  # only once the output is off
    last_v = [(i, a[0]) for i, (n, a, _) in enumerate(calls) if n == "set_voltage"][-1]
    assert last_v[1] == pytest.approx(1.8) and last_v[0] > off
    assert not any(n == "output" and a == (True,) for n, a, _ in calls[off:])  # never turned back on


def test_idle_setpoint_not_programmed_if_the_output_did_not_turn_off(jafwak):
    with open_bench(jafwak, sim=True) as b:
        b.vin_from_psu(1.0)

        def stuck(on):
            raise RuntimeError("OUTP? still 1")

        b.psu.output = stuck
    assert b.sim_model.psu_v == pytest.approx(1.0)  # not touched: the output may still be on
    assert any("idle" in e for e in b.shutdown_errors)


def test_without_idle_voltage_the_setpoint_goes_to_zero():
    demo = load_config(CONFIG_DIR / "demo_board.toml")
    assert demo.psu.idle_voltage is None
    with open_bench(demo, sim=True) as b:
        b.vin_from_psu(1.5)
    assert b.sim_model.psu_v == 0.0 and not b.sim_model.psu_on


def test_idle_voltage_within_the_vin_ceiling(jafwak):
    data = jafwak.model_dump()
    data["psu"]["idle_voltage"] = 2.0  # over dut.vin_max (1.9 V): the supply sits on VIN
    with pytest.raises(ValidationError, match="idle_voltage"):
        BenchConfig.model_validate(data)


def test_startup_runs_with_a_4p6ma_load(jafwak):
    su = jafwak.startup
    assert su.load_code == 5
    assert jafwak.dut.vout_nominal * jafwak.loadbank.conductance(5) == pytest.approx(4.636e-3, rel=1e-3)
