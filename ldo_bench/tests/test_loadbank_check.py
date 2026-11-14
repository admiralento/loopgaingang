"""loadbank-check: each DIO pin switches its own resistor, the bank straight across the PSU.

The current comes from the DMM in series (34401A DC amps, the 1 A range) by default, with the supply's
readback recorded beside it; or from the supply's readback alone (current_from = "psu")."""

import pytest
from helpers import config_path, invoke, spy

from ldo_bench.bench import open_bench
from ldo_bench.config import BenchConfig, LoadBankCheckConfig
from ldo_bench.instruments.base import InstrumentError
from ldo_bench.procedures import loadbank_check


def run_with(cfg, *, source="dmm", setup=None):
    lc = cfg.loadbank_check.model_copy(update={"current_from": source})
    with open_bench(cfg, sim=True, use_dmm=source == "dmm") as b:
        if setup:
            setup(b)
        res = loadbank_check.run(b, lc)
    return res, b


def _drop_pins(b, *pins):
    real_write = b.scope.write_pins
    b.scope.write_pins = lambda levels: real_write({p: v for p, v in levels.items() if p not in pins})


# --- config / wiring ----------------------------------------------------------------------
def test_defaults(jafwak):
    lc = jafwak.loadbank_check
    assert lc.current_from == "dmm" and lc.dmm_range == 1.0
    assert lc.abs_tolerance <= 0.3e-3  # a dead 1 mA B0 can't hide inside it
    assert lc.dmm_abs_tolerance <= 0.05e-3


def test_dmm_wiring_puts_it_in_series(jafwak):
    ch = jafwak.psu.channel
    with open_bench(jafwak, sim=True) as b:
        lines = loadbank_check.wiring(b, jafwak.loadbank_check)
        psu_only = loadbank_check.wiring(b, jafwak.loadbank_check.model_copy(update={"current_from": "psu"}))
    assert f"PSU OUT{ch} + -> DMM I (3A/mA jack)" in lines
    assert any(ln.startswith("DMM LO (COM) -> load bank VIN (J5)") for ln in lines)
    assert f"PSU OUT{ch} + -> load bank VIN (J5)" in psu_only and not any("DMM" in ln for ln in psu_only)


# --- results ------------------------------------------------------------------------------
@pytest.mark.parametrize("source", ["dmm", "psu"])
def test_correct_wiring_passes(cfg, source):
    res, b = run_with(cfg, source=source)
    assert res.passed and not res.diagnosis(), res.report()
    assert not b.sim_model.psu_on and b.sim_model.conductance() == 0
    assert all("measured_psu_a" in r and "measured_dmm_a" in r for r in res.rows)


def test_dmm_is_on_dc_amps_1a_range_before_reading(jafwak):
    calls = []
    res, _ = run_with(jafwak, setup=lambda b: (spy(b.dmm, "configure_dci", calls), spy(b.dmm, "read", calls)))
    first = calls[0]
    assert first[0] == "configure_dci" and first[2]["range"] == 1.0
    assert res.current_from == "dmm"


def test_dmm_mode_needs_a_dmm(jafwak):
    lc = jafwak.loadbank_check  # current_from = "dmm"
    with open_bench(jafwak, sim=True, use_dmm=False) as b, pytest.raises(InstrumentError, match="DMM"):
        loadbank_check.run(b, lc)


@pytest.mark.parametrize("source", ["dmm", "psu"])
def test_swapped_wires_diagnosed(jafwak, source):
    def swap(b):
        real_write = b.scope.write_pins
        s = {3: 4, 4: 3}
        b.scope.write_pins = lambda levels: real_write({s.get(p, p): v for p, v in levels.items()})

    res, _ = run_with(jafwak, source=source, setup=swap)
    assert not res.passed
    notes = " ".join(res.diagnosis())
    assert "DIO 3 draws bit 4's current" in notes and "J4, not J3" in notes


@pytest.mark.parametrize("source", ["dmm", "psu"])
def test_dead_smallest_bit_fails(jafwak, source):
    """B0 is 1 mA after the refit: with the old 1.5 mA absolute tolerance a dead B0 passed."""
    res, _ = run_with(jafwak, source=source, setup=lambda b: _drop_pins(b, 0))
    assert [r["ok"] for r in res.rows] == [False, True, True, True, True]
    assert "DIO 0 (bit 0) draws no current" in res.diagnosis()[0]


def test_uniform_shortfall_is_not_called_a_swap(jafwak):
    """Every bit at ~65 % of V/R (bench, 2026-11-14): one note about the voltage or the reading."""
    res, _ = run_with(jafwak, setup=lambda b: setattr(b.sim_model, "bank_series_drop", 0.43))
    assert not res.passed
    notes = res.diagnosis()
    assert len(notes) == 1, notes
    assert "every bit draws" in notes[0] and "not a wiring fault" in notes[0]
    assert "J" not in notes[0].split("not a wiring fault")[0].split("draws")[0]
    assert "wired to J" not in " ".join(notes)


def test_psu_readback_off_but_dmm_right(jafwak):
    """The DMM decides; the report says the supply's current readback disagrees with it."""
    res, _ = run_with(jafwak, setup=lambda b: setattr(b.sim_model, "psu_i_gain", 0.65))
    assert res.passed, res.report()
    assert any("supply's current readback" in n and "-35 %" in n for n in res.notes())
    assert "supply's current readback" in res.report()


def test_voltage_capped():
    with pytest.raises(ValueError):
        LoadBankCheckConfig(voltage=3.3)


# --- CLI ------------------------------------------------------------------------------------
def test_cli_refuses_dmm_mode_without_a_dmm(jafwak):
    from ldo_bench.cli import _need_dmm

    cfg = BenchConfig.model_validate(jafwak.model_dump() | {"dmm": None})
    with pytest.raises(Exception) as e:
        _need_dmm(cfg, "loadbank-check --current-from dmm")
    assert getattr(e.value, "exit_code", None) == 2


def test_cli_current_from_psu(tmp_path):
    r = invoke("loadbank-check", "-c", config_path(), "--sim", "--yes", "--current-from", "psu")
    assert r.exit_code == 0, r.output
    r = invoke("loadbank-check", "-c", config_path(), "--sim", "--yes")
    assert r.exit_code == 0, r.output
    assert "DMM" in r.output
