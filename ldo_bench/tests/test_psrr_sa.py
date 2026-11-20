"""psrr --meter sa: VIN from the AD2, VOUT from the simulated HP 8591E through U4 / J9, path gain from the overlap."""

import math

import numpy as np
import pytest
from helpers import config_path, invoke

from ldo_bench.bench import open_bench
from ldo_bench.procedures import psrr


def _cfg(jafwak, **upd):
    from ldo_bench.config import BenchConfig

    d = jafwak.model_dump()
    d["psrr"] |= {"meter": "sa", "averages": 2, **upd}
    return BenchConfig.model_validate(d)


def _run(cfg, setup=None):
    with open_bench(cfg, sim=True, use_sa=True) as b:
        if setup:
            setup(b.sim_model)
        res = psrr.run(b, cfg.psrr)
        link = b.sa.t
    return res, link, b.sim_model


def test_vout_from_the_analyzer_above_sa_from(jafwak):
    cfg = _cfg(jafwak)
    res, link, m = _run(cfg)
    assert not res.issues, res.issues
    expected_gain_db = 20 * math.log10(m.sa_u4_gain * 50 / 97)
    assert res.path_gain_db == pytest.approx(expected_gain_db, abs=0.3)
    for p in res.points:
        if p.frequency >= cfg.psrr.sa_from:
            assert p.tone.vout_meter == "8591E" and not np.isfinite(p.phase_deg)
            f = p.frequency  # the model's transfer (psrr_h() reads 0 now: the LDO is off after the run)
            h = m.psrr_h_dc * (1 + 1j * f / m.psrr_fz) / (1 + 1j * f / m.psrr_fp)
            want = -20 * math.log10(abs(h))
            assert p.psrr_db == pytest.approx(want, abs=0.5)
            assert p.ad2_vout is not None
        else:
            assert p.tone.vout_meter == "AD2" and p.ad2_vout is None
    assert res.summary()["sa_path_gain_db"] == pytest.approx(res.path_gain_db)
    assert "IP;" in link.sent[-3:] and link.sent[-1] == "<close>"  # left preset, closed


def test_no_overlap_point_means_no_path_gain_and_invalid_points(jafwak):
    res, _, _ = _run(_cfg(jafwak, sa_cal_margin_db=200.0))
    assert res.path_gain_db is None
    sa_points = [p for p in res.points if p.frequency >= 10e3]
    assert sa_points and all(any("couldn't be measured" in i for i in p.issues) for p in sa_points)
    assert all(p.tone.vout_meter == "AD2" for p in sa_points)  # nothing converted with a guessed gain


def test_a_path_that_isnt_flat_is_a_setup_issue(jafwak):
    def dc_block(m):  # a DC block with a 30 kHz high-pass corner
        m.sa_path = lambda f: f / math.hypot(f, 30e3)

    res, _, _ = _run(_cfg(jafwak), setup=dc_block)
    assert any("isn't flat" in i for i in res.issues), res.issues


def test_a_path_gain_far_from_u4_into_50_ohm_gets_a_note(jafwak):
    res, _, _ = _run(_cfg(jafwak), setup=lambda m: setattr(m, "sa_u4_gain", 0.3))
    assert not res.issues, res.issues
    assert any("should give" in n for n in res.notes), res.notes


def test_config_rules(jafwak):
    with pytest.raises(ValueError, match="sa_cal_to"):
        _cfg(jafwak, sa_from=50e3, sa_cal_to=20e3)
    with pytest.raises(ValueError, match="sa_from"):
        _cfg(jafwak, sa_from=5e3)
    from ldo_bench.config import BenchConfig

    d = jafwak.model_dump()
    d["psrr"]["meter"] = "sa"
    d["sa"] = None
    with pytest.raises(ValueError, match=r"\[sa\]"):
        BenchConfig.model_validate(d)


def test_wiring_names_u4_j9_and_the_dc_block(jafwak):
    cfg = _cfg(jafwak)
    with open_bench(cfg, sim=True, use_sa=True) as b:
        text = "\n".join(psrr.wiring(b, cfg.psrr))
    assert "U4" in text and "J9" in text and "DC block" in text and "J10" in text


def test_cli_meter_sa():
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
    assert "8591E" in r.output and "path gain" in r.output
    r = invoke("psrr", "-c", config_path("demo_board"), "--sim", "--yes", "--meter", "sa")
    assert r.exit_code == 2, r.output


def test_the_analyzers_raw_readings_are_saved_even_without_a_path_gain(jafwak):
    """Bench, 2026-11-20: the path gain failed and nothing showed what the 8591E had read."""
    res, _, _ = _run(_cfg(jafwak, sa_cal_margin_db=200.0))
    assert res.path_gain_db is None
    for row in res.rows:
        if row["frequency_hz"] >= 10e3:
            assert row["sa_vpeak_v"] > 0 and row["sa_floor_vpeak_v"] > 0
            assert row["sa_rbw_hz"] > 0 and abs(row["sa_peak_frequency_hz"] - row["frequency_hz"]) < 1e3
            assert math.isfinite(row["sa_dbm"]) and math.isfinite(row["sa_margin_db"])
        else:
            assert math.isnan(row["sa_vpeak_v"])
    issue = next(i for i in res.issues if "couldn't be measured" in i)
    assert "AD2" in issue and "8591E" in issue and "dB" in issue  # each overlap point's two margins


def test_overlap_captures_default_to_128(jafwak):
    assert jafwak.psrr.sa_cal_averages == 128
