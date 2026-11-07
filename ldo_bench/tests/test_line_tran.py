import pytest

from ldo_bench.bench import open_bench
from ldo_bench.instruments.base import InstrumentError
from ldo_bench.procedures import line_tran


def run(cfg, setup=None, **upd):
    lt = cfg.line_tran.model_copy(update={"averages": 4, **upd})
    with open_bench(cfg, sim=True) as b:
        if setup:
            setup(b)
        res = line_tran.run(b, lt, cfg.analysis)
    return res, b


def test_both_edges_on_sim(cfg):
    res, b = run(cfg)
    m, lt = b.sim_model, cfg.line_tran
    up, down = res.edges
    assert up.metrics.deviation > 0 > down.metrics.deviation  # VOUT kicks with VIN
    # sim: VOUT kick amplitude k_line * dVIN, times ~0.65 (peak of exp*sin, 1 us average)
    assert up.rejection == pytest.approx(0.65 * m.k_line, rel=0.2)
    for e in res.edges:
        assert 3e-6 < e.metrics.settling_time < 20e-6
    # buffer offset (-15 mV in the sim) calibrated out: VIN lands on the configured levels
    assert up.v_from == pytest.approx(lt.vin_low, abs=0.005)
    assert up.v_to == pytest.approx(lt.vin_high, abs=0.005)
    assert res.w1_low == pytest.approx(lt.vin_low - m.buffer_offset, abs=0.005)
    # everything off afterwards
    assert not m.psu_on and m.awg_v == {} and b.loadbank.code == 0


def test_interlock_stops_if_psu_is_on_vin(jafwak):
    def miswired(b):
        b.confirm_wiring = lambda *a, **k: setattr(b.sim_model, "psu_to", "vin")

    with pytest.raises(InstrumentError, match="looks connected to VIN"):
        run(jafwak, setup=miswired)


def test_interlock_never_raises_rail_above_check_level(jafwak):
    seen = []

    def spy(b):
        orig = b.psu.set_voltage
        b.psu.set_voltage = lambda v: (seen.append(v), orig(v))
        b.confirm_wiring = lambda *a, **k: setattr(b.sim_model, "psu_to", "vin")

    with pytest.raises(InstrumentError):
        run(jafwak, setup=spy)
    assert max(seen) <= line_tran.INTERLOCK_RAIL + 1e-9  # the 4 V rail was never applied


def test_buffer_without_headroom_is_reported(jafwak):
    seen = []

    def weak_buffer(b):
        b.sim_model.buffer_dropout = 2.5  # 4.0 V rail can only reach 1.5 V
        orig = b.awg.set_dc
        b.awg.set_dc = lambda ch, v: (seen.append(v), orig(ch, v))

    res, _ = run(jafwak, setup=weak_buffer)  # runs to the end, records the problem
    assert any("headroom" in i for i in res.issues)
    # the calibration never chased the dead buffer upward
    assert max(seen) <= jafwak.line_tran.vin_high + line_tran.CAL_MAX_CORRECTION + 1e-9


def test_load_limit_applies(jafwak):
    from ldo_bench.config import BenchConfig

    d = jafwak.model_dump()
    d["line_tran"]["load_code"] = 8  # 38 mA > 20 mA
    with pytest.raises(ValueError, match="i_max"):
        BenchConfig.model_validate(d)
