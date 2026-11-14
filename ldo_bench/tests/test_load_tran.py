import numpy as np
import pytest

from ldo_bench.analysis import SettlingResult
from ldo_bench.bench import open_bench
from ldo_bench.instruments.base import Capture, InstrumentError
from ldo_bench.procedures import line_tran, load_tran
from ldo_bench.procedures.transient import SWING_TOLERANCE, align_average, capture_issues


def test_align_average_lines_up_triggers():
    base = np.zeros(100)
    a, b = base.copy(), base.copy()
    a[40:] = 1.0
    b[45:] = 1.0  # same step, captured 5 samples later
    avg = align_average([Capture(1e6, a, a, 40), Capture(1e6, b, b, 45)])
    assert avg.trigger_index == 40 and len(avg.ch1) == 40 + 55
    assert avg.ch1[39] == 0.0 and avg.ch1[40] == 1.0  # edges coincide, not smeared


def run(cfg, **upd):
    an = cfg.analysis.model_copy(update={"settle_reference": "post"})
    lt = cfg.load_tran.model_copy(update={"averages": 4, **upd})
    with open_bench(cfg, sim=True) as b:
        res = load_tran.run(b, lt, an)
    return res, b


def test_both_edges_on_sim(cfg):
    res, b = run(cfg)
    m = b.sim_model
    up, down = res.edges
    assert (up.edge, down.edge) == ("rising", "falling")
    assert up.metrics.deviation < 0 < down.metrics.deviation  # dip on load up, overshoot on load down
    # sim: amplitude z_peak * dI, scaled by exp/sin at the peak and the 1 us average (~0.6-0.7)
    di = up.i_to - up.i_from
    assert abs(up.metrics.deviation) == pytest.approx(0.65 * m.z_peak * di, rel=0.2)
    for e in res.edges:
        assert 3e-6 < e.metrics.settling_time < 20e-6 and e.metrics.trustworthy
        assert e.t[e.metrics.step_index] == 0.0
    assert b.loadbank.code == 0  # load released afterwards
    assert set(res.extra_csvs()) == {"waveform_rising.csv", "waveform_falling.csv"}


def test_single_edge_and_auto_trigger_pin(jafwak):
    res, b = run(jafwak, edges=["falling"], trigger_pin=None)
    lt = jafwak.load_tran
    assert [e.edge for e in res.edges] == ["falling"]
    # MSB that changes between the two codes (JAFWAK 1 -> 21: bits 2 and 4 change, so bit 4)
    assert res.trigger_pin == b.loadbank.trigger_pin(lt.code_low, lt.code_high)[0] == 4


def test_trigger_pin_must_change(cfg):
    lt = cfg.load_tran
    still = next(b for b in range(5) if not (lt.code_low ^ lt.code_high) >> b & 1)
    with pytest.raises(InstrumentError, match="never moves"):
        run(cfg, trigger_pin=cfg.loadbank.dio_pins[still])


def test_report_hints_post_when_dc_shift_exceeds_band(cfg):
    lt = cfg.load_tran.model_copy(update={"averages": 2})
    an = cfg.analysis.model_copy(update={"settle_reference": "pre"})  # the hint is for 'pre' only
    with open_bench(cfg, sim=True) as b:
        res = load_tran.run(b, lt, an)
    assert 'settle_reference = "post"' in res.report()


def test_floating_scope_recorded_not_hidden(cfg, monkeypatch):
    """Regression: scope 1 unconnected. The run finishes, keeps the real data and flags it."""
    from ldo_bench.instruments.base import DcReading

    lt = cfg.load_tran.model_copy(update={"averages": 2})
    with open_bench(cfg, sim=True) as b:
        floating = {1: DcReading(-0.0004, 2e-4, 8192), 2: DcReading(0.0, 1e-4, 8192)}
        monkeypatch.setattr(b.scope, "measure_dc", lambda *a, **k: floating)
        res = load_tran.run(b, lt, cfg.analysis)
    assert res.issues and any("VOUT reads -0.0004 V" in i for i in res.issues)
    assert "NOT A VALID MEASUREMENT" in res.report() and res.summary()["valid"] is False
    load_tran.plot(res, "x")  # plots the measured data with the issue box


def test_no_step_in_capture_recorded(cfg, monkeypatch):
    lt = cfg.load_tran.model_copy(update={"averages": 2})
    with open_bench(cfg, sim=True) as b:
        real = b.scope.capture

        def flat_ref(**k):
            c = real(**k)
            c.ch2[:] = 0.0  # trigger channel shows no edge
            return c

        monkeypatch.setattr(b.scope, "capture", flat_ref)
        res = load_tran.run(b, lt, cfg.analysis)
    assert all(e.issues for e in res.edges)
    assert any("no step found" in i for i in res.issues)
    assert len(res.edges[0].vout) > 1000  # the measured trace is kept


def test_never_triggers_keeps_untriggered_data(cfg, monkeypatch):
    lt = cfg.load_tran.model_copy(update={"averages": 3})
    with open_bench(cfg, sim=True) as b:
        real = b.scope.capture

        def no_trigger(**k):
            if not k.get("force"):
                raise InstrumentError("trigger timeout")
            return real(**k)

        monkeypatch.setattr(b.scope, "capture", no_trigger)
        res = load_tran.run(b, lt, cfg.analysis)
    assert any("never triggered" in i for i in res.issues)
    assert len(res.edges[0].vout) == lt.n_samples  # real (untriggered) capture, nothing invented


def test_edges_share_one_time_span(cfg):
    """Regression: each edge used to be zoomed to its own settling time, so a slow edge's panel
    covered ~8x more time than a fast one's and neither matched a scope's fixed time/div."""
    an = cfg.analysis.model_copy(update={"settle_reference": "post"})
    with open_bench(cfg, sim=True) as b:
        res = load_tran.run(b, cfg.load_tran.model_copy(update={"averages": 2}), an)
    res.edges[1].metrics = res.edges[1].metrics.__class__(
        **{**res.edges[1].metrics.__dict__, "settling_time": 200e-6}  # make one edge much slower
    )
    fig = load_tran.plot(res, "x")
    vout_axes = [ax for ax in fig.axes if "VOUT" in ax.get_ylabel()]
    assert vout_axes[0].get_xlim() == pytest.approx(vout_axes[1].get_xlim())
    res.plot_window = 100e-6  # --plot-us 100
    fig = load_tran.plot(res, "x")
    for ax in (ax for ax in fig.axes if "VOUT" in ax.get_ylabel()):
        lo, hi = ax.get_xlim()
        assert hi == pytest.approx(100, abs=0.2) and lo == pytest.approx(-15, abs=0.2)


@pytest.mark.parametrize("proc", ["load_tran", "line_tran"])
def test_zoom_plot_gives_each_edge_its_own_span(jafwak, proc):
    """plot.png shares one span (the slowest edge); plot_zoom.png sizes each edge to its own
    settling time, so a 20 us edge isn't squeezed next to a 200 us one (bench, 2026-11-10)."""
    from dataclasses import replace

    import matplotlib.pyplot as plt

    mod = load_tran if proc == "load_tran" else line_tran
    sec = jafwak.load_tran if proc == "load_tran" else jafwak.line_tran
    an = jafwak.analysis.model_copy(update={"settle_reference": "post"})
    with open_bench(jafwak, sim=True) as b:
        res = mod.run(b, sec.model_copy(update={"averages": 2}), an)
    assert not res.issues
    settle = (20e-6, 200e-6)
    for e, ts in zip(res.edges, settle, strict=True):
        e.metrics = replace(e.metrics, settling_time=ts)

    def right_ends(fig):
        return [a.get_xlim()[1] for a in fig.axes if a.get_ylabel().startswith("VOUT, scope")]

    # the axis ends at the last sample inside the span (0.1 us apart at 10 MS/s)
    assert right_ends(mod.plot(res, "x")) == pytest.approx([2.5 * 200 + 10] * 2, abs=0.5)  # shared span
    figs = mod.extra_plots(res, "x")
    assert set(figs) == {"plot_full.png", "plot_zoom.png"}
    assert right_ends(figs["plot_zoom.png"]) == pytest.approx([2.5 * ts * 1e6 + 10 for ts in settle], abs=0.5)
    plt.close("all")  # three figures per case: don't hold them for the rest of the suite


def test_legend_is_outside_the_data(jafwak):
    """The per-panel legend covered the VOUT trace and the 'settled' label (bench, 2026-11-10): one
    figure legend below the panels instead, each label once, with the cross-check scope's too."""
    import matplotlib.pyplot as plt

    an = jafwak.analysis.model_copy(update={"settle_reference": "post"})
    with open_bench(jafwak, sim=True, use_xscope=True) as b:
        res = load_tran.run(b, jafwak.load_tran.model_copy(update={"averages": 2}), an)
    figs = {"plot.png": load_tran.plot(res, "x"), **load_tran.extra_plots(res, "x")}
    for name, fig in figs.items():
        assert all(a.get_legend() is None for a in fig.axes), name  # nothing drawn over the data
        assert len(fig.legends) == 1, name
        labels = [t.get_text() for t in fig.legends[0].get_texts()]
        assert len(labels) == len(set(labels)) and {"Averaged", "Smoothed"} <= set(labels), name
        assert any(lbl.startswith("54621A") for lbl in labels), name
        # below every axes: its top is under the lowest axes' bottom (figure coordinates)
        fig.canvas.draw()
        leg_top = fig.legends[0].get_window_extent().transformed(fig.transFigure.inverted()).y1
        assert leg_top <= min(a.get_position().y0 for a in fig.axes) + 1e-6, name
    plt.close("all")


def test_supply_current_shows_vin_not_reaching_the_board(cfg, monkeypatch):
    """Bench, 2026-11-10: with the supply unplugged load-tran could only say VOUT was missing. The
    supply's current must rise by what the load bank draws between code_low and code_high."""
    from ldo_bench.instruments.base import DcReading

    lt = cfg.load_tran.model_copy(update={"averages": 2})
    with open_bench(cfg, sim=True) as b:
        monkeypatch.setattr(b.psu, "measure_current", lambda: 0.0)  # leads off: nothing flows
        real = b.scope.measure_dc
        ch = cfg.ad2.vout_channel

        def no_vout(*a, **k):
            r = dict(real(*a, **k))
            r[ch] = DcReading(0.0002, 1e-4, r[ch].n)
            return r

        monkeypatch.setattr(b.scope, "measure_dc", no_vout)
        res = load_tran.run(b, lt, cfg.analysis)
    msg = next(i for i in res.setup_issues if "supply current" in i)
    assert "VIN" in msg and "0.0 mA" in msg
    assert res.summary()["valid"] is False
    sc = res.summary()["supply_current"]
    assert sc["i_low_a"] == 0.0 and sc["i_high_a"] == 0.0 and sc["expected_rise_a"] > 0.01


def test_supply_current_recorded_and_clean_on_a_good_run(cfg):
    lt = cfg.load_tran.model_copy(update={"averages": 2})
    an = cfg.analysis.model_copy(update={"settle_reference": "post"})
    with open_bench(cfg, sim=True) as b:
        res = load_tran.run(b, lt, an)
    assert not any("supply current" in i for i in res.issues)
    sc = res.summary()["supply_current"]
    assert sc["i_high_a"] - sc["i_low_a"] == pytest.approx(sc["expected_rise_a"], rel=0.1)


def capture_with_step(jafwak, *, edge_found=True, vout_before=1.2):
    n, i0 = 1000, 200
    ref = np.where(np.arange(n) >= i0, 3.3, 0.0)
    vout = np.where(np.arange(n) >= i0, 1.2, vout_before)
    a = jafwak.ad2
    ch1, ch2 = (vout, ref) if a.vout_channel == 1 else (ref, vout)
    m = SettlingResult(1.2, 1.19, 0.001, 1.2, 1e-6, 1e-5, 1.2, 10e6, i0, "pre")
    return m, Capture(10e6, ch1, ch2, i0, edge_found=edge_found), ref


def test_capture_without_a_clean_edge_is_flagged(jafwak):
    m, cap, ref = capture_with_step(jafwak, edge_found=False)
    with open_bench(jafwak, sim=True) as b:
        issues = capture_issues(b, m, cap, ref, expected_swing=3.3, ref_desc="DIO 2")
    assert any("no clean edge" in i for i in issues)


def test_vout_before_the_step_is_checked_in_the_capture(jafwak):
    m, cap, ref = capture_with_step(jafwak, vout_before=0.0)  # DC check was fine, the capture isn't
    with open_bench(jafwak, sim=True) as b:
        issues = capture_issues(b, m, cap, ref, expected_swing=3.3, ref_desc="DIO 2")
    assert any("VOUT reads" in i for i in issues)


@pytest.mark.parametrize(
    ("scale", "flagged"), [(1 - SWING_TOLERANCE - 0.05, True), (1 - SWING_TOLERANCE + 0.05, False)]
)
def test_swing_tolerance_boundary(jafwak, scale, flagged):
    m, cap, ref = capture_with_step(jafwak)
    with open_bench(jafwak, sim=True) as b:
        issues = capture_issues(b, m, cap, ref, expected_swing=3.3 / scale, ref_desc="DIO 2")
    assert any("swings" in i for i in issues) == flagged
