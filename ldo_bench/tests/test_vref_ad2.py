"""line-tran --vref ad2: the AD2's V+ supply on VREF_IN (JP2 removed), read back on the DMM.

V+ may only drive VREF_IN while the LDO is powered: after VIN is up and measured, and off again
before VIN goes away (end of the run, an error, the rails coming down).
"""

import pytest
from helpers import config_path, invoke

from ldo_bench.bench import Ad2VplusReference, open_bench
from ldo_bench.instruments.base import InstrumentError
from ldo_bench.procedures import line_tran


def _open(cfg, **kw):
    return open_bench(cfg, sim=True, vref_external=True, vref_source="ad2", use_dmm=True, **kw)


def _lt(cfg):
    return cfg.line_tran.model_copy(update={"averages": 2})


def _watch(b, events):
    """Record V+ and PSU changes with the state of the simulated board at that moment."""
    m = b.sim_model
    set_vplus, psu_set, psu_out = b.scope.set_vplus, b.psu.set_voltage, b.psu.output

    def vplus(v):
        events.append(("vplus", v, m.psu_on, m.vin))
        set_vplus(v)

    def psu_v(v):
        events.append(("psu_v", v, b.scope.vplus, m.vin))
        psu_set(v)

    def output(on):
        events.append(("psu_out", on, b.scope.vplus, m.vin))
        psu_out(on)

    b.scope.set_vplus, b.psu.set_voltage, b.psu.output = vplus, psu_v, output


def test_line_tran_runs_on_the_ad2_reference(jafwak):
    with _open(jafwak) as b:
        res = line_tran.run(b, _lt(jafwak), jafwak.analysis)
        info = b.vref_info()
        m = b.sim_model
    assert not res.issues, res.issues
    assert info["source"] == "external" and info["driver"] == "AD2 V+"
    target = jafwak.vref.external_v
    assert info["proof"]["dmm_v"] == pytest.approx(target, abs=1e-3)  # read back on the DMM
    assert info["dmm_end_v"] == pytest.approx(target, abs=1e-3)
    assert m.vplus_v == 0.0  # off at the end


def test_vplus_only_while_the_ldo_is_powered(jafwak):
    """V+ comes on only with the rails up and VIN at its level; every rail step down, and the outputs
    turning off, happen with V+ already off."""
    events: list = []
    with _open(jafwak) as b:
        _watch(b, events)
        line_tran.run(b, _lt(jafwak), jafwak.analysis)
    on = [e for e in events if e[0] == "vplus" and e[1] is not None]
    assert on, "V+ never came on"
    vin_level = jafwak.line_tran.vin_low
    assert all(e[2] and e[3] >= vin_level - 0.01 for e in on), on
    first_on = events.index(on[0])
    last_on = max(i for i, e in enumerate(events) if e[0] == "vplus" and e[1] is not None)
    off_after = next(i for i, e in enumerate(events) if i > last_on and e[0] == "vplus" and e[1] is None)
    down = [i for i, e in enumerate(events) if i > first_on and e[0] in ("psu_v", "psu_out") and not e[1]]
    assert down and all(i > off_after for i in down), "a rail came down with V+ still on VREF_IN"


def test_an_error_mid_run_turns_vplus_off_before_the_rails(jafwak):
    events: list = []
    with pytest.raises(InstrumentError, match="boom"), _open(jafwak) as b:
        _watch(b, events)
        real = b.scope.capture

        def fail(**kw):
            if b.sim_model.vplus_v > 0:
                raise InstrumentError("boom")
            return real(**kw)

        b.scope.capture = fail
        line_tran.run(b, _lt(jafwak), jafwak.analysis)
    on = next(i for i, e in enumerate(events) if e[0] == "vplus" and e[1] is not None)
    off = next(i for i, e in enumerate(events) if i > on and e[0] == "vplus" and e[1] is None)
    outputs_off = [i for i, e in enumerate(events) if i > on and e[0] == "psu_out" and e[1] is False]
    assert outputs_off and all(i > off for i in outputs_off)  # the shutdown: V+ off first


def test_refused_unless_vin_is_up(jafwak):
    """The reference waits for VIN measured at least at VREF: it never drives an unpowered chip."""
    with _open(jafwak) as b:
        b.vin_from_wavegen(0.0)  # _vin_level is set, but nothing is powered
        b._psu_on = True
        b.measure_vin_fine = lambda: 0.02
        with pytest.raises(InstrumentError, match="not powered"):
            b.reference_on()
        assert b.sim_model.vplus_v == 0.0


def test_jp2_still_fitted_stops_the_run(jafwak):
    def jp2_in(b):
        real = b.confirm_wiring

        def confirm(*a, **k):
            real(*a, **k)
            b.sim_model.jp2_fitted = True  # the operator didn't remove JP2

        b.confirm_wiring = confirm

    with pytest.raises(InstrumentError, match="JP2"), _open(jafwak) as b:
        jp2_in(b)
        line_tran.run(b, _lt(jafwak), jafwak.analysis)
    assert b.sim_model.vplus_v == 0.0


def test_vplus_is_not_also_the_enable_logic_rail(jafwak):
    with pytest.raises(InstrumentError, match="V\\+"):
        _open(jafwak, enable_dio=True).__enter__()
    with _open(jafwak) as b, pytest.raises(InstrumentError, match="V\\+"):
        b.enable_logic(True)


def test_needs_the_dmm(jafwak):
    with pytest.raises(InstrumentError, match="DMM"):
        open_bench(jafwak, sim=True, vref_external=True, vref_source="ad2", use_dmm=False).__enter__()


def test_vplus_below_its_minimum_is_off():
    class FakeAd2:
        def __init__(self):
            self.calls = []

        def set_vplus(self, v):
            self.calls.append(v)

    ad = FakeAd2()
    ref = Ad2VplusReference(ad)
    ref.set_voltage(0.25)
    ref.set_voltage(0.75)
    ref.set_voltage(0.0)
    assert ad.calls == [None, 0.75, None]
    assert ref.get_voltage_setpoint() == 0.0


def test_wiring_lists_vplus_the_dmm_and_jp2(jafwak):
    with _open(jafwak) as b:
        line_tran.run(b, _lt(jafwak), jafwak.analysis)
        lines = b.last_wiring
    assert any(ln.startswith("Remove JP2") for ln in lines)
    assert any(ln.startswith("AD2 V+ -> JP2 pin 1 (VREF_IN)") for ln in lines)
    assert any(ln.startswith("DMM V -> JP2 pin 1 (VREF_IN)") for ln in lines)


# --- CLI -------------------------------------------------------------------------------------
def _cli(*args):
    return invoke("line-tran", "-c", config_path("jafwak"), "--sim", "--yes", *args)


def test_cli_vref_ad2_and_vref_v():
    r = _cli("--vref", "ad2")
    assert r.exit_code == 0, r.output
    assert "VREF" in r.output and "1.204" in r.output
    r = _cli("--vref", "ad2", "--vref-v", "1.15")
    assert r.exit_code == 0, r.output
    assert "1.150" in r.output


def test_cli_refuses_bad_combinations():
    assert _cli("--vref", "ad2", "--vref-v", "1.5").exit_code == 2  # above vref.v_max
    assert _cli("--vref-v", "1.15").exit_code == 2  # no external reference asked for
    assert _cli("--vref", "ad2", "--enable", "dio").exit_code == 2  # V+ is the enable logic rail
    r = invoke("load-reg", "-c", config_path("jafwak"), "--sim", "--yes", "--vref", "ad2")
    assert r.exit_code == 2 and "line-tran" in r.output, r.output
