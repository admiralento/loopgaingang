import numpy as np
import pytest

from ldo_bench.analysis import find_dropout, line_regulation
from ldo_bench.bench import open_bench
from ldo_bench.config import LineRegConfig
from ldo_bench.procedures import line_reg


def ideal_ldo(vin, vout=1.2, vdo=0.15, line=1e-3):
    vin = np.asarray(vin, float)
    reg = vout + line * (vin - 2.0)
    return np.minimum(reg, vin - vdo)


def test_find_dropout_interpolates():
    vin = np.arange(1.2, 3.01, 0.05)
    vd, vo = find_dropout(vin, ideal_ldo(vin), 0.01)
    # VOUT = VIN - 0.15 crosses 0.99 * VOUT(3 V) = 0.99 * 1.201 between grid points
    assert vo == pytest.approx(0.99 * ideal_ldo([3.0])[0])
    # Interpolating across the knee is accurate to a fraction of the 50 mV step.
    assert vd == pytest.approx(vo + 0.15, abs=0.005)


def test_no_dropout_in_sweep():
    vin = np.arange(2.0, 3.01, 0.1)
    r = line_regulation(vin, ideal_ldo(vin))
    assert np.isnan(r.dropout_vin)
    assert r.fit_vin_min == pytest.approx(2.0)
    assert r.slope == pytest.approx(1e-3)


def test_fit_starts_above_knee():
    vin = np.arange(1.2, 3.01, 0.05)
    r = line_regulation(vin, ideal_ldo(vin), knee_margin=0.15)
    assert r.fit_vin_min >= r.dropout_vin + 0.15 - 1e-9
    assert r.slope == pytest.approx(1e-3, rel=1e-6)  # knee excluded from the fit
    assert r.percent_per_v == pytest.approx(100 * 1e-3 / r.v_ref)


def test_explicit_fit_min():
    vin = np.arange(1.2, 3.01, 0.05)
    r = line_regulation(vin, ideal_ldo(vin), fit_vin_min=2.5)
    assert r.fit_vin_min == pytest.approx(2.5)


def test_sweep_points_include_stop():
    lr = LineRegConfig(vin_start=1.25, vin_stop=3.3, vin_step=0.05)
    pts = line_reg.sweep_points(lr)
    assert pts[0] == 1.25 and pts[-1] == 3.3 and len(pts) == 42
    lr = LineRegConfig(vin_start=1.0, vin_stop=2.05, vin_step=0.1)
    assert line_reg.sweep_points(lr)[-1] == 2.05


def test_vin_stop_must_exceed_start():
    with pytest.raises(ValueError):
        LineRegConfig(vin_start=3.0, vin_stop=2.0, vin_step=0.1)


def test_procedure_on_sim(bench):
    res = line_reg.run(bench, bench.cfg.line_reg)
    m = bench.sim_model
    assert res.reg.slope == pytest.approx(m.line_reg, rel=0.1)
    assert np.isfinite(res.reg.dropout_vin)
    # dropout voltage ~ model dropout + I * r_dropout, plus the 1 % criterion
    expected = m.dropout + m.r_dropout * res.i_load + 0.01 * res.reg.v_ref
    assert res.reg.dropout_voltage == pytest.approx(expected, abs=0.02)
    assert bench.loadbank.code == 0  # load released afterwards


def test_procedure_uses_dmm_when_configured(cfg):
    with open_bench(cfg, sim=True) as b:
        assert b.dmm is not None
        res = line_reg.run(b, cfg.line_reg)
    assert res.vout_source == "dmm"
    with open_bench(cfg, sim=True, use_dmm=False) as b:
        assert line_reg.run(b, cfg.line_reg).vout_source == "ad2"


def test_vin_from_psu_needs_no_scope(cfg, monkeypatch):
    lr = cfg.line_reg.model_copy(update={"vin_source": "psu"})
    with open_bench(cfg, sim=True) as b:

        def no_scope(*a, **k):
            raise AssertionError("AD2 scope used in vin_source='psu' mode")

        monkeypatch.setattr(b.scope, "measure_dc", no_scope)
        wiring = line_reg.wiring(b, lr)
        res = line_reg.run(b, lr)
    assert not any("Scope" in w for w in wiring)
    assert res.vin_source == "psu" and res.vout_source == "dmm"
    assert all(r["vin_source"] == "psu" for r in res.rows)
    assert res.reg.slope == pytest.approx(b.sim_model.line_reg, rel=0.1)
    assert "PSU readback" in res.vin_label


def test_vin_source_validated():
    with pytest.raises(ValueError):
        LineRegConfig(vin_start=1, vin_stop=2, vin_step=0.1, vin_source="dmm")
