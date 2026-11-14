"""vref-line: VREF (the BGR output, TP3) on the DMM while VIN sweeps down with the LDO on."""

import numpy as np
import pytest
from helpers import miswire
from pydantic import ValidationError

from ldo_bench import plotting
from ldo_bench.bench import open_bench
from ldo_bench.config import BenchConfig
from ldo_bench.instruments.base import InstrumentError
from ldo_bench.procedures import vref_line


def test_vref_line_on_sim(jafwak):
    with open_bench(jafwak, sim=True) as b:
        m = b.sim_model
        res = vref_line.run(b, jafwak.vref_line)
    vl = jafwak.vref_line
    assert not res.issues, res.issues
    s = res.summary()
    assert s["vref_at_vin_high_v"] == pytest.approx(
        m.vref_nominal + m.bgr_line * (vl.vin_high - 1.8), abs=1e-4
    )
    # the BGR's knee: VREF 1 % low a little below bgr_min_vin (it falls 1 V/V below it)
    knee = m.bgr_min_vin - vl.drop_fraction * s["vref_at_vin_high_v"]
    assert s["bgr_min_vin_v"] == pytest.approx(knee, abs=vl.vin_step)
    assert s["line_sensitivity_mv_per_v"] == pytest.approx(m.bgr_line * 1e3, abs=0.2)
    vins = [r["vin_set_v"] for r in res.rows]
    assert vins[0] == pytest.approx(vl.vin_high) and vins[-1] == pytest.approx(vl.vin_low)
    assert all(np.diff(vins) < 0)  # downward, as in the first plan
    plotting.close(vref_line.plot(res, "x"))


def test_dmm_not_on_vref_is_flagged(jafwak):
    with open_bench(jafwak, sim=True) as b:
        miswire(b, dmm_probe="open")
        res = vref_line.run(b, jafwak.vref_line)
    assert any("TP3" in i for i in res.issues)
    plotting.close(vref_line.plot(res, "x"))  # the invalid plot draws too


def test_ldo_not_enabled_is_flagged(jafwak):
    with open_bench(jafwak, sim=True) as b:
        miswire(b, en_pin=5)  # EN wired to the DIO but nothing drives it
        res = vref_line.run(b, jafwak.vref_line)
    assert any("enabled" in i for i in res.issues)


def test_needs_the_dmm(jafwak):
    with open_bench(jafwak, sim=True, use_dmm=False) as b, pytest.raises(InstrumentError, match="DMM"):
        vref_line.run(b, jafwak.vref_line)


def test_wiring_names_the_board_points(jafwak):
    with open_bench(jafwak, sim=True) as b:
        text = "\n".join(vref_line.wiring(b, jafwak.vref_line))
    assert "VREF (TP3)" in text and "VIN_DC (J7)" in text and "J13 pin 4 (OUTA)" in text


def test_config_limits(jafwak):
    data = jafwak.model_dump()
    data["vref_line"]["vin_high"] = 2.0  # above dut.vin_max = 1.9
    with pytest.raises(ValidationError, match="vin_max"):
        BenchConfig.model_validate(data)
    data["vref_line"].update(vin_high=1.5, vin_low=1.6)
    with pytest.raises(ValidationError, match="vin_low"):
        BenchConfig.model_validate(data)


def test_sweep_points_end_on_vin_low(jafwak):
    pts = vref_line.sweep_points(jafwak.vref_line)
    assert pts[0] == pytest.approx(1.9) and pts[-1] == pytest.approx(1.4)
    assert np.allclose(np.diff(pts), -0.02)


def test_vref_read_at_10_meg_and_10_gig(jafwak):
    """At vin_high the DMM reads VREF at 10 GOhm and at 10 MOhm: the difference shows how much the
    DMM's input loads the BGR, and gives its output resistance."""
    with open_bench(jafwak, sim=True) as b:
        m = b.sim_model
        res = vref_line.run(b, jafwak.vref_line)
    s = res.summary()
    drop = s["vref_hi_z_v"] - s["vref_10meg_v"]
    assert drop == pytest.approx(s["vref_hi_z_v"] * m.bgr_rout / (10e6 + m.bgr_rout), rel=0.2)
    assert s["bgr_rout_ohm"] == pytest.approx(m.bgr_rout, rel=0.3)
    assert "10 MOhm" in res.report()
    assert b.dmm.high_z  # left at 10 GOhm for the sweep


def test_heavily_loaded_vref_is_a_note_not_an_issue(jafwak):
    def high_rout(b):
        b.sim_model.bgr_rout = 200e3  # 23 mV drop at 10 MOhm

    with open_bench(jafwak, sim=True) as b:
        high_rout(b)
        res = vref_line.run(b, jafwak.vref_line)
    assert not res.issues
    assert any("10 MOhm" in n for n in res.notes)
