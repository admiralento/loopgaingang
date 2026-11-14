"""The supply's current limit: psu.current_limit for every run, psu.high_current_limit only for the
tests whose section says high_current = true, and no test configured to draw more than its limit."""

import json

import pytest
from helpers import config_path, invoke, spy
from pydantic import ValidationError

from ldo_bench.bench import open_bench
from ldo_bench.config import BenchConfig
from ldo_bench.instruments.base import InstrumentError
from ldo_bench.procedures import iq, line_reg, load_reg, load_tran, loadbank_check


def _with(cfg: BenchConfig, **sections) -> BenchConfig:
    """cfg re-validated with some sections' fields changed: _with(cfg, psu={"current_limit": 0.5})."""
    data = cfg.model_dump()
    for name, fields in sections.items():
        data[name] = (data[name] or {}) | fields
    return BenchConfig.model_validate(data)


def _limit_used(bench, run) -> float:
    """The current_limit the PSU was set up with, checking it was set before the output first turned on."""
    calls = []
    spy(bench.psu, "setup", calls)
    spy(bench.psu, "output", calls)
    run()
    first_on = next(i for i, (n, a, _) in enumerate(calls) if n == "output" and a[0] is True)
    setups = [i for i, (n, _, _) in enumerate(calls) if n == "setup"]
    assert setups and setups[0] < first_on, "the current limit must be set before the output turns on"
    return calls[setups[0]][2]["current_limit"]


# --- config ------------------------------------------------------------------------
def test_jafwak_limits(jafwak):
    assert jafwak.psu.current_limit == pytest.approx(0.010)
    assert jafwak.psu.high_current_limit == pytest.approx(0.060)
    for sec in (jafwak.load_reg, jafwak.line_reg, jafwak.load_tran, jafwak.smoke, jafwak.loadbank_check):
        assert sec.high_current


def test_high_limit_must_be_above_the_normal_one(jafwak):
    with pytest.raises(ValidationError, match="high_current_limit"):
        _with(jafwak, psu={"high_current_limit": 0.005})


def test_high_current_needs_a_high_limit(jafwak):
    with pytest.raises(ValidationError, match="high_current_limit"):
        _with(jafwak, psu={"high_current_limit": None})


@pytest.mark.parametrize(
    "section, fields",
    [
        ("load_reg", {"high_current": False}),  # every code up to 19.2 mA on a 10 mA limit
        ("line_reg", {"high_current": False}),  # load_code 4 = 19.2 mA
        ("load_tran", {"high_current": False}),  # code_high 4
        ("smoke", {"high_current": False}),  # full load
        ("loadbank_check", {"voltage": 1.0}),  # B4: 1.0 V / 15.4 ohm = 65 mA > 60 mA
        ("iq", {"check_code": 4}),  # iq has no high limit: 19.2 mA > 10 mA
    ],
)
def test_a_load_above_the_limit_is_refused(jafwak, section, fields):
    with pytest.raises(ValidationError, match="current limit"):
        _with(jafwak, **{section: fields})


def test_a_light_load_runs_on_the_normal_limit(jafwak):
    cfg = _with(jafwak, line_reg={"high_current": False, "load_code": 1})  # 4.8 mA
    assert cfg.psu_limit_for(cfg.line_reg) == pytest.approx(0.010)


def test_demo_board_unchanged(cfg):
    if cfg.dut.name.startswith("TPS"):
        assert cfg.psu.current_limit == pytest.approx(0.3) and cfg.psu.high_current_limit is None


# --- run time: which limit each test sets up -----------------------------------------
@pytest.mark.parametrize(
    "module, section, extra",
    [
        (load_reg, "load_reg", ()),
        (line_reg, "line_reg", ()),
        (load_tran, "load_tran", ("analysis",)),
        (loadbank_check, "loadbank_check", ()),
    ],
)
def test_high_current_tests_use_the_high_limit(jafwak, module, section, extra):
    with open_bench(jafwak, sim=True) as b:
        limit = _limit_used(
            b, lambda: module.run(b, getattr(jafwak, section), *(getattr(jafwak, e) for e in extra))
        )
    assert limit == pytest.approx(jafwak.psu.high_current_limit)


def test_other_tests_use_the_normal_limit(jafwak):
    with open_bench(jafwak, sim=True) as b:
        assert _limit_used(b, lambda: iq.run(b, jafwak.iq)) == pytest.approx(jafwak.psu.current_limit)


def test_smoke_uses_the_high_limit(monkeypatch):
    from ldo_bench import bench as bench_mod

    seen = []
    real = bench_mod.Bench.set_psu_limit

    def record(self, **k):
        seen.append(real(self, **k))
        return seen[-1]

    monkeypatch.setattr(bench_mod.Bench, "set_psu_limit", record)
    r = invoke("smoke", "-c", config_path(), "--sim", "--yes")
    assert r.exit_code == 0, r.output
    assert seen == [pytest.approx(0.060)]
    assert "current limit 60 mA" in r.output


def test_set_psu_limit_refusals(jafwak):
    with open_bench(jafwak, sim=True) as b:
        with pytest.raises(InstrumentError, match="above"):
            b.set_psu_limit(amps=0.011)  # over psu.current_limit
        with pytest.raises(InstrumentError, match="above"):
            b.set_psu_limit(high=True, amps=0.061)  # over psu.high_current_limit
        b.set_psu_limit(amps=0.005)
        with pytest.raises(InstrumentError, match="current limit"):
            b.set_psu_limit(high=True)  # already set up with 5 mA


def test_set_psu_limit_high_without_a_high_limit():
    from helpers import CONFIG_DIR

    from ldo_bench.config import load_config

    demo = load_config(CONFIG_DIR / "demo_board.toml")  # no psu.high_current_limit
    with open_bench(demo, sim=True) as b, pytest.raises(InstrumentError, match="high_current_limit"):
        b.set_psu_limit(high=True)


def test_meta_records_the_limit(tmp_path):
    r = invoke("load-reg", "-c", config_path(), "--sim", "--yes", "--save-sim", "-o", str(tmp_path))
    assert r.exit_code == 0, r.output
    meta = json.loads(next(tmp_path.rglob("meta.json")).read_text())
    assert meta["psu_current_limit_a"] == pytest.approx(0.060)
