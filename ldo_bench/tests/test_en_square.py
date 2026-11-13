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
    assert CliRunner().invoke(app, [*base, "--shape", "sawtooth"]).exit_code == 2
