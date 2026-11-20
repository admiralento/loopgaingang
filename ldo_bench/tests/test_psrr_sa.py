"""psrr --meter sa: VIN from the AD2, VOUT from the simulated HP 8591E through U4 / J9, converted with an
sa-path-cal curve and checked against the AD2 in the run."""

import math

import numpy as np
import pytest
from helpers import config_path, invoke

from ldo_bench.bench import open_bench
from ldo_bench.instruments.base import InstrumentError
from ldo_bench.procedures import psrr
from ldo_bench.procedures.sa_path_cal import PathGainCurve

SIM_GAIN_DB = 20 * math.log10(0.98 * 50 / 97)  # the simulator's U4 into R14 + 50 ohm


def _cfg(jafwak, **upd):
    from ldo_bench.config import BenchConfig

    d = jafwak.model_dump()
    d["psrr"] |= {"meter": "sa", "averages": 2, "sa_cal_averages": 16, **upd}
    return BenchConfig.model_validate(d)


def _flat(db=SIM_GAIN_DB):
    return PathGainCurve.flat(db, 9e3, 2e6, "test curve")


def _run(cfg, curve=None, setup=None):
    with open_bench(cfg, sim=True, use_sa=True) as b:
        if setup:
            setup(b.sim_model)
        res = psrr.run(b, cfg.psrr, sa_cal=curve or _flat())
        link = b.sa.t
    return res, link, b.sim_model


def _model_psrr_db(m, f):
    h = m.psrr_h_dc * (1 + 1j * f / m.psrr_fz) / (1 + 1j * f / m.psrr_fp)  # psrr_h() is 0 once the LDO is off
    return -20 * math.log10(abs(h))


def test_vout_from_the_analyzer_above_sa_from(jafwak):
    cfg = _cfg(jafwak)
    res, link, m = _run(cfg)
    assert not res.issues, res.issues
    for p in res.points:
        if p.frequency >= cfg.psrr.sa_from:
            assert p.tone.vout_meter == "8591E" and not np.isfinite(p.phase_deg)
            assert p.psrr_db == pytest.approx(_model_psrr_db(m, p.frequency), abs=0.5)
            assert p.ad2_vout is not None
        else:
            assert p.tone.vout_meter == "AD2" and p.ad2_vout is None
    s = res.summary()["sa_cal"]
    assert s["source"] == "test curve" and s["checks"]  # checked against the AD2 in the run
    assert all(abs(c["diff_db"]) < 0.5 for c in s["checks"])
    assert "IP;" in link.sent[-3:] and link.sent[-1] == "<close>"  # left preset, closed


def test_a_rolled_off_path_is_corrected_by_its_curve(jafwak):
    """A DC block rolling off (30 kHz corner): the curve carries it, so the PSRR is still right."""
    cfg = _cfg(jafwak)
    corner = 30e3

    def hp(f):
        return f / np.hypot(f, corner)

    f = np.geomspace(9e3, 2e6, 40)
    curve = PathGainCurve(f, SIM_GAIN_DB + 20 * np.log10(hp(f)), "rolled-off curve")
    res, _, m = _run(cfg, curve, setup=lambda m: setattr(m, "sa_path", hp))
    assert not res.issues, res.issues
    for p in res.points:
        if p.frequency >= cfg.psrr.sa_from:
            assert p.psrr_db == pytest.approx(_model_psrr_db(m, p.frequency), abs=0.5)


def test_a_curve_that_doesnt_match_todays_path_is_a_setup_issue(jafwak):
    """The calibration was made with another cable / DC block: the AD2 disagrees, the run is invalid."""
    res, _, _ = _run(_cfg(jafwak), setup=lambda m: setattr(m, "sa_u4_gain", 0.6))
    assert any("doesn't match this run" in i for i in res.issues), res.issues


def test_a_curve_that_doesnt_cover_the_sweep_is_refused_before_power_up(jafwak):
    cfg = _cfg(jafwak)
    short = PathGainCurve.flat(SIM_GAIN_DB, 9e3, 200e3, "short curve")
    with open_bench(cfg, sim=True, use_sa=True) as b, pytest.raises(InstrumentError, match="outside"):
        psrr.run(b, cfg.psrr, sa_cal=short)
    assert not b.sim_model.psu_on


def test_the_analyzers_raw_readings_are_saved(jafwak):
    res, _, _ = _run(_cfg(jafwak))
    for row in res.rows:
        if row["frequency_hz"] >= 30e3:
            assert row["sa_vpeak_v"] > 0 and row["sa_floor_vpeak_v"] > 0
            assert row["sa_rbw_hz"] > 0 and abs(row["sa_peak_frequency_hz"] - row["frequency_hz"]) < 1e3
            assert math.isfinite(row["sa_dbm"]) and math.isfinite(row["sa_margin_db"])
        else:
            assert math.isnan(row["sa_vpeak_v"])
    assert any("test curve" in n for n in res.notes)


def test_config_rules(jafwak):
    with pytest.raises(ValueError, match="sa_cal_to"):
        _cfg(jafwak, sa_from=100e3, sa_cal_to=50e3)
    with pytest.raises(ValueError, match="sa_from"):
        _cfg(jafwak, sa_from=5e3)
    from ldo_bench.config import BenchConfig

    d = jafwak.model_dump()
    d["psrr"]["meter"] = "sa"
    d["sa"] = None
    with pytest.raises(ValueError, match=r"\[sa\]"):
        BenchConfig.model_validate(d)
    assert jafwak.psrr.sa_from == 30e3


def test_wiring_names_u4_j9_and_the_dc_block(jafwak):
    cfg = _cfg(jafwak)
    with open_bench(cfg, sim=True, use_sa=True) as b:
        text = "\n".join(psrr.wiring(b, cfg.psrr))
    assert "U4" in text and "J9" in text and "DC block" in text and "J10" in text


def test_cli_meter_sa(tmp_path):
    r = invoke(
        "psrr",
        "-c",
        config_path("jafwak"),
        "--sim",
        "--yes",
        "--meter",
        "sa",
        "--fstart",
        "1e4",
        "--ppd",
        "2",
    )
    assert r.exit_code == 0, r.output
    assert "8591E" in r.output and "simulator (flat)" in r.output
    r = invoke("psrr", "-c", config_path("demo_board"), "--sim", "--yes", "--meter", "sa")
    assert r.exit_code == 2, r.output
    # on hardware with no calibration saved yet: refused before anything opens
    r = invoke("psrr", "-c", config_path("jafwak"), "--yes", "--meter", "sa", "-o", str(tmp_path))
    assert r.exit_code == 2 and "sa-path-cal" in r.output, r.output
