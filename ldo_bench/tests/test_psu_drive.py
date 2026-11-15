"""psu-hold (hold a voltage until Ctrl-C or the hard timeout) and psu-sweep (step the voltage)."""

import csv

import pytest
from helpers import config_path, invoke, spy
from pydantic import ValidationError

from ldo_bench.bench import open_bench
from ldo_bench.config import BenchConfig, PsuHoldConfig, PsuSweepConfig
from ldo_bench.procedures import psu_hold, psu_sweep


def _cfg(jafwak, name: str, **fields) -> BenchConfig:
    data = jafwak.model_dump()
    data[name] = (data[name] or {}) | fields
    return BenchConfig.model_validate(data)


def _interrupt_after(bench, n: int, seconds: float) -> None:
    """Make the n-th bench.sleep(seconds) raise KeyboardInterrupt, as Ctrl-C would.

    Only sleeps of that length count (the hold's interval, the sweep's dwell), so the ramp's own
    pauses don't: Ctrl-C during the ramp is an abort (exit 130), not the end of a hold.
    """
    real, count = bench.sleep, [0]

    def sleep(s):
        if s == seconds:
            count[0] += 1
            if count[0] >= n:
                raise KeyboardInterrupt
        real(s)

    bench.sleep = sleep


# --- config --------------------------------------------------------------------------
def test_sweep_setpoints():
    sc = PsuSweepConfig(start=0.0, stop=1.0, step=0.25)
    assert sc.setpoints() == pytest.approx([0.0, 0.25, 0.5, 0.75, 1.0])
    up_down = PsuSweepConfig(start=0.0, stop=1.0, step=0.25, updown=True).setpoints()
    assert up_down == pytest.approx([0.0, 0.25, 0.5, 0.75, 1.0, 0.75, 0.5, 0.25, 0.0])
    # a step that doesn't divide the span still ends on stop; a downward sweep works too
    assert PsuSweepConfig(start=0.0, stop=1.0, step=0.3).setpoints() == pytest.approx([0, 0.3, 0.6, 0.9, 1.0])
    assert PsuSweepConfig(start=1.0, stop=0.5, step=0.25).setpoints() == pytest.approx([1.0, 0.75, 0.5])


def test_hold_timeout_is_capped_at_60_minutes():
    with pytest.raises(ValidationError):
        PsuHoldConfig(voltage=1.0, max_minutes=61)


@pytest.mark.parametrize("name", ["psu_hold", "psu_sweep"])
def test_current_limit_cannot_exceed_psu_current_limit(jafwak, name):
    with pytest.raises(ValidationError, match="current_limit"):
        _cfg(jafwak, name, current_limit=0.011)


def test_vin_ceiling(jafwak):
    with pytest.raises(ValidationError, match="vin_max"):
        _cfg(jafwak, "psu_hold", voltage=2.0)  # VIN: dut.vin_max = 1.9 V
    with pytest.raises(ValidationError, match="vin_max"):
        _cfg(jafwak, "psu_sweep", stop=2.0)
    # with nothing but a load or meter on the supply only psu.v_max (5.5 V) applies
    assert _cfg(jafwak, "psu_hold", voltage=5.0, to="bench").psu_hold.voltage == 5.0
    with pytest.raises(ValidationError, match="v_max"):
        _cfg(jafwak, "psu_hold", voltage=5.6, to="bench")


# --- psu-hold ----------------------------------------------------------------------------
def test_hold_runs_to_the_hard_timeout(jafwak):
    hc = jafwak.psu_hold.model_copy(update={"max_minutes": 2, "interval": 10})
    with open_bench(jafwak, sim=True) as b:
        ramps = spy(b, "vin_from_psu")
        res = psu_hold.run(b, hc)
        on_during = b.sim_model.psu_on
    assert not b.sim_model.psu_on and on_during  # held on, then shut down
    assert ramps and ramps[0][1] == (hc.voltage,)
    assert res.stopped_by == "timeout"
    assert 12 <= len(res.rows) <= 13  # 120 s at 10 s
    assert res.rows[-1]["t_s"] <= 120
    assert res.limit_a == pytest.approx(jafwak.psu.current_limit)
    assert not res.issues


def test_hold_ctrl_c_ends_it_and_shuts_down(jafwak):
    with open_bench(jafwak, sim=True) as b:
        _interrupt_after(b, 5, jafwak.psu_hold.interval)
        res = psu_hold.run(b, jafwak.psu_hold)
    assert res.stopped_by == "ctrl-c" and res.rows
    assert not b.sim_model.psu_on


def test_hold_uses_the_given_lower_limit(jafwak):
    hc = jafwak.psu_hold.model_copy(update={"current_limit": 0.005, "max_minutes": 0.1})
    with open_bench(jafwak, sim=True) as b:
        calls = spy(b.psu, "setup")
        psu_hold.run(b, hc)
    assert calls[0][2]["current_limit"] == pytest.approx(0.005)


def test_hold_flags_near_the_limit_but_not_as_a_problem(jafwak):
    hc = jafwak.psu_hold.model_copy(update={"max_minutes": 0.1})
    with open_bench(jafwak, sim=True) as b:
        b.psu.measure_current = lambda: 0.0099  # at the 10 mA limit
        b.psu.measure_voltage = lambda: 1.2  # so the output sags: constant current
        res = psu_hold.run(b, hc)
    assert all(r["near_limit"] for r in res.rows)
    assert not res.issues
    assert "near the current limit" in res.report()


def test_hold_readback_off_setpoint_is_a_setup_problem(jafwak):
    hc = jafwak.psu_hold.model_copy(update={"max_minutes": 0.1})
    with open_bench(jafwak, sim=True) as b:
        b.psu.measure_voltage = lambda: 0.0  # output not following, current not at the limit
        res = psu_hold.run(b, hc)
    assert res.issues and "readback" in res.issues[0]


def test_hold_cli_prints_and_saves_nothing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    r = invoke(
        "psu-hold", "-c", config_path(), "--sim", "--yes", "--v", "1.5", "--ilim", "5mA", "--minutes", "0.1"
    )
    assert r.exit_code == 0, r.output
    assert "1.5" in r.output and "5 mA" in r.output and "Ctrl-C" in r.output
    assert not any(tmp_path.rglob("*"))


def test_hold_cli_refuses_over_limits():
    r = invoke("psu-hold", "-c", config_path(), "--sim", "--yes", "--v", "1.5", "--ilim", "20mA")
    assert r.exit_code == 2 and "current_limit" in r.output
    r = invoke("psu-hold", "-c", config_path(), "--sim", "--yes", "--v", "2.5")
    assert r.exit_code == 2 and "vin_max" in r.output


# --- psu-sweep --------------------------------------------------------------------------
def test_sweep_steps_through_the_bench(jafwak):
    sc = jafwak.psu_sweep.model_copy(update={"start": 0.0, "stop": 1.8, "step": 0.2, "updown": True})
    with open_bench(jafwak, sim=True) as b:
        ramps = spy(b, "vin_from_psu")
        res = psu_sweep.run(b, sc)
    assert [a[0] for _, a, _ in ramps] == pytest.approx(sc.setpoints())
    assert len(res.rows) == len(sc.setpoints())
    assert {r["direction"] for r in res.rows} == {"up", "down"}
    assert res.complete and not res.issues
    assert all(abs(r["v_psu"] - r["set_v"]) < 0.05 for r in res.rows)
    assert not b.sim_model.psu_on


def test_sweep_to_bench_uses_psu_output(jafwak):
    sc = jafwak.psu_sweep.model_copy(update={"to": "bench", "stop": 3.0})
    with open_bench(jafwak, sim=True) as b:
        outs = spy(b, "psu_output")
        psu_sweep.run(b, sc)
    assert max(a[0] for _, a, _ in outs) == pytest.approx(3.0)


def test_sweep_ctrl_c_keeps_the_points(jafwak):
    with open_bench(jafwak, sim=True) as b:
        n = len(jafwak.psu_sweep.setpoints())
        _interrupt_after(b, 10, jafwak.psu_sweep.dwell)  # part way through
        res = psu_sweep.run(b, jafwak.psu_sweep)
    assert 0 < len(res.rows) < n and not res.complete
    assert "Ctrl-C" in res.report()


def test_sweep_readback_problem(jafwak):
    with open_bench(jafwak, sim=True) as b:
        b.psu.measure_voltage = lambda: 0.0
        res = psu_sweep.run(b, jafwak.psu_sweep)
    assert res.issues  # points at 0 V setpoint read 0 V correctly; the rest don't


def test_sweep_cli_saves(tmp_path):
    r = invoke(
        "psu-sweep",
        "-c",
        config_path(),
        "--sim",
        "--yes",
        "--save-sim",
        "-o",
        str(tmp_path),
        "--start",
        "0",
        "--stop",
        "1",
        "--step",
        "0.5",
    )
    assert r.exit_code == 0, r.output
    with next(tmp_path.rglob("data.csv")).open() as f:
        rows = list(csv.DictReader(f))
    assert [float(x["set_v"]) for x in rows] == pytest.approx([0.0, 0.5, 1.0])
    assert {"v_psu", "i_psu", "near_limit"} <= set(rows[0])
    assert any(tmp_path.rglob("plot.png"))
