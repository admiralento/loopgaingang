"""enable-sine --shape square: EN_AIN -> EN_OUT propagation delay (first bring-up plan, Test 1.8)."""

from pathlib import Path

import numpy as np
import pytest
from typer.testing import CliRunner

from ldo_bench import plotting
from ldo_bench.analysis import propagation_delays
from ldo_bench.bench import open_bench
from ldo_bench.cli import app
from ldo_bench.procedures import en_sine

CFG = str(Path(__file__).parents[1] / "configs" / "jafwak.toml")


def square_pair(delay_lh, delay_hl, invert=False, f=1e5, fs=100e6, cycles=4):
    """Input square (high for the first half period) and an output that rises delay_lh after the
    input rises and falls delay_hl after it falls (or the inverse of that)."""
    t = np.arange(int(cycles / f * fs)) / fs
    u = np.mod(t, 1 / f)  # time within the period
    ain = np.where(u < 0.5 / f, 1.8, 0.0)
    high = (u >= delay_lh) & (u < 0.5 / f + delay_hl)
    out = 1.8 * (~high if invert else high)
    return t, ain, out.astype(float), fs


def test_propagation_delays_non_inverting():
    _, ain, out, fs = square_pair(150e-9, 90e-9)
    d = propagation_delays(ain, out, fs)
    assert d.lh == pytest.approx(150e-9, abs=15e-9)  # output low -> high
    assert d.hl == pytest.approx(90e-9, abs=15e-9)
    assert not d.inverting and d.n_lh >= 3 and d.n_hl >= 3


def test_propagation_delays_inverting():
    """~EN_OUT: the output rises when the input falls; LH / HL still name the output's edge."""
    _, ain, out, fs = square_pair(150e-9, 90e-9, invert=True)
    d = propagation_delays(ain, out, fs)
    assert d.inverting
    assert d.lh == pytest.approx(90e-9, abs=15e-9) and d.hl == pytest.approx(150e-9, abs=15e-9)


def test_propagation_delays_no_output_edges():
    _, ain, _, fs = square_pair(1e-7, 1e-7)
    d = propagation_delays(ain, np.zeros_like(ain), fs)
    assert np.isnan(d.lh) and np.isnan(d.hl)


def test_square_on_sim_measures_the_delay(jafwak):
    es = jafwak.en_sine.model_copy(update={"shape": "square", "frequency": 1e5, "captures": 2})
    with open_bench(jafwak, sim=True) as b:
        b.sim_model.en_delay = 200e-9
        res = en_sine.run(b, es)
    assert not res.issues, res.issues
    assert res.sample_rate == pytest.approx(100e6)  # capped at the AD2's maximum
    s = res.summary()
    assert s["tpd_lh"]["mean"] == pytest.approx(200e-9, abs=20e-9)
    assert s["tpd_hl"]["mean"] == pytest.approx(200e-9, abs=20e-9)
    assert s["vt_rise"] is None and s["hysteresis"] is None  # a square doesn't measure thresholds
    text = res.report()
    assert "propagation delay" in text and "VT+" not in text
    plotting.close(en_sine.plot(res, "x"))


def test_square_delay_under_two_samples_is_a_bound(jafwak):
    es = jafwak.en_sine.model_copy(update={"shape": "square", "frequency": 1e3, "captures": 1})
    with open_bench(jafwak, sim=True) as b:
        b.sim_model.en_delay = 200e-9  # far under 2 x 488 ns at 2 MS/s
        res = en_sine.run(b, es)
    line = next(ln for ln in res.report().splitlines() if "propagation delay" in ln)
    assert "under 977 ns" in line, line


def test_cli_square(tmp_path):
    base = ["enable-sine", "-c", CFG, "--sim", "--yes", "-o", str(tmp_path), "--captures", "1"]
    r = CliRunner().invoke(app, [*base, "--shape", "square", "--freq", "1e5"])
    assert r.exit_code == 0, r.output
    assert "square 0.00-1.80 V at 100000 Hz" in r.output


def _report(jafwak, **update) -> str:
    es = jafwak.en_sine.model_copy(update={"captures": 1, **update})
    with open_bench(jafwak, sim=True) as b:
        return en_sine.run(b, es).report()


def test_slow_wave_points_to_the_square_for_edge_times(jafwak):
    """At 2 MS/s the edges are a bound: the report says which run resolves them."""
    text = _report(jafwak, shape="sine", frequency=1e3)
    assert "under" in next(ln for ln in text.splitlines() if "rise / fall" in ln)
    assert "--shape square --freq 1e5" in text


def test_square_does_not_point_to_itself(jafwak):
    assert "--shape square" not in _report(jafwak, shape="square", frequency=1e5)


def test_edge_window_finds_each_edge():
    _, _, out, _ = square_pair(150e-9, 90e-9)
    for rising in (True, False):
        w = en_sine.edge_window(out, rising, 12)
        seg = out[w]
        assert len(seg) == 26
        assert (seg[0] < 0.9) == rising and (seg[-1] > 0.9) == rising  # the edge is inside, centred


def test_edge_window_skips_edges_at_the_capture_ends():
    out = np.r_[0.0, 1.8, 1.8, np.zeros(50), np.full(50, 1.8)]  # first rising edge at sample 0
    w = en_sine.edge_window(out, True, 12)
    assert w.start == 52 - 12  # the second rising edge, the first has no samples before it
    assert en_sine.edge_window(np.zeros(100), True, 12) is None


@pytest.mark.parametrize("shape,freq", [("square", 1e5), ("triangle", 1.0)])
def test_plot_zooms_into_one_rising_and_one_falling_edge(jafwak, shape, freq):
    es = jafwak.en_sine.model_copy(update={"shape": shape, "frequency": freq, "captures": 1})
    with open_bench(jafwak, sim=True) as b:
        b.sim_model.en_delay = 200e-9
        res = en_sine.run(b, es)
    fig = en_sine.plot(res, "x")
    titles = [ax.get_title("left") for ax in fig.axes]
    rise = next(ax for ax in fig.axes if ax.get_title("left").startswith("Output rising edge:"))
    fall = next(ax for ax in fig.axes if ax.get_title("left").startswith("Output falling edge:"))
    assert "none" not in rise.get_title("left") and "none" not in fall.get_title("left"), titles
    for ax in (rise, fall):
        y = ax.lines[1].get_ydata()  # EN_OUT, close up: it crosses its midpoint inside the window
        assert y.min() < 0.5 * y.max() and len(y) >= 2 * en_sine.EDGE_MIN_HALF_WIDTH
    plotting.close(fig)
