"""temp --heater: a power resistor on PSU OUT2 ramps the board 1 C/min up to heater.t_high and back,
with the interlocks that keep it from cooking the board."""

import numpy as np
import pytest
from helpers import miswire, spy
from pydantic import ValidationError

from ldo_bench.bench import open_bench
from ldo_bench.config import BenchConfig
from ldo_bench.instruments.base import InstrumentError
from ldo_bench.procedures import temp


def hot_run(cfg, setup=None, **heater_update):
    hc = cfg.heater.model_copy(update=heater_update)
    with open_bench(cfg, sim=True) as b:
        if setup:
            setup(b)
        res = temp.run(b, cfg.temp.model_copy(update={"duration": 7200}), heater=hc)
        return res, b


def test_hot_run_follows_the_ramp_and_gives_the_curve(jafwak):
    res, b = hot_run(jafwak)
    assert not res.issues, res.issues
    hc = jafwak.heater
    T = np.array([r["temp_c"] for r in res.rows])
    target = np.array([r["target_c"] for r in res.rows])
    assert T.max() == pytest.approx(hc.t_high, abs=1.5)  # reached it without overshooting much
    ramping = (target > T[0] + 5) & (target < hc.t_high - 5)
    assert np.abs(T[ramping] - target[ramping]).max() < 2.0  # followed the 1 C/min ramp
    assert max(r["heater_w"] for r in res.rows) <= hc.max_power + 1e-9
    assert res.rows[-1]["heater_w"] == 0.0  # off at the end
    assert {"heating", "cooling"} <= {bn["direction"] for bn in res.curve.bins}
    f = res.curve.fit
    m = b.sim_model
    peak_true = 25.0 - m.bgr_tc / (2 * m.bgr_tc2)
    assert f["peak_c"] == pytest.approx(peak_true, abs=3.0)
    assert res.summary()["curve"]["fit"]["peak_c"] == f["peak_c"]
    assert any("heater" in n for n in res.notes)


def test_hot_run_plot(jafwak):
    from ldo_bench import plotting

    res, _ = hot_run(jafwak)
    plotting.close(temp.plot(res, "x"))


def test_open_heater_stops_before_heating(jafwak):
    levels = []

    def setup(b):
        miswire(b, heater_wired=False)
        real = b.psu.aux

        def aux(ch):
            out = real(ch)
            spy(out, "set_voltage", levels)
            return out

        b.psu.aux = aux

    with pytest.raises(InstrumentError, match="open"):
        hot_run(jafwak, setup=setup)
    assert max(a[0] for _, a, _ in levels) <= jafwak.heater.check_v  # never more than the check


def test_out2_still_on_vs_buff_is_caught(jafwak):
    """VS_BUFF draws ~1 mA: thousands of ohms, not the heater's 1 Ohm."""
    with pytest.raises(InstrumentError, match="resistance"):
        hot_run(jafwak, setup=lambda b: miswire(b, heater_r=3000.0))


def test_cutoff_turns_the_heater_off(jafwak):
    def setup(b):
        real, n = b.sleep, []

        def sleep(s):
            if s == jafwak.temp.interval:
                n.append(s)
                if len(n) == 30:
                    b.sim_model.t_sensor = jafwak.heater.t_cutoff + 5  # something heats the sensor
            real(s)

        b.sleep = sleep

    res, b = hot_run(jafwak, setup=setup)
    assert any("t_cutoff" in i for i in res.issues)
    assert res.rows[-1]["heater_w"] == 0.0
    assert b.sim_model.heater_v == 0.0


def test_heater_not_on_the_board_stops_the_run(jafwak):
    res, b = hot_run(jafwak, setup=lambda b: miswire(b, heater_coupled=False))
    assert any("isn't rising" in i for i in res.issues)
    assert b.sim_model.heater_v == 0.0
    assert res.rows[-1]["t_s"] < 15 * 60  # stopped soon, not after the whole profile


def test_sensor_lost_while_heating_turns_the_heater_off(jafwak):
    def setup(b):
        real, n = b.sleep, []

        def sleep(s):
            if s == jafwak.temp.interval:
                n.append(s)
                if len(n) == 20:
                    b.sim_model.vs_buff_wired = False  # VS_BUFF supply off: the TMP235 reads 0 V
            real(s)

        b.sleep = sleep

    res, b = hot_run(jafwak, setup=setup)
    assert any("J13 pin 5" in i for i in res.issues)
    assert b.sim_model.heater_v == 0.0


def test_ctrl_c_turns_the_heater_off_and_keeps_the_data(jafwak):

    def setup(b):
        real, n = b.sleep, []

        def sleep(s):
            if s == jafwak.temp.interval:
                n.append(s)
                if len(n) == 40:
                    raise KeyboardInterrupt
            real(s)

        b.sleep = sleep

    res, b = hot_run(jafwak, setup=setup)
    assert len(res.rows) == 40 and res.stopped_early
    assert b.sim_model.heater_v == 0.0


def test_shutdown_sets_the_heater_to_0_before_the_outputs_go_off(jafwak):
    calls = []
    with open_bench(jafwak, sim=True) as b:
        b.confirm_wiring("x", [], ref_probe=("tmp", 0), aux_to="heater", vs_buff_by_hand=True)
        b.vin_from_psu(1.8)
        b.heater_on()
        b.heater_set(5.0)
        spy(b.heater_out, "set_voltage", calls, "heater")
        spy(b.psu, "output", calls, "psu_output")
    names = [n for n, _, _ in calls]
    assert names.index("heater") < names.index("psu_output")
    assert calls[names.index("heater")][1][0] == 0.0


def test_heater_needs_vin_up_first(jafwak):
    with open_bench(jafwak, sim=True) as b, pytest.raises(InstrumentError, match="VIN up"):
        b.heater_on()


def test_heater_set_is_clamped(jafwak):
    hc = jafwak.heater
    with open_bench(jafwak, sim=True) as b:
        b.confirm_wiring("x", [], ref_probe=("tmp", 0), aux_to="heater", vs_buff_by_hand=True)
        b.vin_from_psu(1.8)
        r = b.heater_on()
        assert r == pytest.approx(1.0, rel=0.01)
        assert b.heater_set(100.0) <= hc.max_power + 1e-9
        assert b.heater_out.get_voltage_setpoint() <= hc.v_max + 1e-9
        assert b.heater_set(-1.0) == 0.0


def test_heater_refused_while_out2_powers_vs_buff(jafwak):
    with open_bench(jafwak, sim=True) as b:
        b.confirm_wiring("x", [], ref_probe=("tmp", 0), aux_to="vs_buff")
        b.vin_from_psu(1.8)
        b.vs_buff_on(5.0)
        with pytest.raises(InstrumentError, match="VS_BUFF"):
            b.heater_on()


def test_heater_run_needs_vs_buff_external(jafwak):
    cfg = jafwak.model_copy(update={"temp": jafwak.temp.model_copy(update={"vs_buff_source": "psu"})})
    with open_bench(cfg, sim=True) as b, pytest.raises(InstrumentError, match="vs_buff_source"):
        temp.run(b, cfg.temp, heater=cfg.heater)


def test_profile_longer_than_the_duration_is_refused_before_heating(jafwak):
    with open_bench(jafwak, sim=True) as b:
        with pytest.raises(InstrumentError, match="--minutes"):
            temp.run(b, jafwak.temp.model_copy(update={"duration": 1800}), heater=jafwak.heater)
        assert b.sim_model.heater_v == 0.0


def test_hot_and_cold_wiring(jafwak):
    with open_bench(jafwak, sim=True) as b:
        hot = "\n".join(temp.wiring(b, jafwak.temp, jafwak.heater))
        cold = "\n".join(temp.wiring(b, jafwak.temp))
    for s in (
        "PSU OUT2 + -> heater",
        "neither lead touching the board",
        "No ice",
        "Opaque box",
        "Separate supply + (5.0 V) -> VS_BUFF (J4)",
        "0.2 V",
        "60 C",
    ):
        assert s in hot, s
    for s in ("Ice:", "sealed bag", "Opaque box", "Separate supply + (5.0 V) -> VS_BUFF (J4)", "Ctrl-C"):
        assert s in cold, s
    assert "PSU OUT2 + -> VS_BUFF" not in hot and "PSU OUT2 + -> VS_BUFF" not in cold


@pytest.mark.parametrize(
    ("update", "match"),
    [
        ({"ovp": 2.9}, "ovp"),
        ({"t_high": 70.0}, "t_cutoff"),
        ({"channel": 1}, "psu.channel"),
        ({"check_v": 3.5}, "check_v"),
        ({"rate": 1.5}, "max_rate"),
    ],
)
def test_heater_config_checks(jafwak, update, match):
    data = jafwak.model_dump()
    data["heater"] |= update
    with pytest.raises(ValidationError, match=match):
        BenchConfig.model_validate(data)


def test_second_output_range_is_set_before_its_current_limit():
    """The E3648A's HIGH range stops at 2.5 A: LOW must be selected before CURR 3.2."""
    from test_vref import _FakeTransport

    from ldo_bench.instruments.e36xx import AgilentE36xx

    t = _FakeTransport()
    AgilentE36xx(t, channel=1).aux(2).setup(current_limit=3.2, ovp=3.5, voltage_range="LOW")
    cmds = t.sent[t.sent.index("INST:SEL OUT2") :]
    assert cmds.index("VOLT:RANG LOW") < cmds.index("CURR 3.2000")


def test_temp_heater_cli_on_sim():
    from helpers import config_path, invoke

    r = invoke("temp", "-c", config_path(), "--sim", "--heater")
    assert r.exit_code == 0, r.output
    assert "peak" in r.output and "heater:" in r.output


def test_temp_heater_cli_needs_the_minutes():
    from helpers import config_path, invoke

    r = invoke("temp", "-c", config_path(), "--sim", "--heater", "--minutes", "30")
    assert r.exit_code == 1 and "--minutes" in r.output
