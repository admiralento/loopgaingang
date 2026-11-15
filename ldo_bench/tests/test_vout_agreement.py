"""The AD2's VOUT must agree with the DMM's in the DC tests, or the run is flagged.

Regression (bench, 2026-11-10): AD2 scope 1 came off VOUT during load-reg and read ~16 mV at every
code while the DMM read ~0.99 V. The run was marked valid because VOUT came from the DMM.
"""

import pytest

from ldo_bench.bench import open_bench
from ldo_bench.instruments.base import DcReading
from ldo_bench.procedures import line_reg, load_reg


def _ad2_vout_reads(b, monkeypatch, *, fixed=None, offset=0.0):
    """Make AD2 scope 1 read a fixed VOUT (probe off) or the real one plus an offset; VIN untouched."""
    real = b.scope.measure_dc
    ch = b.cfg.ad2.vout_channel

    def measure_dc(*a, **k):
        r = dict(real(*a, **k))
        v = r[ch]
        r[ch] = DcReading(fixed if fixed is not None else v.mean + offset, v.std, v.n)
        return r

    monkeypatch.setattr(b.scope, "measure_dc", measure_dc)


@pytest.mark.parametrize("proc", [load_reg, line_reg])
def test_ad2_off_vout_while_dmm_reads_it_is_a_setup_problem(cfg, proc, monkeypatch):
    # line-reg reads the AD2 only with vin_source = "ad2" (the demo board config uses "psu")
    sec = cfg.load_reg if proc is load_reg else cfg.line_reg.model_copy(update={"vin_source": "ad2"})
    with open_bench(cfg, sim=True) as b:
        assert b.dmm is not None
        _ad2_vout_reads(b, monkeypatch, fixed=0.016)
        res = proc.run(b, sec)
    assert res.vout_source == "dmm"
    assert any("disagrees with the DMM" in i and "scope 1" in i for i in res.issues), res.issues
    assert res.summary()["valid"] is False


@pytest.mark.parametrize("proc", [load_reg, line_reg])
def test_small_ad2_offset_is_not_flagged(cfg, proc, monkeypatch):
    # the bench AD2 read 1-8 mV off the DMM with the probes a few mm apart: not a setup problem
    sec = cfg.load_reg if proc is load_reg else cfg.line_reg
    with open_bench(cfg, sim=True) as b:
        _ad2_vout_reads(b, monkeypatch, offset=-0.008)
        res = proc.run(b, sec)
    assert not any("disagrees with the DMM" in i for i in res.issues)


def test_no_check_without_a_dmm(cfg, monkeypatch):
    with open_bench(cfg, sim=True, use_dmm=False) as b:
        assert b.vout_agreement_issue(b.measure_point(0.01)) is None


def test_line_reg_from_psu_readback_does_not_read_the_ad2(cfg, monkeypatch):
    # vin_source = "psu": only the DMM is on the board, so there is nothing to compare
    lr = cfg.line_reg.model_copy(update={"vin_source": "psu"})
    with open_bench(cfg, sim=True) as b:
        res = line_reg.run(b, lr)
    assert not any("disagrees with the DMM" in i for i in res.issues)
