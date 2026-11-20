"""sa-path-cal on the simulator: J10 out, W2 (100 mV) on U4's input, the 8591E on J9, the LDO unpowered."""

import csv
import math

import pytest
from helpers import config_path, invoke

from ldo_bench.bench import U4_INPUT_MAX, U4_PROOF_MAX, open_bench
from ldo_bench.instruments.base import InstrumentError
from ldo_bench.procedures import sa_path_cal
from ldo_bench.procedures.sa_path_cal import PathGainCurve

SIM_GAIN_DB = 20 * math.log10(0.98 * 50 / 97)


def _open(jafwak):
    return open_bench(jafwak, sim=True, use_sa=True)


def _run(jafwak, setup=None):
    with _open(jafwak) as b:
        if setup:
            setup(b)
        res = sa_path_cal.run(b, jafwak.psrr)
    return res, b


def test_measures_the_path_over_psrrs_analyzer_range(jafwak):
    res, b = _run(jafwak)
    assert not res.issues, res.issues
    cal = res.curve
    assert cal[0].frequency == pytest.approx(jafwak.psrr.sa_from) and cal[-1].frequency == pytest.approx(
        jafwak.psrr.f_stop
    )
    assert all(p.gain_db == pytest.approx(SIM_GAIN_DB, abs=0.05) for p in cal)
    assert {p.amplitude for p in res.points if p.kind == "linearity"} == set(sa_path_cal.LINEARITY_AMPLITUDES)
    assert res.proof["vout_v"][1] < 0.1  # VOUT stayed put while W2 stepped: J10 out
    m = b.sim_model
    assert not m.psu_on and m.u4_channel not in m.awg_wave and m.awg_v.get(m.u4_channel, 0.0) == 0.0


def test_a_rolled_off_path_shows_in_the_curve(jafwak):
    res, _ = _run(jafwak, setup=lambda b: setattr(b.sim_model, "sa_path", lambda f: f / math.hypot(f, 100e3)))
    g = {round(p.frequency): p.gain_db for p in res.curve}
    assert g[30000] < SIM_GAIN_DB - 9  # -10.4 dB more at 30 kHz with a 100 kHz corner
    assert g[1000000] == pytest.approx(SIM_GAIN_DB, abs=0.1)


def test_j10_still_fitted_stops_before_w2_goes_past_the_proof_level(jafwak):
    levels: list[float] = []

    def j10_in(b):
        real_confirm, real_dc, real_wave = b.confirm_wiring, b.awg.set_dc, b.awg.set_wave

        def confirm(*a, **k):
            real_confirm(*a, **k)
            b.sim_model.j10_fitted = True  # the operator left J10 in

        def dc(ch, v):
            if ch == b.u4_channel:
                levels.append(v)
            real_dc(ch, v)

        def wave(ch, **kw):
            if ch == b.u4_channel:
                levels.append(kw["high"])
            real_wave(ch, **kw)

        b.confirm_wiring, b.awg.set_dc, b.awg.set_wave = confirm, dc, wave

    with pytest.raises(InstrumentError, match="J10 is still fitted"):
        _run(jafwak, setup=j10_in)
    assert levels and max(levels) <= U4_PROOF_MAX + 1e-9


def test_w2_limits(jafwak):
    with _open(jafwak) as b:
        with pytest.raises(InstrumentError, match="before J10 is proven"):
            b.u4_input_from_wavegen(0.7)
        b._j10_proven = True
        with pytest.raises(InstrumentError, match="limit"):
            b.u4_input_from_wavegen(1.3, 0.2, 1e3)  # 1.5 V > U4_INPUT_MAX
        with pytest.raises(InstrumentError, match="limit"):
            b.u4_input_from_wavegen(0.05, 0.1, 1e3)  # below 0 V
        b.u4_input_from_wavegen(1.2, U4_INPUT_MAX - 1.2, 1e3)


def test_vout_up_with_the_ldo_meant_off_stops(jafwak):
    def powered(b):
        real = b.scope.measure_dc

        def dc(duration, setups):
            r = real(duration, setups)
            ch = b.cfg.ad2.ref_channel
            if ch in r:
                r[ch] = type(r[ch])(1.2, r[ch].std, r[ch].n)  # VOUT at 1.2 V: something powers the chip
            return r

        b.scope.measure_dc = dc

    # caught by the rails' ramp check (it reads scope 2, here on VOUT) or by the check after it
    with pytest.raises(InstrumentError, match=r"unpowered|reads 1\.200 V"):
        _run(jafwak, setup=powered)


def test_shutdown_holds_u4s_input_at_0_v_before_the_rails_go(jafwak):
    events: list[tuple[str, float]] = []
    with pytest.raises(InstrumentError, match="boom"), _open(jafwak) as b:
        real_dc, real_out = b.awg.set_dc, b.psu.output

        def dc(ch, v):
            if ch == b.u4_channel:
                events.append(("w2", v))
            real_dc(ch, v)

        def out(on):
            events.append(("psu", on))
            real_out(on)

        b.awg.set_dc, b.psu.output = dc, out
        b.sa.sweep = lambda *a, **k: (_ for _ in ()).throw(InstrumentError("boom"))
        sa_path_cal.run(b, jafwak.psrr)
    last_psu_off = max(i for i, e in enumerate(events) if e == ("psu", False))
    w2_zero = max(i for i, e in enumerate(events) if e == ("w2", 0.0))
    assert w2_zero < last_psu_off


def test_nonlinear_analyzer_is_a_setup_issue(jafwak):
    def compress(b):  # the analyzer reads 1 dB high below -50 dBm (the 1 mV tone reaches it at -56 dBm)
        b.sa.t.scale_error_db = lambda dbm: 1.0 if dbm < -50 else 0.0

    res, _ = _run(jafwak, setup=compress)
    assert any("isn't linear" in i for i in res.issues), res.issues


def test_the_saved_curve_loads_and_interpolates(jafwak, tmp_path):
    res, _ = _run(jafwak)
    run = tmp_path / "sa-path-cal" / "20261120-150000_JAFWAK_sa-path-cal"
    run.mkdir(parents=True)
    with (run / "data.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(res.rows[0]))
        w.writeheader()
        w.writerows(res.rows)
    (tmp_path / "sa-path-cal" / "20261120-160000_JAFWAK_sa-path-cal_INVALID").mkdir()  # newer, but invalid
    curve = PathGainCurve.newest(tmp_path)
    assert curve.source == str(run)
    assert 20 * math.log10(curve.gain(50e3)) == pytest.approx(SIM_GAIN_DB, abs=0.05)
    with pytest.raises(InstrumentError, match="outside"):
        curve.gain(5e3)
    with pytest.raises(InstrumentError, match="invalid or simulated"):
        PathGainCurve.load(tmp_path / "sa-path-cal" / "20261120-160000_JAFWAK_sa-path-cal_INVALID")
    with pytest.raises(InstrumentError, match="no valid sa-path-cal"):
        PathGainCurve.newest(tmp_path / "nothing")


def test_cli_on_the_simulator():
    r = invoke("sa-path-cal", "-c", config_path("jafwak"), "--sim", "--yes")
    assert r.exit_code == 0, r.output
    assert "REMOVE J10" in r.output and "path gain" in r.output
    assert invoke("sa-path-cal", "-c", config_path("demo_board"), "--sim", "--yes").exit_code == 2
