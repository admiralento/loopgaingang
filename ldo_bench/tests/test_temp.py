"""temp: log the TMP235 (AD2 on J13 pin 5), VREF (DMM on TP3) and VOUT over time; tempco from stable plateaus."""

import numpy as np
import pytest
from helpers import miswire, spy
from pydantic import ValidationError

from ldo_bench import plotting
from ldo_bench.bench import open_bench
from ldo_bench.config import BenchConfig
from ldo_bench.instruments.base import InstrumentError
from ldo_bench.procedures import temp


def step_profile(t):
    """Room temperature for 300 s, then ice on U1: 5 C."""
    return 25.0 if t < 300 else 5.0


def test_plateaus_finds_the_stable_stretches():
    t = np.arange(0, 600, 5.0)
    T = np.where(t < 200, 25.0, np.where(t < 300, 25 - (t - 200) * 0.2, 5.0))  # 100 s ramp between
    pl = temp.plateaus(t, T, window=30, delta=0.5)
    assert len(pl) == 2
    (a0, _), (b0, _) = pl
    assert T[a0] == pytest.approx(25) and T[b0] == pytest.approx(5)
    assert t[a0] >= 30 - 1e-9  # needs a full window behind it to count as stable
    assert t[b0] >= 300 + 30 - 5  # not stable until 30 s after the ramp ends


def test_temp_on_sim_gives_the_tempco(jafwak):
    tc = jafwak.temp.model_copy(update={"duration": 900, "interval": 5})
    with open_bench(jafwak, sim=True) as b:
        b.sim_model.temp_profile = step_profile
        res = temp.run(b, tc)
        m = b.sim_model
        # the line through 25 C and 5 C on VREF = V25 (1 + tc d + tc2 d^2), d = T - 25
        tc_sim = m.bgr_tc - 20 * m.bgr_tc2
    assert not res.issues, res.issues
    s = res.summary()
    assert s["plateaus"] == 2
    assert [p["temp_c"] for p in s["plateau_points"]] == pytest.approx([25, 5], abs=0.1)
    assert s["vref_tc_ppm_per_c"] == pytest.approx(tc_sim * 1e6, rel=0.1)
    assert s["vout_tc_ppm_per_c"] == pytest.approx(tc_sim * 1e6, rel=0.2)
    assert len(res.rows) == pytest.approx(900 / 5, abs=2)
    assert {"t_s", "temp_c", "tmp_v", "vref_v", "vout_v", "stable"} <= res.rows[0].keys()
    plotting.close(temp.plot(res, "x"))


def test_one_temperature_only_gives_no_tempco(jafwak):
    tc = jafwak.temp.model_copy(update={"duration": 120, "interval": 5})
    with open_bench(jafwak, sim=True) as b:
        res = temp.run(b, tc)
    assert not res.issues
    assert res.summary()["vref_tc_ppm_per_c"] is None
    assert any("two stable temperatures" in n for n in res.notes)


def test_tmp235_unpowered_is_flagged(jafwak):
    tc = jafwak.temp.model_copy(update={"duration": 30, "interval": 5})
    with open_bench(jafwak, sim=True) as b:
        miswire(b, vs_buff_wired=False)  # PSU OUT2 not on VS_BUFF (J4)
        res = temp.run(b, tc)
    assert any("J13 pin 5" in i and "VS_BUFF" in i for i in res.issues)
    plotting.close(temp.plot(res, "x"))


def test_ctrl_c_ends_the_log_and_keeps_the_data(jafwak):
    tc = jafwak.temp.model_copy(update={"duration": 900, "interval": 5})
    with open_bench(jafwak, sim=True, enable_dio=True) as b:
        real, calls = b.sleep, []

        def sleep(s):  # Ctrl-C during the 10th wait between samples (the ramps before it sleep too)
            if s == tc.interval:
                calls.append(s)
                if len(calls) == 10:
                    raise KeyboardInterrupt
            real(s)

        b.sleep = sleep
        res = temp.run(b, tc)
        en_low = not b.sim_model.dio.get(jafwak.enable.dio_pin, False)
    assert len(res.rows) == 10  # the samples before Ctrl-C
    assert any("Ctrl-C" in n for n in res.notes)
    assert en_low


def test_wiring_names_the_board_points(jafwak):
    with open_bench(jafwak, sim=True) as b:
        text = "\n".join(temp.wiring(b, jafwak.temp))
    for s in (
        "J13 pin 5 (TMP)",
        "Separate supply + (5.0 V) -> VS_BUFF (J4)",
        "VREF (TP3)",
        "J13 pin 4 (OUTA)",
        "VIN_DC (J7)",
    ):
        assert s in text
    # the 1 mA comes from load code 1 (B0, 1.2 kOhm), not a resistor fitted by hand
    assert jafwak.temp.load_code == 1 and "AD2 DIO 0 -> J13 pin 7 (B0)" in text
    assert "Optional" not in text
    assert "PSU OUT2 + -> VS_BUFF" not in text  # temp.vs_buff_source = "external" on JAFWAK (2026-11-21)


def test_wiring_with_vs_buff_on_out2(jafwak_out2):
    with open_bench(jafwak_out2, sim=True) as b:
        text = "\n".join(temp.wiring(b, jafwak_out2.temp))
    assert "PSU OUT2 + -> VS_BUFF (J4)" in text and "Separate supply" not in text


def test_duration_is_capped(jafwak):
    data = jafwak.model_dump()
    data["temp"]["duration"] = 3 * 3600
    with pytest.raises(ValidationError, match="duration"):
        BenchConfig.model_validate(data)


# --- VS_BUFF from PSU OUT2 (powers the TMP235) ----------------------------------------
@pytest.fixture
def jafwak_out2(jafwak):
    """JAFWAK with temp.vs_buff_source = "psu": VS_BUFF from PSU OUT2 instead of a separate supply."""
    return jafwak.model_copy(update={"temp": jafwak.temp.model_copy(update={"vs_buff_source": "psu"})})


def watch_out2(b) -> list:
    """Spy on set_voltage of every PSU second output the run opens; returns the call log."""
    calls: list = []
    real = b.psu.aux

    def aux(ch):
        out = real(ch)
        spy(out, "set_voltage", calls, f"OUT{ch}")
        return out

    b.psu.aux = aux
    return calls


def test_temp_powers_vs_buff_from_out2(jafwak_out2):
    tc = jafwak_out2.temp
    with open_bench(jafwak_out2, sim=True) as b:
        calls = watch_out2(b)
        res = temp.run(b, tc.model_copy(update={"duration": 60, "interval": 5}))
    assert not res.issues, res.issues
    levels = [a[0] for n, a, _ in calls if n == f"OUT{tc.vs_buff_channel}"]
    assert max(levels) == pytest.approx(tc.vs_buff_v) and levels[-1] == 0.0  # up, then back to 0 V
    assert all(np.diff(levels[: levels.index(max(levels)) + 1]) <= jafwak_out2.psu.ramp_step + 1e-9)  # ramped
    assert "i_vs_buff_a" in res.rows[0]


def test_vs_buff_overcurrent_stops_the_run(jafwak_out2):
    with open_bench(jafwak_out2, sim=True) as b:
        miswire(b, vs_buff_short=True)
        calls = watch_out2(b)
        with pytest.raises(InstrumentError, match="VS_BUFF"):
            temp.run(b, jafwak_out2.temp.model_copy(update={"duration": 30, "interval": 5}))
        assert calls[-1][1][0] == 0.0  # OUT2 back at 0 V before the error


def test_vs_buff_needs_vin_up_first(jafwak):
    with open_bench(jafwak, sim=True) as b, pytest.raises(InstrumentError, match="VIN up"):
        b.vs_buff_on(5.0)


def test_vs_buff_refused_above_psu_v_max(jafwak):
    with open_bench(jafwak, sim=True) as b:
        b.vin_from_psu(1.8)
        with pytest.raises(InstrumentError, match="refusing VS_BUFF"):
            b.vs_buff_on(jafwak.psu.v_max + 0.1)


def test_vs_buff_refused_with_vref_ext(jafwak):
    with open_bench(jafwak, sim=True, vref_external=True) as b:
        b.vin_from_psu(1.8)
        with pytest.raises(InstrumentError, match="OUT2"):
            b.vs_buff_on(5.0)


def test_vs_buff_refused_with_split_rails_up(jafwak):
    with open_bench(jafwak, sim=True) as b:
        b.confirm_wiring("x", [], ref_probe=("vin", 0), aux_to="rails")
        b.vin_from_psu(1.5)
        b.split_rails_on()
        with pytest.raises(InstrumentError, match="split rails"):
            b.vs_buff_on(5.0)


def test_shutdown_takes_vs_buff_down_before_the_psu_output(jafwak):
    calls: list = []
    with open_bench(jafwak, sim=True) as b:
        b.confirm_wiring("x", [], ref_probe=("tmp", 0), aux_to="vs_buff")
        b.vin_from_psu(1.8)
        b.vs_buff_on(5.0)
        spy(b.vs_buff_out, "set_voltage", calls, "vs_buff")
        spy(b.psu, "output", calls, "psu_output")
    names = [n for n, _, _ in calls]
    assert names.index("vs_buff") < names.index("psu_output")
    assert calls[names.index("vs_buff")][1][0] == 0.0


@pytest.mark.parametrize(
    ("update", "match"),
    [
        ({"vs_buff_v": 6.0, "vs_buff_ovp": 6.5}, "psu.v_max"),
        ({"vs_buff_channel": 1}, "psu.channel"),
        ({"vs_buff_ovp": 5.0}, "vs_buff_ovp"),
    ],
)
def test_vs_buff_config_checks(jafwak, update, match):
    data = jafwak.model_dump()
    data["temp"] |= update
    with pytest.raises(ValidationError, match=match):
        BenchConfig.model_validate(data)


def test_a_steady_ramp_is_not_a_plateau(jafwak):
    """A 1 C/min ramp sits right at stable_delta / stable_window (0.5 C in 30 s): noise must not make
    it a string of short plateaus (seen in the simulated hot run, 2026-11-21)."""
    with open_bench(jafwak, sim=True) as b:
        b.sim_model.temp_profile = lambda t: 20 + t / 60 + 0.02 * np.sin(t)
        res = temp.run(b, jafwak.temp.model_copy(update={"duration": 1200, "interval": 2}))
    assert res.points == []


def test_curve_keeps_a_1c_per_min_ramp(jafwak):
    """The heater ramps at 1 C/min: max_rate must sit above that, or noise drops half the ramp."""
    assert jafwak.temp.max_rate > jafwak.heater.rate
