import numpy as np
import pytest

from ldo_bench.analysis import SettlingResult
from ldo_bench.bench import open_bench
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


def test_buffer_without_headroom_is_reported(jafwak_single):
    seen = []

    def weak_buffer(b):
        # healthy through the interlock's proof (rail at vin_max), weak at the full rail:
        # the full rail (line_tran.psu_voltage) can then only reach 1.5 V
        rail = b.buffer_rail

        def weak_at_full_rail(v):
            if v > b.cfg.dut.vin_max:
                b.sim_model.buffer_dropout = v - 1.5
            rail(v)

        b.buffer_rail = weak_at_full_rail
        orig = b.awg.set_dc
        b.awg.set_dc = lambda ch, v: (seen.append(v), orig(ch, v))

    res, _ = run(jafwak_single, setup=weak_buffer)  # runs to the end, records the problem
    assert any("headroom" in i for i in res.issues)
    # the calibration never chased the dead buffer upward
    lt = jafwak_single.line_tran
    assert max(seen) <= min(lt.vin_high + line_tran.CAL_MAX_CORRECTION, jafwak_single.dut.vin_max) + 1e-9


def test_load_limit_applies(jafwak):
    from ldo_bench.config import BenchConfig

    d = jafwak.model_dump()
    d["line_tran"]["load_code"] = 23  # 21.04 mA > 21 mA
    with pytest.raises(ValueError, match="i_max"):
        BenchConfig.model_validate(d)


def test_line_tran_rejection_by_hand():
    m = SettlingResult(1.2, 1.205, 5e-4, 1.2, 1e-5, 1e-5, 1.2, 10e6, 100, "pre")  # +5 mV kick
    e = line_tran.EdgeResult("rising", 1.75, 1.85, m, np.zeros(1), np.zeros(1), np.zeros(1), np.zeros(1))
    assert e.rejection == pytest.approx(0.005 / 0.1)  # 50 mV per V of VIN step


def test_positive_buffer_offset_never_overshoots_vin(jafwak):
    """Bench, 2026-11-17: U2 read +65 mV above W1 at no load. Calibration that starts with W1 at the
    target put VIN at 1.915 V (over dut.vin_max) before the first reading. It must come up from
    below: no VIN reading during calibration may exceed the target it is heading for (+5 mV)."""
    readings: list[float] = []

    def offset(b):
        b.sim_model.buffer_offset = 0.065
        real = b.measure_vin_fine

        def spy():
            v = real()
            readings.append(v)
            return v

        b.measure_vin_fine = spy

    res, _ = run(jafwak, setup=offset, load_code=0)
    assert not [i for i in res.issues if "VIN reached" in i], res.issues
    assert max(readings) <= jafwak.line_tran.vin_high + 0.005, max(readings)
