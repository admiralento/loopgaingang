import numpy as np
import pytest

from ldo_bench.bench import open_bench
from ldo_bench.instruments.base import Capture, InstrumentError
from ldo_bench.procedures import load_tran
from ldo_bench.procedures.transient import align_average


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
    # MSB that changes between the two codes (JAFWAK 1 -> 4: bit 2)
    assert res.trigger_pin == b.loadbank.trigger_pin(lt.code_low, lt.code_high)[0] == 2


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

    from ldo_bench.procedures import line_tran

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
