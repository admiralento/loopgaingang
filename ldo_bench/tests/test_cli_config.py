"""Which config a command loads: configs/jafwak.toml unless -c or LDO_BENCH_CONFIG says otherwise."""

import pytest
from helpers import config_path, invoke


@pytest.fixture(autouse=True)
def _no_env_config(monkeypatch):
    monkeypatch.delenv("LDO_BENCH_CONFIG", raising=False)


def test_without_c_it_loads_jafwak():
    r = invoke("check-config")
    assert r.exit_code == 0, r.output
    assert "jafwak.toml" in r.output.splitlines()[0] and "OK" in r.output


def test_c_overrides_the_default():
    r = invoke("check-config", "-c", config_path("demo_board"))
    assert r.exit_code == 0, r.output
    assert "demo_board.toml" in r.output.splitlines()[0] and "OK" in r.output


def test_env_var_overrides_the_default(monkeypatch):
    monkeypatch.setenv("LDO_BENCH_CONFIG", config_path("demo_board"))
    r = invoke("check-config")
    assert r.exit_code == 0, r.output
    assert "demo_board.toml" in r.output.splitlines()[0] and "OK" in r.output


def test_every_run_says_which_config_it_loaded():
    r = invoke("smoke", "--sim", "--yes")
    assert r.exit_code == 0, r.output
    assert r.output.lstrip().startswith("config: ")
    assert "jafwak.toml" in r.output.splitlines()[0]
