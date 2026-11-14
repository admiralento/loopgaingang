"""enable-startup measuring input and output current together: AD2 scope 1 across R13, scope 2 across R1,
VOUT on the 54621A (triggered on EN_AIN, 1.8 V logic). No scope ever goes on VREF (it loads the BGR)."""

import pytest
from helpers import config_path, invoke, miswire, spy
from pydantic import ValidationError

from ldo_bench.bench import open_bench
from ldo_bench.config import BenchConfig, StartupConfig
from ldo_bench.instruments.base import InstrumentError
from ldo_bench.procedures import startup


def _run(cfg, *, xscope=True, setup=None, **upd):
    su = cfg.startup.model_copy(update={"averages": 2, **upd})
    with open_bench(cfg, sim=True, use_xscope=xscope) as b:
        if setup:
            setup(b)
        return startup.run(b, su), b


# --- config ---------------------------------------------------------------------------
def test_default_is_both_currents(jafwak):
    assert jafwak.startup.channels == "currents"


def test_vref_mode_is_gone(jafwak):
    with pytest.raises(ValidationError):
        StartupConfig.model_validate(jafwak.startup.model_dump() | {"channels": "vref"})


def test_old_ch2_key_says_what_replaced_it(jafwak):
    data = jafwak.startup.model_dump()
    data["ch2"] = "iin"
    with pytest.raises(ValidationError, match="channels"):
        StartupConfig.model_validate(data)


# --- wiring ---------------------------------------------------------------------------
def test_currents_wiring(jafwak):
    with open_bench(jafwak, sim=True) as b:
        lines = startup.wiring(b, jafwak.startup)
    text = "\n".join(lines)
    a, x = jafwak.ad2, jafwak.xscope
    assert f"Scope {a.vout_channel}+ -> J13 pin 1 (INA), Scope {a.vout_channel}- -> J13 pin 2 (INB)" in text
    assert f"Scope {a.ref_channel}+ -> J13 pin 4 (OUTA), Scope {a.ref_channel}- -> J13 pin 3 (OUTB)" in text
    assert any(ln.startswith(f"54621A ch{x.vout_channel}+ -> J13 pin 4 (OUTA)") for ln in lines)
    assert any(ln.startswith(f"54621A ch{x.ref_channel}+ -> EN_AIN (TP6)") and "1.8 V" in ln for ln in lines)
    assert "TP3" not in text and "VREF" not in text


@pytest.mark.parametrize("channels", ["currents", "iin", "iout"])
def test_no_mode_probes_vref(jafwak, channels):
    with open_bench(jafwak, sim=True) as b:
        text = "\n".join(startup.wiring(b, jafwak.startup.model_copy(update={"channels": channels})))
    assert "TP3" not in text and "VREF" not in text


# --- run --------------------------------------------------------------------------------
def test_currents_run(jafwak):
    res, b = _run(jafwak, load_code=2)
    assert not res.issues, res.issues
    on = res.edges[0]
    assert on.vout_source.startswith("54621A")
    assert on.vout_m.after == pytest.approx(jafwak.dut.vout_nominal, abs=0.02)
    assert [c.name for c in on.currents] == ["iin", "iout"]
    iin, iout = on.currents
    assert iout.m.peak > iout.m.after  # inrush: C dV/dt on top of the load current
    assert iin.m.after > iout.m.after  # the input also carries the LDO's own current
    assert not b.sim_model.dio.get(jafwak.enable.dio_pin)  # enable low afterwards
    startup.plot(res, "x")


def test_xscope_is_armed_for_a_startup_edge(jafwak):
    arms = []
    _run(jafwak, setup=lambda b: spy(b.xscope, "arm", arms), edges=["rising"])
    ((_, _, k),) = arms
    su, x = jafwak.startup, jafwak.xscope
    assert k["vout_channel"] == x.vout_channel and k["ref_channel"] == x.ref_channel
    assert k["vout_range"] == su.xscope_vout_range
    assert k["vout_offset"] == pytest.approx(jafwak.dut.vout_nominal / 2)
    assert k["window"] == pytest.approx(su.window) and k["pre_trigger"] == su.pre_trigger
    assert k["edge"] == "rising" and k["averages"] == 2
    assert su.xscope_trigger_high == pytest.approx(1.8)
    assert k["trigger_level"] == pytest.approx(0.9)  # EN_AIN is 1.8 V logic, not the 3.3 V DIO
    assert k["ref_range"] == pytest.approx(3.6) and k["ref_offset"] == pytest.approx(0.9)


def test_no_xscope_means_no_vout_and_an_invalid_run(jafwak):
    res, _ = _run(jafwak, xscope=False)
    assert res.issues and all("VOUT" in i for i in res.issues)
    assert all(e.vout_m is None and len(e.currents) == 2 for e in res.edges)  # currents still kept
    assert "NOT A VALID" in res.report()
    startup.plot(res, "x")


def test_xscope_failure_is_recorded(jafwak):
    def broken(b):
        def collect(*a, **k):
            raise InstrumentError("never finished its averaged acquisition")

        b.xscope.collect = collect

    res, _ = _run(jafwak, setup=broken)
    assert any("54621A" in i and "never finished" in i for i in res.issues)


def test_vout_checks_use_the_xscope_trace(jafwak):
    res, _ = _run(jafwak, setup=lambda b: miswire(b, wiring_kw={"en_pin": None}))
    assert any("already" in i and "before the enable edge" in i for i in res.issues)


@pytest.mark.parametrize("channels", ["iin", "iout"])
def test_fallback_uses_the_ad2_only(jafwak, channels):
    arms = []
    res, _ = _run(jafwak, setup=lambda b: spy(b.xscope, "arm", arms), channels=channels, load_code=2)
    assert not arms  # the 54621A isn't used even if it's there
    assert not res.issues, res.issues
    on = res.edges[0]
    assert on.vout_source.startswith("AD2")
    assert [c.name for c in on.currents] == [channels]
    startup.plot(res, "x")


# --- CLI --------------------------------------------------------------------------------
def test_cli_saves_vout_from_the_xscope(tmp_path):
    r = invoke("enable-startup", "-c", config_path(), "--sim", "--yes", "--save-sim", "-o", str(tmp_path))
    assert r.exit_code == 0, r.output
    run = next(p for p in (tmp_path / "enable-startup").iterdir() if p.is_dir())
    assert (run / "waveform_rising_vout.csv").exists()
    head = (run / "waveform_rising.csv").read_text().splitlines()[0]
    assert head == "t_s,iin_a,iout_a"


def test_cli_fallback_and_old_option():
    r = invoke("enable-startup", "-c", config_path(), "--sim", "--yes", "--channels", "iout")
    assert r.exit_code == 0, r.output
    r = invoke("enable-startup", "-c", config_path(), "--sim", "--yes", "--ch2", "vref")
    assert r.exit_code == 2


def test_currents_refused_without_xscope_section(jafwak):
    from ldo_bench.cli import _need_xscope

    cfg = BenchConfig.model_validate(jafwak.model_dump() | {"xscope": None})
    with pytest.raises(Exception) as e:
        _need_xscope(cfg, cfg.startup)
    assert getattr(e.value, "exit_code", None) == 2


def test_inrush_peak_is_reported_even_when_the_step_is_under_the_floor(jafwak):
    res, _ = _run(jafwak, load_code=2, edges=["rising"])  # 9.6 mA step < 12.2 mA floor; inrush far above
    iin = res.edges[0].currents[0]
    assert not iin.resolved and abs(iin.m.peak) >= iin.floor
    assert f"peak {iin.m.peak * 1e3:.1f} mA" in res.report()


def test_vout_time_zero_is_named(jafwak):
    res, _ = _run(jafwak, edges=["rising"])
    assert "t = 0 at EN_AIN" in res.report()
