"""Safety layer: every check here must stop a run (InstrumentError) or refuse a setting.

Each test would fail if the check it names were deleted. They run on the simulator.
"""

import itertools

import pytest

from ldo_bench.bench import open_bench
from ldo_bench.config import BenchConfig
from ldo_bench.instruments.base import InstrumentError
from ldo_bench.procedures import en_sine, line_tran, psrr


def spy(obj, name, log, tag=None):
    """Wrap obj.name so each call appends (tag or name, args) to log."""
    orig = getattr(obj, name)

    def wrapped(*a, **k):
        log.append((tag or name, a, k))
        return orig(*a, **k)

    setattr(obj, name, wrapped)


def run_buffer_test(proc, cfg, setup):
    """Run line-tran or psrr on the sim with `setup(bench)` applied; return (exception, PSU setpoints)."""
    setpoints = []
    with open_bench(cfg, sim=True) as b:
        orig = b.confirm_wiring

        def cw(*a, **k):
            orig(*a, **k)
            setup(b)

        b.confirm_wiring = cw
        spy(b.psu, "set_voltage", setpoints)
        with pytest.raises(InstrumentError) as exc:
            if proc is line_tran:
                line_tran.run(b, cfg.line_tran.model_copy(update={"averages": 2}), cfg.analysis)
            else:
                psrr.run(b, cfg.psrr.model_copy(update={"averages": 2}))
    return exc.value, [a[0] for _, a, _ in setpoints]


# --- buffer interlock (line-tran, psrr) -----------------------------------------
@pytest.mark.parametrize("proc", [line_tran, psrr])
def test_interlock_stops_with_psu_on_vin_before_rail_goes_up(proc, jafwak):
    def miswired(b):
        b.sim_model.psu_to = "vin"

    err, seen = run_buffer_test(proc, jafwak, miswired)
    assert "connected to VIN" in str(err)
    assert max(seen) <= line_tran.INTERLOCK_RAIL + 1e-9  # never above the 1 V check level


@pytest.mark.parametrize("proc", [line_tran, psrr])
def test_interlock_stops_with_psu_on_vin_and_vin_probe_off(proc, jafwak):
    """The case the first stage can't see: the probe reads ~0 V whatever VIN does."""

    def miswired_and_probe_off(b):
        b.sim_model.psu_to = "vin"
        b.sim_model.ref_value = lambda: 0.0  # scope 2 floating / not on VIN

    err, seen = run_buffer_test(proc, jafwak, miswired_and_probe_off)
    assert "isn't proven" in str(err)
    assert max(seen) <= jafwak.dut.vin_max + 1e-9  # the rail never went above what VIN can take


@pytest.mark.parametrize("proc", [line_tran, psrr])
def test_interlock_stops_with_vin_probe_off(proc, jafwak):
    def probe_off(b):
        b.sim_model.ref_value = lambda: 0.0

    err, seen = run_buffer_test(proc, jafwak, probe_off)
    assert "isn't proven" in str(err)
    assert max(seen) <= jafwak.dut.vin_max + 1e-9


# --- VIN limit with the AD2's measurement margin ---------------------------------
def test_calibration_stops_inside_the_measurement_margin(jafwak):
    """A VIN reading just under vin_max is already too close: the AD2 could be reading low."""

    def offset(b):
        b.sim_model.buffer_offset = 0.045  # W1 = 1.85 V gives VIN = 1.895 V: < 1.9, > 1.9 - margin

    err, _ = run_buffer_test(line_tran, jafwak, offset)
    assert "above dut.vin_max" in str(err)


def test_calibration_stops_above_vin_max(jafwak):
    def offset(b):
        b.sim_model.buffer_offset = 0.1

    err, _ = run_buffer_test(line_tran, jafwak, offset)
    assert "above dut.vin_max" in str(err)


def test_psrr_peak_stop_turns_the_wavegen_off_first(jafwak):
    events = []

    class TooBig:
        name = "fake"

        def measure(self, f):
            events.append("measure")
            return psrr.Tone(complex(0.2), complex(1e-4), 1e-5, 1e-5, 1.8)  # 2.0 V peak

    with open_bench(jafwak, sim=True) as b:
        orig = b.wavegen_off
        b.wavegen_off = lambda: (events.append("wavegen_off"), orig())
        with pytest.raises(InstrumentError, match=r"above dut\.vin_max"):
            psrr.run(b, jafwak.psrr, TooBig())
        # switched off by the check itself, before the bench's shutdown ran
        assert events.index("wavegen_off", events.index("measure")) > events.index("measure")
        assert not b.sim_model.awg_sine


# --- enable-sine goes through the bench ------------------------------------------
def test_en_sine_refuses_a_sine_above_vin_max_at_run_time(jafwak):
    es = jafwak.en_sine.model_copy(update={"high": 2.5})  # model_copy skips the config check
    with open_bench(jafwak, sim=True) as b, pytest.raises(InstrumentError, match="refusing"):
        en_sine.run(b, es)


# --- PSU ceilings ------------------------------------------------------------------
def test_buffer_rail_and_psu_output_refuse_above_psu_v_max(jafwak):
    with open_bench(jafwak, sim=True) as b:
        for method in (b.buffer_rail, b.psu_output):
            with pytest.raises(InstrumentError, match="refusing"):
                method(jafwak.psu.v_max + 0.1)
        assert b.sim_model.psu_v <= jafwak.psu.v_max


def test_vin_from_psu_obeys_psu_v_max_when_it_is_below_vin_max(jafwak):
    data = jafwak.model_dump()
    data["psu"] |= {"v_max": 1.5, "ovp": 2.0}
    data["psu_check"]["points"] = [0.5, 1.0]
    for sec in ("load_reg", "line_reg", "load_tran", "line_tran", "psrr", "en_sine", "startup", "iq"):
        data[sec] = None
    cfg = BenchConfig.model_validate(data)
    with open_bench(cfg, sim=True) as b, pytest.raises(InstrumentError, match="refusing"):
        b.vin_from_psu(1.6)  # under dut.vin_max (1.9) but over psu.v_max (1.5)


# --- PSU setup, ramping, shutdown --------------------------------------------------
def test_current_limit_and_ovp_set_before_the_output_turns_on(jafwak):
    calls = []
    with open_bench(jafwak, sim=True) as b:
        spy(b.psu, "setup", calls)
        spy(b.psu, "output", calls)
        b.vin_from_psu(1.8)
    first_on = next(i for i, (n, a, _) in enumerate(calls) if n == "output" and a[0] is True)
    setups = [i for i, (n, _, _) in enumerate(calls) if n == "setup"]
    assert setups and setups[0] < first_on
    _, _, k = calls[setups[0]]
    assert k["current_limit"] == jafwak.psu.current_limit and k["ovp"] == jafwak.psu.ovp


def test_output_turns_on_at_0v_and_ramps_in_small_steps_both_ways(jafwak):
    seen = []
    on_at = []
    with open_bench(jafwak, sim=True) as b:
        spy(b.psu, "set_voltage", seen)
        orig = b.psu.output
        b.psu.output = lambda on: (on_at.append(b.sim_model.psu_v) if on else None, orig(on))
        b.vin_from_psu(1.8)
        b.vin_from_psu(0.5)
        volts = [a[0] for _, a, _ in seen]  # before shutdown (which sets 0 V with the output already off)
    assert on_at == [0.0]
    steps = [abs(y - x) for x, y in itertools.pairwise(volts)]
    assert max(steps) <= jafwak.psu.ramp_step + 1e-9
    assert volts[-1] == pytest.approx(0.5) and 1.8 in volts  # it went up and came back down


def test_shutdown_order_signals_before_supply(jafwak):
    order = []
    with open_bench(jafwak, sim=True) as b:
        b.vin_from_psu(1.8)
        b.loadbank.set(1)
        b.vin_from_wavegen(0.5)
        spy(b.awg, "off", order, "wavegen off")
        spy(b.loadbank, "off", order, "load off")
        spy(b.dio, "release_all", order, "dio release")
        orig = b.psu.output
        b.psu.output = lambda on: (order.append(("psu off" if not on else "psu on", (), {})), orig(on))
    names = [n for n, _, _ in order]
    assert names.index("wavegen off") < names.index("load off") < names.index("dio release")
    assert names.index("dio release") < names.index("psu off")


def test_ovp_rejection_stops_the_run():
    from test_e36xx import FakeTransport

    from ldo_bench.instruments.e36xx import AgilentE36xx

    t = FakeTransport()
    p = AgilentE36xx(t)
    t.errors = ['+0,"No error"', '-113,"Undefined header"']  # CURR ok, OVP rejected
    with pytest.raises(InstrumentError, match="OVP"):
        p.setup(current_limit=0.3, ovp=6.0, voltage_range=None)
