from pathlib import Path

import pytest
from pydantic import ValidationError

from ldo_bench.config import BenchConfig, load_config

CONFIG = Path(__file__).parents[1] / "configs" / "jafwak.toml"


def test_shipped_config_is_valid():
    cfg = load_config(CONFIG)
    assert cfg.loadbank.max_code == 31


def _base() -> dict:
    return {
        "dut": {"vout_nominal": 1.8},
        "psu": {"current_limit": 0.4, "vin_max": 5.5, "ovp": 6.0},
        "loadbank": {"dio_pins": [0, 1], "resistors": [100.0, 50.0]},
    }


def test_unknown_key_rejected():
    d = _base()
    d["psu"]["curent_limit"] = 1.0  # typo
    with pytest.raises(ValidationError):
        BenchConfig.model_validate(d)


def test_vin_above_ceiling_rejected():
    d = _base()
    d["load_reg"] = {"vin": 6.0}
    with pytest.raises(ValidationError, match="vin_max"):
        BenchConfig.model_validate(d)


def test_code_outside_bank_rejected():
    d = _base()
    d["line_reg"] = {"vin_start": 2, "vin_stop": 5, "vin_step": 0.1, "load_code": 4}
    with pytest.raises(ValidationError, match="load bank"):
        BenchConfig.model_validate(d)


def test_buffer_headroom_enforced():
    d = _base()
    d["line_tran"] = {"psu_voltage": 5.0, "vin_low": 2.8, "vin_high": 3.3}
    with pytest.raises(ValidationError, match="BUF634"):
        BenchConfig.model_validate(d)
