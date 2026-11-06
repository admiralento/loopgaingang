"""Settling analysis on synthetic waveforms with known answers."""

import numpy as np
import pytest

from ldo_bench.analysis import find_step, settling_analysis, smooth

FS = 10e6
N = 8192
I0 = 1600  # step at 20 % of the capture


def step_response(amp=-0.02, tau=4e-6, f_ring=150e3, v0=1.0, dv_final=0.0, noise=0.0, glitch=0.0, seed=0):
    t = (np.arange(N) - I0) / FS
    ts = np.where(t >= 0, t, 0)
    # Response starts at zero and builds up (a real LDO can't jump to the full dip instantly).
    if f_ring:  # noqa: SIM108 (clearer as two branches)
        resp = np.exp(-ts / tau) * np.sin(2 * np.pi * f_ring * ts)
    else:
        resp = 1 - np.exp(-ts / tau)  # slow first-order drift
    v = np.where(t >= 0, v0 + dv_final + amp * resp, v0)
    v = v + np.random.default_rng(seed).normal(0, noise, N)
    v[I0] += glitch
    ref = np.where(t >= 0, 3.3, 0.0)
    return v, ref


def test_find_step():
    _, ref = step_response()
    assert find_step(ref, "rising") == I0
    assert find_step(ref[::-1].copy(), "falling") == N - I0
    assert find_step(np.zeros(100), "rising") == -1


def test_smooth_matches_moving_average_in_the_middle():
    x = np.random.default_rng(1).normal(size=200)
    assert smooth(x, 3)[100] == pytest.approx(x[97:104].mean())
    assert smooth(x, 3)[0] == pytest.approx(x[:4].mean())  # shrinks at the ends


def test_dip_and_settling_of_decaying_ring():
    v, ref = step_response(amp=-0.02, tau=4e-6)
    r, _ = settling_analysis(v, ref, FS, "rising")
    true_dip = v.min() - 1.0  # ~ -13.6 mV at ~1.4 us
    assert r.nominal == pytest.approx(1.0)
    # The 1.1 us moving average attenuates a 150 kHz ring by sinc(pi f T) = 0.956, like the
    # WaveForms script does, so the reported dip is ~4.5 % smaller than the raw minimum.
    t_avg = (2 * int(1e-6 * FS / 2) + 1) / FS
    sinc = np.sinc(150e3 * t_avg)
    assert r.deviation == pytest.approx(true_dip * sinc, rel=0.01)
    assert r.band == pytest.approx(0.1 * abs(r.deviation))
    # envelope 0.02 * exp(-t/tau) falls inside the band at tau * ln(0.02 / band) ~ 10.7 us;
    # the last excursion outside is at or a bit before that
    t_env = 4e-6 * np.log(0.02 / r.band)
    assert t_env - 3.5e-6 < r.settling_time <= t_env + 0.3e-6
    assert r.trustworthy


def test_glitch_is_blanked():
    clean, ref = step_response()
    spiky, _ = step_response(glitch=0.5)
    a, _ = settling_analysis(clean, ref, FS, "rising")
    b, _ = settling_analysis(spiky, ref, FS, "rising")
    assert b.peak == pytest.approx(a.peak, abs=1e-4)


def test_noise_reported_and_flags_untrustworthy():
    v, ref = step_response(amp=-0.002, noise=2e-3)
    r, _ = settling_analysis(v, ref, FS, "rising")
    assert r.noise > r.band / 3 and not r.trustworthy


def test_never_settles_gives_nan():
    v, ref = step_response(tau=1.0, f_ring=0.0)  # decays far slower than the capture
    r, _ = settling_analysis(v, ref, FS, "rising")
    assert np.isnan(r.settling_time)


def test_no_edge_gives_nan():
    v, _ = step_response()
    r, _ = settling_analysis(v, np.zeros(N), FS, "rising")
    assert np.isnan(r.nominal) and r.step_index == -1


def test_post_reference_settles_to_final_level():
    # load step with 5 mV of DC load regulation: 'pre' never comes back to the old level
    v, ref = step_response(amp=-0.02, dv_final=-0.005)
    pre, _ = settling_analysis(v, ref, FS, "rising", reference="pre")
    post, _ = settling_analysis(v, ref, FS, "rising", reference="post")
    assert np.isnan(pre.settling_time)  # final level is outside the +/-2 mV 'pre' band
    assert post.settle_level == pytest.approx(0.995, abs=2e-4)
    assert 0 < post.settling_time < 15e-6
