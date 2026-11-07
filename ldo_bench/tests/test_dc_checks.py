"""DC tests check what they assume: VIN where it was set, the load drawing what it should, a real fit."""

import numpy as np
import pytest

from ldo_bench.analysis import dropout_strays, find_dropout
from ldo_bench.bench import DcPoint, open_bench
from ldo_bench.procedures import line_reg, load_reg


def run(proc, cfg, setup=None, **upd):
    sec = (cfg.load_reg if proc is load_reg else cfg.line_reg).model_copy(update=upd)
    with open_bench(cfg, sim=True) as b:
        if setup:
            setup(b)
        return proc.run(b, sec), b


def vin_probe_on_wrong_node(b):
    b.sim_model.ref_value = lambda: 1.0  # scope 2 reads 1.0 V whatever VIN is


@pytest.mark.parametrize("proc", [load_reg, line_reg])
def test_vin_at_the_board_must_match_the_setpoint(proc, jafwak):
    res, _ = run(proc, jafwak, vin_probe_on_wrong_node, **({"vin_source": "ad2"} if proc is line_reg else {}))
    assert any("VIN at the board" in i for i in res.issues), res.issues


@pytest.mark.parametrize("proc", [load_reg, line_reg])
def test_vin_check_passes_on_a_good_bench(proc, jafwak):
    res, _ = run(proc, jafwak)
    assert not any("VIN at the board" in i for i in res.issues)


def test_dead_load_bit_is_caught_by_the_supply_current(jafwak):
    def dead_bit(b):
        b.sim_model.resistors = [*b.sim_model.resistors[:2], 1e12, *b.sim_model.resistors[3:]]  # B2 open

    res, _ = run(load_reg, jafwak, dead_bit)
    assert any("load bank" in i for i in res.issues), res.issues


def test_load_current_check_needs_the_readback(jafwak):
    def dead_bit(b):
        b.sim_model.resistors = [*b.sim_model.resistors[:2], 1e12, *b.sim_model.resistors[3:]]

    res, _ = run(load_reg, jafwak, dead_bit, psu_readback=False)
    assert not any("load bank" in i for i in res.issues)  # nothing to compare: says so in a note
    assert any("readback" in n for n in res.notes)


def test_line_reg_without_enough_points_above_the_knee_is_flagged(jafwak):
    res, _ = run(line_reg, jafwak, vin_start=1.25, vin_stop=1.35, vin_step=0.05)
    assert any("fit" in i for i in res.issues), res.issues


def test_fit_vin_min_below_dropout_is_flagged(jafwak):
    res, _ = run(line_reg, jafwak, fit_vin_min=1.26)
    assert any("fit_vin_min" in i for i in res.issues), res.issues


def test_a_stray_low_reading_doesnt_move_the_dropout(jafwak):
    def glitch(b):
        orig = b.measure_point

        def mp(*a, **k):
            p = orig(*a, **k)
            if abs(b.sim_model.psu_v - 1.7) < 1e-6:  # one bad DMM reading mid-regulation
                return DcPoint(p.vout - 0.05, p.vout_std, p.vin, p.vout_ad2, p.source)
            return p

        b.measure_point = mp

    good, _ = run(line_reg, jafwak)
    bad, _ = run(line_reg, jafwak, glitch)
    assert bad.reg.dropout_vin == pytest.approx(good.reg.dropout_vin, abs=1e-3)
    assert any("1.70" in i and "dropout" in i for i in bad.issues), bad.issues


def test_dropout_is_the_first_crossing_from_the_bottom():
    vin = np.arange(1.0, 2.0, 0.1)
    vout = np.minimum(vin - 0.1, 1.2)
    vout[7] = 1.1  # a glitch at VIN 1.7
    d_vin, _ = find_dropout(vin, vout, 0.01)
    assert 1.2 < d_vin < 1.4  # the real knee (VIN = 1.3), not after the glitch
    assert dropout_strays(vin, vout, 0.01) == [pytest.approx(1.7)]
