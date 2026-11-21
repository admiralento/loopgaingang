"""temp-curve: VREF against temperature from a temp log (rates, misreads, heating / cooling bins, a
parabola fit), and the merge of a cold and a hot run."""

import csv
import json

import numpy as np
import pytest
from helpers import config_path, invoke

from ldo_bench.config import load_config
from ldo_bench.procedures import temp_curve as tcur


@pytest.fixture
def tc(jafwak):
    return jafwak.temp


def parabola(T):
    """A bandgap-like VREF (V): peak 1.1975 V at 50 C, -1.3 uV/C^2."""
    return 1.1975 - 1.3e-6 * (np.asarray(T) - 50.0) ** 2


def rows_from(t, T, vref, vout=None):
    vout = vref if vout is None else vout
    return [
        {"t_s": float(a), "temp_c": float(b), "vref_v": float(c), "vout_v": float(d)}
        for a, b, c, d in zip(t, T, vref, vout, strict=True)
    ]


def sweep(lag_c=0.0, rate=0.8):
    """Up 20 -> 60 C and back down at `rate` C/min, every 3 s. The sensor reads `lag_c` behind the
    die (low while heating, high while cooling); VREF follows the die."""
    t_up = (60 - 20) / rate * 60
    t = np.arange(0, 2 * t_up, 3.0)
    die = np.where(t < t_up, 20 + rate * t / 60, 60 - rate * (t - t_up) / 60)
    sensor = np.where(t < t_up, die - lag_c, die + lag_c)
    return rows_from(t, sensor, parabola(die))


def test_rate_of_a_ramp():
    t = np.arange(0, 600, 3.0)
    r = tcur.rate_c_per_min(t, 20 + 0.5 * t / 60, window=60)
    assert r == pytest.approx(np.full_like(t, 0.5), abs=1e-6)


def test_heating_and_cooling_are_binned_apart_and_averaged(tc):
    res = tcur.analyse(sweep(lag_c=0.6), tc)
    dirs = {b["direction"] for b in res.bins}
    assert dirs == {"heating", "cooling"}
    # lag: at the same sensor reading, heating sees a hotter die than cooling, so their VREFs differ;
    # their average cancels most of it
    mid = [c for c in res.curve if c["from"] == "heating+cooling" and 25 <= c["bin_c"] <= 45]
    assert mid
    for c in mid:
        assert c["vref_v"] == pytest.approx(parabola(c["temp_c"]), abs=5e-6)
    h = {b["bin_c"]: b["vref_v"] for b in res.bins if b["direction"] == "heating"}
    k = {b["bin_c"]: b["vref_v"] for b in res.bins if b["direction"] == "cooling"}
    assert abs(h[30.0] - k[30.0]) > 20e-6  # the lag is visible before averaging


def test_fit_recovers_the_parabola(tc):
    res = tcur.analyse(sweep(), tc)
    f = res.fit
    assert f["peak_c"] == pytest.approx(50.0, abs=0.5)
    assert f["peak_v"] == pytest.approx(1.1975, abs=5e-6)
    assert f["curvature_ppm_per_c2"] == pytest.approx(-1.3e-6 / 1.1975 * 1e6, rel=0.05)
    assert f["peak_in_range"] is True
    assert f["residual_rms_v"] < 5e-6
    # box method over the measured span: (max - min) / (mean * span)
    assert f["box_tc_ppm_per_c"] > 0


def test_fast_samples_stay_out_of_the_curve(tc):
    res = tcur.analyse(sweep(rate=3.0), tc)  # 3 C/min, above max_rate (1 C/min)
    # only the turn-over at 60 C is slow enough; none of the ramp itself
    assert res.curve and all(c["bin_c"] >= 59 for c in res.curve)
    assert all(not r["in_curve"] for r in res.rows if r["temp_c"] < 58)
    assert any("max_rate" in n for n in res.notes)


def test_misread_is_left_out_and_listed(tc):
    rows = sweep()
    rows[100]["vref_v"] = 1.457  # the DMM misread; VOUT (AD2) still at the right level
    res = tcur.analyse(rows, tc)
    assert res.misreads == [rows[100]["t_s"]]
    assert res.rows[100]["misread"] and not res.rows[100]["in_curve"]
    assert not res.issues  # 1 of ~2000 samples: under misread_max_fraction (1 %)
    assert all(abs(c["vref_v"] - parabola(c["temp_c"])) < 1e-4 for c in res.curve)


def test_many_misreads_make_an_issue(tc):
    rows = sweep()
    for k in range(0, len(rows), 20):  # 5 %
        rows[k]["vref_v"] = 1.0
    res = tcur.analyse(rows, tc)
    assert any("misread" in i for i in res.issues)


def test_a_real_vref_drop_is_not_a_misread(tc):
    """VREF and VOUT moving together is the reference itself (2026-11-21): kept, not hidden."""
    rows = sweep()
    for r in rows[50:60]:
        r["vref_v"] -= 0.1
        r["vout_v"] -= 0.1
    res = tcur.analyse(rows, tc)
    assert not res.misreads
    assert any(b["vref_std_v"] > 0.01 for b in res.bins)  # it shows up as a wide bin
    assert any("spread" in n for n in res.notes)


def test_excluded_time_ranges(tc):
    rows = sweep()
    res = tcur.analyse(rows, tc, exclude=[(0.0, 300.0)])
    assert all(not r["in_curve"] for r in res.rows if r["t_s"] <= 300)
    assert any("excluded" in n and "0-300 s" in n for n in res.notes)


def test_merge_reports_the_overlap(tc):
    cold = tcur.analyse(rows_from(*_cold()), tc)
    hot = tcur.analyse(sweep(), tc)
    m = tcur.merge([("cold", cold), ("hot", hot)])
    assert min(c["bin_c"] for c in m.curve) < 10 and max(c["bin_c"] for c in m.curve) > 55
    (ov,) = m.overlaps
    assert ov["runs"] == ["cold", "hot"] and ov["bins"] > 0
    assert ov["mean_diff_mv"] == pytest.approx(0.05, abs=0.005)  # the cold run sits 50 uV high
    assert m.fit["peak_c"] == pytest.approx(50.0, abs=1.0)


def _cold():
    """5 -> 25 C at 0.5 C/min, VREF 50 uV above the hot run's curve (a setup offset)."""
    t = np.arange(0, 40 * 60, 3.0)
    T = 5 + 0.5 * t / 60
    return t, T, parabola(T) + 50e-6


def _saved_run(tmp_path, name="run1"):
    """A temp run folder as the bench saves it: data.csv, summary.json, meta.json."""
    d = tmp_path / "results" / "temp" / name
    d.mkdir(parents=True)
    rows = sweep()
    with (d / "data.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    cfg = load_config(config_path()).model_dump()
    (d / "summary.json").write_text(json.dumps({"valid": True}))
    (d / "meta.json").write_text(json.dumps({"test": "temp", "sim": True, "config": cfg}))
    return d


def test_temp_curve_cli_writes_a_new_folder(tmp_path):
    src = _saved_run(tmp_path)
    before = {p.name: p.read_bytes() for p in src.iterdir()}
    out = tmp_path / "results"
    r = invoke("temp-curve", str(src), "--out", str(out), "--exclude", "1:0-60")
    assert r.exit_code == 0, r.output
    (new,) = [p for p in (out / "temp-curve").iterdir() if p.is_dir()]
    assert new.name.endswith("_temp-curve_SIM")
    assert {"curve.csv", "bins.csv", "summary.json", "meta.json", "plot.png"} <= {
        p.name for p in new.iterdir()
    }
    assert {p.name: p.read_bytes() for p in src.iterdir()} == before  # the source run is untouched
    s = json.loads((new / "summary.json").read_text())
    assert s["fit"]["peak_c"] == pytest.approx(50.0, abs=1.0)
    assert s["runs"][0]["exclude_s"] == [[0.0, 60.0]]


def test_temp_curve_cli_rejects_a_bad_exclude(tmp_path):
    src = _saved_run(tmp_path)
    r = invoke("temp-curve", str(src), "--out", str(tmp_path / "results"), "--exclude", "2:0-60")
    assert r.exit_code == 2
