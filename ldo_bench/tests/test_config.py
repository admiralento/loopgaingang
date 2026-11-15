from pathlib import Path

import pytest
from pydantic import ValidationError

from ldo_bench.config import BenchConfig, load_config

CONFIGS = sorted((Path(__file__).parents[1] / "configs").glob("*.toml"))


@pytest.mark.parametrize("path", CONFIGS, ids=lambda p: p.stem)
def test_shipped_configs_are_valid(path):
    cfg = load_config(path)
    assert cfg.loadbank.max_code == 31


def test_both_boards_shipped():
    assert {p.stem for p in CONFIGS} >= {"jafwak", "demo_board"}


def _base() -> dict:
    return {
        "dut": {"vout_nominal": 1.2, "vin_max": 3.6},
        "psu": {"current_limit": 0.4, "v_max": 5.5, "ovp": 6.0},
        "loadbank": {"dio_pins": [0, 1], "resistors": [100.0, 50.0]},
    }


def test_unknown_key_rejected():
    d = _base()
    d["psu"]["curent_limit"] = 1.0  # typo
    with pytest.raises(ValidationError):
        BenchConfig.model_validate(d)


def test_vin_above_ceiling_rejected():
    d = _base()
    d["load_reg"] = {"vin": 4.0}  # below psu.v_max but above dut.vin_max
    with pytest.raises(ValidationError, match=r"dut\.vin_max"):
        BenchConfig.model_validate(d)


def test_line_tran_may_exceed_dut_vin_on_buffer_rail_only():
    d = _base()
    d["line_tran"] = {"psu_voltage": 4.5, "vin_low": 1.6, "vin_high": 2.0}
    BenchConfig.model_validate(d)  # 4.5 V on VS_BUFF is fine
    d["line_tran"] = {"psu_voltage": 5.5, "vin_low": 3.0, "vin_high": 3.8, "buffer_headroom": 1.0}
    with pytest.raises(ValidationError, match=r"dut\.vin_max"):
        BenchConfig.model_validate(d)


def test_code_outside_bank_rejected():
    d = _base()
    d["line_reg"] = {"vin_start": 1.3, "vin_stop": 3.3, "vin_step": 0.1, "load_code": 4}
    with pytest.raises(ValidationError, match="load bank"):
        BenchConfig.model_validate(d)


def test_buffer_headroom_enforced():
    d = _base()
    d["line_tran"] = {"psu_voltage": 3.5, "vin_low": 1.6, "vin_high": 2.0}
    with pytest.raises(ValidationError, match="BUF634"):
        BenchConfig.model_validate(d)
