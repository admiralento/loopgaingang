import itertools

import pytest

from ldo_bench.bench import open_bench
from ldo_bench.instruments.base import InstrumentError


def test_vin_ceiling_enforced(jafwak_single):
    vmax = jafwak_single.dut.vin_max
    with open_bench(jafwak_single, sim=True) as b:
        with pytest.raises(InstrumentError, match=rf"limit {vmax}"):
            b.vin_from_psu(vmax + 0.1)
        b.buffer_rail(4.5)  # buffer rail may exceed the DUT's VIN limit
        with pytest.raises(InstrumentError):
            b.vin_from_wavegen(vmax + 0.1)


def test_psu_ramps_in_steps(jafwak):
    with open_bench(jafwak, sim=True) as b:
        seen = []
        orig = b.psu.set_voltage
        b.psu.set_voltage = lambda v: (seen.append(v), orig(v))
        b.vin_from_psu(1.8)
        steps = [y - x for x, y in itertools.pairwise(seen)]
        assert seen[-1] == pytest.approx(1.8)
        assert max(steps) <= jafwak.psu.ramp_step + 1e-9


def test_shutdown_runs_on_exception(jafwak):
    b = open_bench(jafwak, sim=True)
    m = b.sim_model
    with pytest.raises(RuntimeError), b:
        b.vin_from_psu(1.8)
        b.loadbank.set(b.loadbank.max_allowed_code)
        b.vin_from_wavegen(1.5)
        raise RuntimeError("boom")
    assert not m.psu_on and m.psu_v == 0.0
    assert m.awg_v == {}
    assert m.conductance() == 0.0


def test_shutdown_continues_past_failures(jafwak):
    b = open_bench(jafwak, sim=True)
    b.vin_from_psu(1.8)
    b.awg.off = lambda ch: (_ for _ in ()).throw(InstrumentError("usb gone"))
    b.shutdown()
    assert not b.sim_model.psu_on


def test_dc_measurement_tracks_load(bench):
    bench.vin_from_psu(1.8)
    bench.loadbank.set(0)
    v0, ref = bench.measure_dc(0.1)
    bench.loadbank.set(bench.loadbank.max_allowed_code)
    v1, _ = bench.measure_dc(0.1)
    assert ref.mean == pytest.approx(1.8, abs=1e-3)
    assert v0.mean == pytest.approx(bench.cfg.dut.vout_nominal, abs=5e-3)
    assert v1.mean < v0.mean


def test_dut_current_limit_enforced_at_runtime(jafwak):
    with open_bench(jafwak, sim=True) as b:
        assert b.loadbank.allowed_codes() == [0, 1, 2, 3, 4]  # <= 20 mA at 1.2 V
        assert b.loadbank.max_allowed_code == 4
        with pytest.raises(InstrumentError, match=r"dut\.i_max"):
            b.loadbank.set(5)  # 24 mA
        with b.loadbank.unrestricted():  # loadbank-check: no DUT connected
            b.loadbank.set(31)
        with pytest.raises(InstrumentError):
            b.loadbank.set(31)  # limit back on afterwards


def test_dut_current_limit_enforced_in_config(jafwak):
    from ldo_bench.config import BenchConfig

    d = jafwak.model_dump()
    d["load_tran"]["code_high"] = 17
    with pytest.raises(ValueError, match=r"dut\.i_max"):
        BenchConfig.model_validate(d)


def test_vout_issue(jafwak):
    with open_bench(jafwak, sim=True) as b:
        assert b.vout_issue(1.19, source="scope 1") is None  # within 20 % of 1.2 V
        msg = b.vout_issue(-0.0004, source="scope 1")  # floating input, like the 15:09 run
        assert msg and "should be at 1.2 V" in msg
