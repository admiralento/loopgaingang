"""--ext-load-ohm: a fixed resistor on VOUT (e.g. 1.2 kOhm on J5) whose current the scripts add."""

import numpy as np
import pytest
from helpers import invoke
from pydantic import ValidationError

from ldo_bench.bench import open_bench
from ldo_bench.config import BenchConfig
from ldo_bench.instruments.base import InstrumentError
from ldo_bench.procedures import line_reg, load_reg


def with_ext(cfg, ohm):
    data = cfg.model_dump()
    data["loadbank"]["external_ohm"] = ohm
    return BenchConfig.model_validate(data)


def test_load_reg_adds_the_external_current(jafwak):
    cfg = with_ext(jafwak, 12e3)  # 0.1 mA: the top code (19.2 mA) stays under dut.i_max
    with open_bench(cfg, sim=True) as b:
        res = load_reg.run(b, cfg.load_reg)
    assert not res.issues, res.issues
    for row in res.rows:
        bank_only = jafwak.loadbank.conductance(row["code"]) * row["vout_v"]
        assert row["i_est_a"] == pytest.approx(bank_only + row["vout_v"] / 12e3, rel=1e-6)
    # the simulated board really carries it: VOUT at code 0 sits lower than with no external load
    with open_bench(jafwak, sim=True) as b:
        plain = load_reg.run(b, jafwak.load_reg)
    assert res.rows[0]["vout_v"] < plain.rows[0]["vout_v"]


def test_external_load_counts_toward_i_max_before_power_up(jafwak):
    cfg = with_ext(jafwak, 1200.0)  # +1 mA: code 4 (19.2 mA) becomes 20.2 mA, over the 20 mA limit
    assert cfg.line_reg.load_code == 4
    with open_bench(cfg, sim=True) as b, pytest.raises(InstrumentError, match="external load"):
        line_reg.run(b, cfg.line_reg)
    assert not b.sim_model.psu_on  # refused before anything powered up


def test_load_reg_sweep_leaves_out_codes_over_the_limit_and_says_so(jafwak):
    cfg = with_ext(jafwak, 1200.0)
    with open_bench(cfg, sim=True) as b:
        res = load_reg.run(b, cfg.load_reg)
    assert [r["code"] for r in res.rows] == [0, 1, 2, 3]
    assert any("code(s) 4" in n for n in res.notes)


def test_external_ohm_must_be_positive(jafwak):
    with pytest.raises(ValidationError):
        with_ext(jafwak, 0.0)


def test_wiring_names_the_external_load(jafwak):
    cfg = with_ext(jafwak, 12e3)
    with open_bench(cfg, sim=True) as b:
        load_reg.run(b, cfg.load_reg)
        assert "12 kOhm external load from VOUT (J5) to GND (J12) (always connected)" in b.last_wiring


def test_cli_option(tmp_path):
    r = invoke("load-reg", "--sim", "--yes", "-o", str(tmp_path), "--ext-load-ohm", "12000")
    assert r.exit_code == 0, r.output


def test_line_reg_points_to_vref_line_for_the_bgr_knee(jafwak):
    with open_bench(jafwak, sim=True) as b:
        res = line_reg.run(b, jafwak.line_reg)
    assert np.isfinite(res.reg.dropout_vin)
    assert any("vref-line" in n for n in res.notes)
