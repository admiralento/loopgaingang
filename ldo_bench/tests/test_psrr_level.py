"""psrr levelling past the buffer's roll-off (bench 2026-11-20: U2's ~5 ohm into ~0.5 uF on VIN, ~60 kHz),
small tones on the analyzer's points, and more AD2 averages where the path gain is measured."""

from itertools import pairwise

import pytest

from ldo_bench.bench import open_bench
from ldo_bench.procedures import psrr
from ldo_bench.procedures.line_tran import vin_limit


def _cfg(jafwak, **upd):
    from ldo_bench.config import BenchConfig

    d = jafwak.model_dump()
    d["psrr"] |= {"averages": 2, **upd}
    return BenchConfig.model_validate(d)


def _run(cfg, fc=60e3, setup=None, spy=None):
    with open_bench(cfg, sim=True, use_sa=cfg.psrr.meter == "sa") as b:
        b.sim_model.buffer_fc = fc
        if setup:
            setup(b.sim_model)
        if spy:
            spy(b)
        res = psrr.run(b, cfg.psrr)
        limit = vin_limit(b)
    return res, limit


def test_levels_vin_to_the_target_past_the_rolloff(jafwak):
    cfg = _cfg(jafwak)  # 100 Hz - 1 MHz, 3 per decade: 100 kHz is one of them
    plays: list[tuple[float, float]] = []

    def spy(b):
        real = b.vin_sine_from_wavegen

        def sine(*, dc, amplitude, frequency):
            plays.append((frequency, amplitude))
            real(dc=dc, amplitude=amplitude, frequency=frequency)

        b.vin_sine_from_wavegen = sine

    res, limit = _run(cfg, spy=spy)
    target = cfg.psrr.amplitude
    for p in res.points:
        assert p.tone.vin_dc + p.vin_amp <= limit  # never over the VIN limit
        if p.frequency <= 100e3:
            assert p.vin_amp == pytest.approx(target, rel=cfg.psrr.level_tolerance), p.frequency
    at_100k = [a for f, a in plays if f == pytest.approx(100e3, rel=0.01)]
    assert max(at_100k) > 0.081  # past the old cap (VIN DC + W1 amplitude <= 1.88 V)
    for (f0, a0), (f1, a1) in pairwise(plays):
        if f0 == f1:
            assert a1 <= psrr.LEVEL_MAX_STEP * a0 + 1e-9  # one step at a time


def test_small_tones_are_valid_on_the_analyzers_points_only(jafwak):
    res_sa, _ = _run(_cfg(jafwak, meter="sa"))
    high = [p for p in res_sa.points if p.frequency >= 400e3]
    assert high and all(p.vin_amp < 0.03 for p in high)  # the tone is small up there
    assert not any("only carries" in i for p in high for i in p.issues), [p.issues for p in high]
    res_ad2, _ = _run(_cfg(jafwak))
    assert any("only carries" in i for p in res_ad2.points if p.frequency >= 400e3 for i in p.issues)


def test_overlap_points_take_sa_cal_averages(jafwak):
    cfg = _cfg(jafwak, meter="sa", f_start=1e3, f_stop=100e3)
    counts: dict[float, int] = {}

    def spy(b):
        real, m = b.scope.capture, b.sim_model

        def capture(**kw):
            f = next(iter(m.awg_wave.values()))[1]
            counts[f] = counts.get(f, 0) + 1
            return real(**kw)

        b.scope.capture = capture

    _run(cfg, spy=spy)
    for f, n in counts.items():
        k = cfg.psrr.sa_cal_averages if cfg.psrr.sa_from <= f <= cfg.psrr.sa_cal_to else cfg.psrr.averages
        assert n % k == 0 and n >= k, (f, n)


def test_more_overlap_averages_find_a_path_gain_four_dont(jafwak):
    def noisy(m):
        m.noise_rms = 4e-3  # an AD2 floor near VOUT's 10-50 kHz ripple, as on the bench

    res4, _ = _run(_cfg(jafwak, meter="sa", sa_cal_averages=2), setup=noisy)
    res32, _ = _run(_cfg(jafwak, meter="sa"), setup=noisy)
    assert res4.path_gain_db is None
    assert res32.path_gain_db is not None, res32.issues


def test_a_gain_that_rises_between_frequencies_cant_push_vin_over(jafwak):
    """With VIN's DC near its limit, the amplitude carried to the next frequency allows for the buffer's
    gain rising up to LEVEL_MAX_STEP x (here 0.3 -> 0.42 at 50 kHz): VIN stays under the limit, no stop."""
    cfg = _cfg(jafwak, vin=1.82, f_start=10e3, f_stop=200e3)

    def step(m):
        m.buffer_offset = 0.2  # U2 reads 200 mV high: W1 sits near 1.62 V, so W1's own cap isn't what binds
        m.buffer_gain = lambda f: 0.3 if f < 50e3 else 0.42

    res, limit = _run(cfg, fc=None, setup=step)  # raises InstrumentError if the hard stop trips
    assert all(p.tone.vin_dc + p.vin_amp <= limit for p in res.points)
