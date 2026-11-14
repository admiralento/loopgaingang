"""enable-startup: input current from the 54621A as ch1 (INA) - ch2 (INB) across R13, VOUT and EN_IN on
the AD2. The 54621A's channels are ground-referenced (its grounds and the AD2's are common), so the
voltage across R13 is the difference of two channels, zeroed at the disabled level."""

import numpy as np
import pytest
from helpers import config_path, invoke, miswire, spy
from pydantic import ValidationError

from ldo_bench.bench import open_bench
from ldo_bench.config import BenchConfig, StartupConfig
from ldo_bench.instruments.agilent546xx import ScopeTrace
from ldo_bench.instruments.base import InstrumentError
from ldo_bench.procedures import startup


def _run(cfg, *, xscope=True, setup=None, **upd):
    su = cfg.startup.model_copy(update={"averages": 2, **upd})
    with open_bench(cfg, sim=True, use_xscope=xscope) as b:
        if setup:
            setup(b)
        return startup.run(b, su), b


# --- config ---------------------------------------------------------------------------
def test_defaults(jafwak):
    su = jafwak.startup
    assert (su.xscope_ina_channel, su.xscope_inb_channel) == (1, 2)
    assert su.xscope_shunt_range == pytest.approx(0.16)
    assert su.xscope_trigger_high == pytest.approx(1.8)


@pytest.mark.parametrize("old", ["channels", "ch2", "sense_range", "xscope_vout_range", "vref_nominal"])
def test_removed_keys_say_what_replaced_them(jafwak, old):
    data = jafwak.startup.model_dump() | {old: "x"}
    with pytest.raises(ValidationError, match="removed"):
        StartupConfig.model_validate(data)


def test_ina_and_inb_on_different_channels(jafwak):
    with pytest.raises(ValidationError, match="xscope_inb_channel"):
        StartupConfig.model_validate(jafwak.startup.model_dump() | {"xscope_inb_channel": 1})


# --- wiring ---------------------------------------------------------------------------
def test_wiring(jafwak):
    with open_bench(jafwak, sim=True) as b:
        lines = startup.wiring(b, jafwak.startup)
    text = "\n".join(lines)
    a, b_ = jafwak.ad2, jafwak.board
    assert f"Scope {a.vout_channel}+ -> J13 pin 4 (OUTA)" in text
    assert f"Scope {a.ref_channel}+ -> {b_.enable_ldo_in}" in text
    assert any(
        ln.startswith("54621A ch1+ -> J13 pin 1 (INA), 54621A ch1 ground -> J13 pin 6 (GND)") for ln in lines
    )
    assert any(
        ln.startswith("54621A ch2+ -> J13 pin 2 (INB), 54621A ch2 ground -> J13 pin 6 (GND)") for ln in lines
    )
    assert any(ln.startswith("54621A Ext Trig -> EN_AIN (TP6)") and "1.8 V" in ln for ln in lines)
    assert "ground clips on GND only" in text  # never on INB: the scope grounds are common
    assert "TP3" not in text and "VREF" not in text


# --- run --------------------------------------------------------------------------------
def test_run(jafwak):
    res, b = _run(jafwak, load_code=2)
    assert not res.issues, res.issues
    on, off = res.edges
    assert on.vout_m.after == pytest.approx(jafwak.dut.vout_nominal, abs=0.02)
    assert on.en_in[-1] > 1.0 and on.en_in[0] < 0.2  # EN_IN on the AD2 steps up
    i = on.iin
    assert i is not None and i.m.peak > 0.1  # the inrush (sim: ~340 mA) is far above the floor
    assert abs(i.m.before) < i.floor  # zeroed at the disabled level before the enable edge
    assert abs(off.iin.m.after) < off.iin.floor  # ... and after the disable edge
    assert not b.sim_model.dio.get(jafwak.enable.dio_pin)
    startup.plot(res, "x")


def test_xscope_is_armed_on_the_shunt_with_the_external_trigger(jafwak):
    arms = []
    _run(jafwak, setup=lambda b: spy(b.xscope, "arm", arms), edges=["rising"])
    ((_, _, k),) = arms
    su = jafwak.startup
    assert k["trigger"] == "external" and k["trigger_level"] == pytest.approx(0.9)  # 1.8 V logic
    assert (k["vout_channel"], k["ref_channel"]) == (su.xscope_ina_channel, su.xscope_inb_channel)
    assert (
        k["vout_range"] == k["ref_range"] == su.xscope_shunt_range
    )  # same scale: the difference is meaningful
    assert k["vout_offset"] == k["ref_offset"] == pytest.approx(su.vin)  # both zoomed in on VIN
    assert k["window"] == pytest.approx(su.window) and k["edge"] == "rising"


def test_current_floor_is_reported_as_a_bound(jafwak):
    res, _ = _run(jafwak, load_code=0)  # ~150 uA of Iq: far under 4 counts of the 8-bit scope
    for e in res.edges:
        assert not e.iin.resolved
        assert np.isnan(e.iin.m.t10)
    assert "resolve" in res.report()


def test_no_xscope_means_no_current_and_an_invalid_run(jafwak):
    res, _ = _run(jafwak, xscope=False)
    assert res.issues and all("input current" in i for i in res.issues)
    assert all(e.iin is None and e.vout_m is not None for e in res.edges)  # VOUT still measured
    startup.plot(res, "x")


def test_xscope_failure_is_recorded(jafwak):
    def broken(b):
        def collect(*a, **k):
            raise InstrumentError("never finished its averaged acquisition")

        b.xscope.collect = collect

    res, _ = _run(jafwak, setup=broken)
    assert any("never finished" in i for i in res.issues)


def test_clipped_channel_is_flagged(jafwak):
    def clipped(b):
        su = jafwak.startup

        def collect(*a, **k):
            t = np.linspace(-1e-3, 9e-3, 2000)
            top = su.vin + su.xscope_shunt_range / 2
            ina = np.full_like(t, su.vin)
            inb = np.where(t > 0, su.vin - su.xscope_shunt_range, su.vin)  # VIN sags off the screen
            inb = np.clip(inb, su.vin - su.xscope_shunt_range / 2, top)
            return {1: ScopeTrace(t, ina, 2), 2: ScopeTrace(t, inb, 2)}

        b.xscope.collect = collect

    res, _ = _run(jafwak, setup=clipped, edges=["rising"])
    assert any("clipped" in i and "ch2" in i for i in res.issues)


def test_vout_checks_still_run(jafwak):
    res, _ = _run(jafwak, setup=lambda b: miswire(b, wiring_kw={"en_pin": None}))
    assert any("already" in i and "before the enable edge" in i for i in res.issues)


def test_unsettled_vout_flagged(jafwak):
    res, _ = _run(jafwak, window=40e-6)
    assert any("settle" in i for i in res.issues)


# --- CLI --------------------------------------------------------------------------------
def test_cli_saves_both_time_axes(tmp_path):
    r = invoke("enable-startup", "-c", config_path(), "--sim", "--yes", "--save-sim", "-o", str(tmp_path))
    assert r.exit_code == 0, r.output
    run = next(p for p in (tmp_path / "enable-startup").iterdir() if p.is_dir())
    assert (run / "waveform_rising.csv").read_text().splitlines()[0] == "t_s,vout_v,en_in_v"
    assert (run / "waveform_rising_iin.csv").read_text().splitlines()[0] == "t_s,ina_v,inb_v,iin_a"


def test_cli_channels_option_is_gone():
    r = invoke("enable-startup", "-c", config_path(), "--sim", "--yes", "--channels", "iin")
    assert r.exit_code == 2


def test_refused_without_xscope_section(jafwak):
    from ldo_bench.cli import _need_xscope

    cfg = BenchConfig.model_validate(jafwak.model_dump() | {"xscope": None})
    with pytest.raises(Exception) as e:
        _need_xscope(cfg)
    assert getattr(e.value, "exit_code", None) == 2
