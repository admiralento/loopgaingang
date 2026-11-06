import pytest

from ldo_bench.bench import open_bench
from ldo_bench.procedures import loadbank_check


def run_with(cfg):
    with open_bench(cfg, sim=True, use_dmm=False) as b:
        res = loadbank_check.run(b, cfg.loadbank_check)
    return res, b


def test_correct_wiring_passes(cfg):
    res, b = run_with(cfg)
    assert res.passed and not res.diagnosis()
    assert not b.sim_model.psu_on and b.sim_model.conductance() == 0


def test_swapped_wires_diagnosed(jafwak):
    with open_bench(jafwak, sim=True, use_dmm=False) as b:
        real_write = b.scope.write_pins
        swap = {3: 4, 4: 3}
        b.scope.write_pins = lambda levels: real_write({swap.get(p, p): v for p, v in levels.items()})
        res = loadbank_check.run(b, jafwak.loadbank_check)
    assert not res.passed
    notes = " ".join(res.diagnosis())
    assert "DIO 3 draws bit 4's current" in notes and "J4, not J3" in notes


def test_open_gate_diagnosed(jafwak):
    with open_bench(jafwak, sim=True, use_dmm=False) as b:
        real_write = b.scope.write_pins
        b.scope.write_pins = lambda levels: real_write({p: v for p, v in levels.items() if p != 2})
        res = loadbank_check.run(b, jafwak.loadbank_check)
    assert [r["ok"] for r in res.rows] == [True, True, False, True, True]
    assert "DIO 2 (bit 2) draws no current" in res.diagnosis()[0]


def test_voltage_capped():
    from ldo_bench.config import LoadBankCheckConfig

    with pytest.raises(ValueError):
        LoadBankCheckConfig(voltage=3.3)
