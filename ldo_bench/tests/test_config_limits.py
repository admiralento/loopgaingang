"""Every config cross-check refuses its bad value, and CLI options go through the same checks."""

from pathlib import Path

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from ldo_bench.cli import app
from ldo_bench.config import BenchConfig

CONFIGS = Path(__file__).parents[1] / "configs"

# (description, change to the JAFWAK config, text the error must contain)
CASES = [
    ("line_reg sweep over vin_max", {"line_reg": {"vin_stop": 2.0}}, "line_reg sweep"),
    ("load_tran.vin over vin_max", {"load_tran": {"vin": 2.0}}, "load_tran.vin"),
    ("en_sine.vin over vin_max", {"en_sine": {"vin": 2.0}}, "en_sine.vin"),
    ("startup.vin over vin_max", {"startup": {"vin": 2.0}}, "startup.vin"),
    ("iq sweep over vin_max", {"iq": {"vin_stop": 2.0}}, "iq sweep"),
    ("load_reg code over i_max", {"load_reg": {"codes": [22]}}, "load_reg.codes"),
    ("line_reg code over i_max", {"line_reg": {"load_code": 22}}, "line_reg.load_code"),
    ("startup code over i_max", {"startup": {"load_code": 22}}, "startup.load_code"),
    ("iq check code over i_max", {"iq": {"check_code": 22}}, "iq.check_code"),
    ("psrr code over i_max", {"psrr": {"load_code": 22}}, "psrr.load_code"),
    ("load_tran low code not below high", {"load_tran": {"code_low": 4, "code_high": 4}}, "code_low"),
    ("OVP at or below v_max", {"psu": {"ovp": 5.0}}, "must be above psu.v_max"),
    ("line_tran rail over v_max", {"line_tran": {"psu_voltage": 6.0}}, "line_tran.psu_voltage"),
    ("psrr rail over v_max", {"psrr": {"psu_voltage": 6.0}}, "psrr.psu_voltage"),
    ("line_tran step inside the AD2 error", {"line_tran": {"vin_high": 1.89}}, "measurement error"),
    ("psrr peak inside the AD2 error", {"psrr": {"amplitude": 0.09}}, "measurement error"),
    ("enable pins clash (startup)", {"enable": {"dio_pin": 2}, "en_sine": None, "iq": None}, "enable DIO"),
    ("enable pins clash (iq)", {"enable": {"dio_pin": 2}, "en_sine": None, "startup": None}, "enable DIO"),
]


def jafwak_with(change: dict) -> dict:
    from ldo_bench.config import load_config

    data = load_config(CONFIGS / "jafwak.toml").model_dump()
    for section, fields in change.items():
        data[section] = None if fields is None else data[section] | fields
    return data


def test_jafwak_itself_is_valid():
    BenchConfig.model_validate(jafwak_with({}))


@pytest.mark.parametrize(("what", "change", "match"), CASES, ids=[c[0] for c in CASES])
def test_cross_check_refuses(what, change, match):
    with pytest.raises(ValidationError, match=match):
        BenchConfig.model_validate(jafwak_with(change))


runner = CliRunner()


@pytest.mark.parametrize(
    "args",
    [
        ["load-reg", "--vin", "2.5"],  # over JAFWAK's vin_max
        ["psrr", "--amp-mv", "200"],  # 1.8 V + 0.2 V peak
        ["line-reg", "--load", "22"],  # over i_max
    ],
)
def test_cli_options_are_validated(args, tmp_path):
    r = runner.invoke(
        app, [args[0], "-c", str(CONFIGS / "jafwak.toml"), "--sim", "-o", str(tmp_path), *args[1:]]
    )
    assert r.exit_code == 2, r.output
