import json

import numpy as np
import pytest

from ldo_bench.analysis import linear_fit, load_regulation
from ldo_bench.procedures import load_reg
from ldo_bench.results import RunDir


def test_linear_fit_exact():
    f = linear_fit([0, 1, 2], [1.0, 0.9, 0.8])
    assert f.slope == pytest.approx(-0.1) and f.intercept == pytest.approx(1.0) and f.r2 == pytest.approx(1)


def test_linear_fit_degenerate():
    assert np.isnan(linear_fit([1, 1], [0, 1]).slope)


def test_load_regulation_numbers():
    i = np.array([0.0, 0.05, 0.1])
    v = 1.2 - 0.02 * i  # 20 mOhm
    r = load_regulation(i, v)
    assert r.r_out == pytest.approx(0.02)
    assert r.delta_v == pytest.approx(0.002)
    assert r.percent == pytest.approx(100 * 0.002 / 1.2)


def test_load_regulation_unsorted_input():
    r = load_regulation([0.1, 0.0, 0.05], [1.198, 1.2, 1.199])
    assert r.i_min == 0.0 and r.v_at_i_min == 1.2 and r.delta_v == pytest.approx(0.002)


def test_procedure_recovers_sim_output_resistance(bench):
    res = load_reg.run(bench, bench.cfg.load_reg)
    assert len(res.rows) == len(bench.loadbank.allowed_codes())  # 32, or 5 under JAFWAK's 20 mA limit
    # sim model: r_out = 50 mOhm
    assert res.reg.r_out == pytest.approx(bench.sim_model.r_out, rel=0.1)
    assert res.reg.r2 > 0.99
    # supply off and load released afterwards
    assert bench.loadbank.code == 0


def test_codes_subset(bench):
    top = bench.loadbank.max_allowed_code
    lr = bench.cfg.load_reg.model_copy(update={"codes": [0, top], "psu_readback": False})
    res = load_reg.run(bench, lr)
    assert [r["code"] for r in res.rows] == [0, top] and "i_psu_a" not in res.rows[0]


def test_run_folder(bench, tmp_path):
    res = load_reg.run(bench, bench.cfg.load_reg)
    run = RunDir(tmp_path, load_reg.NAME, bench.cfg)
    run.write_csv("data.csv", res.rows)
    run.write_json("summary.json", res.summary())
    run.write_meta(idns=bench.idns(), sim=True)
    run.save_fig(load_reg.plot(res, bench.cfg.dut.name))
    meta = json.loads((run.path / "meta.json").read_text())
    assert meta["sim"] is True and meta["config"]["dut"]["name"] == bench.cfg.dut.name
    assert (run.path / "plot.png").stat().st_size > 10_000
    header = (run.path / "data.csv").read_text().splitlines()[0].split(",")
    assert {"code", "i_est_a", "i_expected_a", "vout_v", "vout_ok"} <= set(header)


def test_load_regulation_is_a_least_squares_fit_not_the_endpoints():
    i = np.array([0, 1, 2, 3, 4]) * 1e-3
    v = 1.2 - 3.0 * i + 1e5 * i**3  # curved, asymmetric: the endpoints' slope differs from the fit's
    r = load_regulation(i, v)
    assert r.slope == pytest.approx(np.polyfit(i, v, 1)[0])
    assert r.slope != pytest.approx((v[-1] - v[0]) / (i[-1] - i[0]))
