import numpy as np
import pytest

from ldo_bench.analysis import SettlingResult
from ldo_bench.bench import open_bench
from ldo_bench.procedures import line_tran


def run(cfg, setup=None, **upd):
    lt = cfg.line_tran.model_copy(update={"averages": 4, **upd})
    with open_bench(cfg, sim=True) as b:
        if setup:
            setup(b)
        res = line_tran.run(b, lt, cfg.analysis)
    return res, b


def test_both_edges_on_sim(cfg):
    res, b = run(cfg)
    m, lt = b.sim_model, cfg.line_tran
    up, down = res.edges
    assert up.metrics.deviation > 0 > down.metrics.deviation  # VOUT kicks with VIN
    # sim: VOUT kick amplitude k_line * dVIN, times ~0.65 (peak of exp*sin, 1 us average)
    assert up.rejection == pytest.approx(0.65 * m.k_line, rel=0.2)
    for e in res.edges:
        assert 3e-6 < e.metrics.settling_time < 20e-6
    # buffer offset (-15 mV in the sim) calibrated out: VIN lands on the configured levels
    assert up.v_from == pytest.approx(lt.vin_low, abs=0.005)
    assert up.v_to == pytest.approx(lt.vin_high, abs=0.005)
    assert res.w1_low == pytest.approx(lt.vin_low - m.buffer_offset, abs=0.005)
    # everything off afterwards
    assert not m.psu_on and m.awg_v == {} and b.loadbank.code == 0


def test_buffer_without_headroom_is_reported(jafwak_single):
    seen = []

    def weak_buffer(b):
        # healthy through the interlock's proof (rail at vin_max), weak at the full rail:
        # the full rail (line_tran.psu_voltage) can then only reach 1.5 V
        rail = b.buffer_rail

        def weak_at_full_rail(v):
            if v > b.cfg.dut.vin_max:
                b.sim_model.buffer_dropout = v - 1.5
            rail(v)

        b.buffer_rail = weak_at_full_rail
        orig = b.awg.set_dc
        b.awg.set_dc = lambda ch, v: (seen.append(v), orig(ch, v))

    res, _ = run(jafwak_single, setup=weak_buffer)  # runs to the end, records the problem
    assert any("headroom" in i for i in res.issues)
    # the calibration never chased the dead buffer upward
    lt = jafwak_single.line_tran
    assert max(seen) <= min(lt.vin_high + line_tran.CAL_MAX_CORRECTION, jafwak_single.dut.vin_max) + 1e-9


def test_load_limit_applies(jafwak):
    from ldo_bench.config import BenchConfig

    d = jafwak.model_dump()
    d["line_tran"]["load_code"] = 23  # 21.04 mA > 21 mA
    with pytest.raises(ValueError, match="i_max"):
        BenchConfig.model_validate(d)


def test_line_tran_rejection_by_hand():
    m = SettlingResult(1.2, 1.205, 5e-4, 1.2, 1e-5, 1e-5, 1.2, 10e6, 100, "pre")  # +5 mV kick
    e = line_tran.EdgeResult("rising", 1.75, 1.85, m, np.zeros(1), np.zeros(1), np.zeros(1), np.zeros(1))
    assert e.rejection == pytest.approx(0.005 / 0.1)  # 50 mV per V of VIN step


def test_positive_buffer_offset_never_overshoots_vin(jafwak):
    """Bench, 2026-11-17: U2 read +65 mV above W1 at no load. Calibration that starts with W1 at the
    target put VIN at 1.915 V (over dut.vin_max) before the first reading. It must come up from
    below: no VIN reading during calibration may exceed the target it is heading for (+5 mV)."""
    readings: list[float] = []

    def offset(b):
        b.sim_model.buffer_offset = 0.065
        real = b.measure_vin_fine

        def spy():
            v = real()
            readings.append(v)
            return v

        b.measure_vin_fine = spy

    res, _ = run(jafwak, setup=offset, load_code=0)
    assert not [i for i in res.issues if "VIN reached" in i], res.issues
    assert max(readings) <= jafwak.line_tran.vin_high + 0.005, max(readings)


def _sim_step_capture(b, cfg, hysteresis):
    """One simulated capture of a 1.75 -> 1.85 V VIN step (PSU straight on VIN), trigger at 1.80 V."""
    from ldo_bench.instruments.base import ChannelSetup

    m, ch = b.sim_model, cfg.ad2.ref_channel
    m.psu_to, m.psu_on, m.psu_v = "vin", True, 1.75  # sim VIN = the PSU setpoint

    def step():
        m.psu_v = 1.85

    return b.scope.capture(
        sample_rate=1e6,
        n_samples=1000,
        setups={cfg.ad2.vout_channel: ChannelSetup(5.0, 0.0), ch: ChannelSetup(50.0, 0.0)},
        trigger_channel=ch,
        trigger_level=1.8,
        edge="rising",
        pre_trigger=0.2,
        stimulus=step,
        timeout=1.0,
        hysteresis=hysteresis,
    )


def test_sim_trigger_needs_hysteresis_before_the_edge(jafwak):
    """Like the AD2, the simulated scope ignores a crossing that doesn't start `hysteresis` beyond the
    level. Its default (1 % of range) is 0.5 V on the 50 V range: more than a 100 mV step."""
    from ldo_bench.instruments.base import InstrumentError

    with open_bench(jafwak, sim=True) as b:
        with pytest.raises(InstrumentError, match="trigger timeout"):
            _sim_step_capture(b, jafwak, None)
        assert _sim_step_capture(b, jafwak, 0.025).edge_found


def test_small_vin_step_triggers_with_ref_on_50v_range(jafwak):
    """Bench, 2026-11-17 and 2026-11-19: with [ad2] ref on the 50 V range the AD2's default trigger
    hysteresis is 0.5 V, so line-tran's 100 mV VIN step never triggered. line-tran must record VIN on
    the fine range and set its own hysteresis."""
    assert jafwak.ad2.ref.range == 50.0  # the config that failed on the bench
    res, _ = run(jafwak)
    assert not [i for i in res.issues if "never triggered" in i], res.issues
    for e in res.edges:
        assert e.metrics.step_index >= 0


def test_trigger_hysteresis_leaves_room_for_calibration_error(jafwak):
    """Each calibrated level may land up to CAL_TOLERANCE nearer the trigger. The fine range alone gives
    a 50 mV default hysteresis, exactly half the 100 mV step: on the bench a level 1 mV short would
    never trigger. line-tran must pass a hysteresis that clears that margin, with VIN on the fine range."""
    seen: list[dict] = []

    def spy(b):
        real = b.scope.capture

        def capture(**kw):
            seen.append(kw)
            return real(**kw)

        b.scope.capture = capture

    run(jafwak, setup=spy)
    lt, ch = jafwak.line_tran, jafwak.ad2.ref_channel
    half_step = abs(lt.vin_high - lt.vin_low) / 2
    edges = [kw for kw in seen if not kw.get("force")]  # not the square's untriggered VIN check
    assert edges
    for kw in edges:
        assert kw["hysteresis"] is not None
        assert 0 < kw["hysteresis"] <= half_step - line_tran.CAL_TOLERANCE
        assert kw["setups"][ch].range < jafwak.ad2.ref.range  # the fine range, not [ad2] ref's 50 V


def _spy_waves(b, seen):
    real = b.awg.set_wave

    def spy(ch, **kw):
        seen.append(kw)
        real(ch, **kw)

    b.awg.set_wave = spy


def test_square_is_the_default_and_ends_on_dc(jafwak):
    """Bench, 2026-11-18 (Joseph) and 2026-11-19: the AD2's DC level moves with tau ~2 ms, its square
    in ~0.2-0.35 ms. line-tran runs W1 as a square between the calibrated setpoints by default, and
    leaves it on DC before the rails come down."""
    lt = jafwak.line_tran
    assert lt.square_hz == 20
    seen: list[dict] = []
    res, b = run(jafwak, setup=lambda b: _spy_waves(b, seen))
    assert not res.issues, res.issues
    assert len(seen) == 1 and seen[0]["shape"] == "square" and seen[0]["frequency"] == 20
    assert seen[0]["low"] == pytest.approx(res.w1_low) and seen[0]["high"] == pytest.approx(res.w1_high)
    assert b.sim_model.awg_wave == {}  # back on DC, then off, by the end
    assert res.summary()["square_hz"] == 20


def test_dc_steps_without_square_hz(jafwak):
    seen: list[dict] = []
    res, _ = run(jafwak, setup=lambda b: _spy_waves(b, seen), square_hz=None)
    assert not res.issues, res.issues
    assert seen == []
    assert res.summary()["square_hz"] is None


def test_square_refuses_levels_above_vin_max(jafwak):
    from ldo_bench.instruments.base import InstrumentError

    with open_bench(jafwak, sim=True) as b:
        with pytest.raises(InstrumentError, match="refusing"):
            b.vin_square_from_wavegen(low=1.75, high=jafwak.dut.vin_max + 0.05, frequency=20)
        with pytest.raises(InstrumentError, match="refusing"):
            b.vin_square_from_wavegen(low=1.85, high=1.75, frequency=20)
        with pytest.raises(InstrumentError, match="refusing"):
            b.vin_square_from_wavegen(low=1.75, high=1.85, frequency=0)
        assert b.sim_model.awg_wave == {}  # nothing reached the wavegen


def test_square_over_vin_max_stops_the_run_and_shutdown_leaves_dc(jafwak):
    """The DC calibration can't see the square's own levels: if VIN on the square reads over
    dut.vin_max (less the AD2 margin) the run stops before any edge is captured, and the shutdown
    leaves W1 on DC, not the square."""
    from ldo_bench.instruments.base import InstrumentError

    lt = jafwak.line_tran.model_copy(update={"averages": 2})
    with pytest.raises(InstrumentError, match="square"), open_bench(jafwak, sim=True) as b:
        m, real = b.sim_model, b.awg.set_wave

        def high_square(ch, **kw):
            m.buffer_offset += 0.1  # U2 reads 100 mV higher on the square than at DC
            real(ch, **kw)

        b.awg.set_wave = high_square
        captures = []
        real_capture = b.scope.capture
        b.scope.capture = lambda **kw: captures.append(kw) or real_capture(**kw)
        line_tran.run(b, lt, jafwak.analysis)
    assert len(captures) == 1 and captures[0]["force"]  # only the check, no edge captures
    assert m.awg_wave == {} and not m.psu_on


def test_square_half_period_must_cover_the_capture(jafwak):
    """Each capture must fit inside one half-period, or it would see the next edge as well."""
    d = jafwak.line_tran.model_dump()
    with pytest.raises(ValueError, match="square_hz"):
        type(jafwak.line_tran).model_validate(d | {"square_hz": 20, "sample_rate": 8192 / 30e-3})
    type(jafwak.line_tran).model_validate(d | {"square_hz": 20, "sample_rate": 8192 / 20e-3})


def _vout_labels(fig):
    return {ln.get_label() for ax in fig.axes for ln in ax.get_lines()}


def test_raw_plots_only_measured_samples(jafwak):
    """--raw: one capture per edge, and the plots show only what the scope read (no smoothed trace)."""
    res, _ = run(jafwak, averages=1)
    assert "Smoothed" in _vout_labels(line_tran.plot(res, "JAFWAK"))
    res.raw = True
    for fig in [line_tran.plot(res, "JAFWAK"), *line_tran.extra_plots(res, "JAFWAK").values()]:
        labels = _vout_labels(fig)
        assert "Smoothed" not in labels and "Averaged" not in labels
        assert "Measured (raw)" in labels
    assert res.summary()["raw"] is True
