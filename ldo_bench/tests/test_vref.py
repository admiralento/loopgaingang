"""External reference (--vref ext): config limits, Bench sequencing and proof, CLI, compare, driver."""

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ldo_bench.bench import Bench, open_bench
from ldo_bench.cli import app
from ldo_bench.config import BenchConfig
from ldo_bench.instruments.base import InstrumentError
from ldo_bench.instruments.e36xx import AgilentE36xx
from ldo_bench.instruments.sim import LdoModel, SimAnalogDiscovery, SimPowerSupply

CONFIGS = Path(__file__).parents[1] / "configs"
runner = CliRunner()


def with_vref(cfg, **vref) -> BenchConfig:
    data = cfg.model_dump()
    data["vref"] = {**data["vref"], **vref}
    return BenchConfig.model_validate(data)


def ext_bench(cfg, **kw):
    return open_bench(cfg, sim=True, vref_external=True, **kw)


# --- config -------------------------------------------------------------------
def test_vref_needs_the_other_psu_output(jafwak):
    with pytest.raises(ValueError, match="other output"):
        with_vref(jafwak, psu_channel=1)


def test_vref_v_max_within_vin_max(jafwak):
    with pytest.raises(ValueError, match=r"exceeds dut\.vin_max"):
        with_vref(jafwak, v_max=2.0, ovp=None)


def test_vref_too_far_above_a_tests_lowest_vin(jafwak):
    data = jafwak.model_dump()
    data["line_reg"]["vin_start"] = 0.8  # 1.204 V reference would sit 0.4 V above VIN
    with pytest.raises(ValueError, match="line-reg's lowest VIN"):
        BenchConfig.model_validate(data)


def test_vref_ovp_above_v_max(jafwak):
    with pytest.raises(ValueError, match=r"vref\.ovp"):
        with_vref(jafwak, ovp=1.3)


# --- Bench --------------------------------------------------------------------
def test_reference_comes_on_only_after_vin_and_sets_vout(jafwak):
    with ext_bench(jafwak) as b:
        m = b.sim_model
        b.confirm_wiring("t", [], ref_probe=("vin", 0))
        log = []
        orig = b.psu.set_voltage
        b.psu.set_voltage = lambda v: (log.append(("vin", v)), orig(v))[1]
        b.vin_from_psu(1.8)
        assert m.vref_out_v == 0.0  # held at 0 V while VIN comes up
        b.reference_on()
        assert m.vref_out_v == pytest.approx(1.204)
        assert b._vout_now() == pytest.approx(1.204, abs=2e-3)  # VOUT follows the external reference
        assert b.vref_proof["vout_moved_v"] == pytest.approx(0.02, abs=2e-3)
        assert log[-1] == ("vin", 1.8)  # VIN was finished before the reference started


def test_reference_refused_before_vin(jafwak):
    with ext_bench(jafwak) as b:
        b.confirm_wiring("t", [], ref_probe=("vin", 0))
        b.supply_off()  # supply (and the reference output) set up, VIN off
        with pytest.raises(InstrumentError, match="VIN up first"):
            b.reference_on()
        assert b.sim_model.vref_out_v == 0.0


def test_jp2_left_in_stops_the_run_and_zeroes_the_reference(jafwak):
    with ext_bench(jafwak) as b:
        b.confirm_wiring("t", [], ref_probe=("vin", 0))
        b.sim_model.jp2_fitted = True  # the operator didn't remove it
        b.vin_from_psu(1.8)
        with pytest.raises(InstrumentError, match="JP2 still"):
            b.reference_on()
        assert b.sim_model.vref_out_v == 0.0
        assert not b._vref_on


def test_vin_cannot_drop_below_the_reference_window(jafwak):
    with ext_bench(jafwak) as b:
        b.confirm_wiring("t", [], ref_probe=("vin", 0))
        b.vin_from_psu(1.8)
        b.reference_on()
        b.vin_from_psu(1.0)  # 0.204 V below the reference: allowed (above_vin 0.3)
        with pytest.raises(InstrumentError, match=r"refusing VIN = 0\.800"):
            b.vin_from_psu(0.8)
        with pytest.raises(InstrumentError, match="buffer_rail"):
            b.buffer_rail(4.0)


def test_supply_off_zeroes_the_reference_first(jafwak):
    with ext_bench(jafwak) as b:
        b.confirm_wiring("t", [], ref_probe=("vin", 0))
        b.vin_from_psu(1.8)
        b.reference_on()
        seen = []
        orig = b.psu.output
        b.psu.output = lambda on: (seen.append(b.sim_model.vref_out_v), orig(on))[1]
        b.supply_off()
        assert seen == [0.0]


def test_wavegen_off_on_vin_zeroes_the_reference_first(jafwak_single):
    with ext_bench(jafwak_single) as b:
        b.confirm_wiring("t", [], ref_probe=("vin", 0), psu_to="rail")
        b.buffer_rail(4.0)
        b.vin_from_wavegen(1.8)
        b.reference_on()
        seen = []
        orig = b.awg.off
        b.awg.off = lambda ch: (seen.append(b.sim_model.vref_out_v), orig(ch))[1]
        b.wavegen_off()
        assert seen == [0.0]


def test_shutdown_zeroes_the_reference_before_anything_else(jafwak):
    b = ext_bench(jafwak)
    b.confirm_wiring("t", [], ref_probe=("vin", 0))
    b.vin_from_psu(1.8)
    b.reference_on()
    order = []
    m = b.sim_model
    b.awg.off = lambda ch: order.append(("wavegen", m.vref_out_v))
    orig = b.psu.output
    b.psu.output = lambda on: (order.append(("psu", m.vref_out_v)), orig(on))[1]
    b.shutdown()
    assert order[0] == ("wavegen", 0.0)
    assert ("psu", 0.0) in order


def test_bgr_mode_never_touches_the_reference_output(jafwak):
    with open_bench(jafwak, sim=True) as b:
        b.confirm_wiring("t", [], ref_probe=("vin", 0))
        b.vin_from_psu(1.8)
        b.reference_on()  # no-op
        assert b.vref_out is None
        assert b.vref_info() == {"source": "bgr", "bgr_measured_v": 1.17}


def _hw_like_bench(cfg, confirm, assume_yes):
    """A Bench that isn't in sim mode (so it asks), on sim instruments."""
    m = LdoModel(cfg.dut.vout_nominal, cfg.loadbank.resistors, cfg.loadbank.dio_pins)
    return Bench(
        cfg,
        SimPowerSupply(m),
        SimAnalogDiscovery(m),
        assume_yes=assume_yes,
        confirm=confirm,
        vref_external=True,
    )


def test_jp2_question_is_asked_even_with_yes(jafwak):
    asked = []
    b = _hw_like_bench(jafwak, lambda q: asked.append(q) or True, assume_yes=True)
    b.confirm_wiring("t", ["x"], ref_probe=("vin", 0))
    assert len(asked) == 1 and "JP2 removed" in asked[0]


def test_jp2_not_confirmed_aborts(jafwak):
    b = _hw_like_bench(jafwak, lambda q: "JP2" not in q, assume_yes=False)
    with pytest.raises(KeyboardInterrupt):
        b.confirm_wiring("t", ["x"], ref_probe=("vin", 0))


def test_jp2_needs_someone_to_ask(jafwak):
    b = _hw_like_bench(jafwak, None, assume_yes=True)
    with pytest.raises(InstrumentError, match="confirm JP2"):
        b.confirm_wiring("t", ["x"], ref_probe=("vin", 0))


def test_open_bench_refuses_ext_without_vref_section(jafwak):
    data = jafwak.model_dump()
    data["vref"] = None
    with pytest.raises(InstrumentError, match=r"\[vref\]"):
        open_bench(BenchConfig.model_validate(data), sim=True, vref_external=True)


# --- every test that offers --vref ext ---------------------------------------
@pytest.mark.parametrize("cmd", ["smoke", "load-reg", "line-reg", "load-tran", "line-tran", "psrr", "iq"])
def test_each_test_proves_the_reference_and_runs_on_it(cmd, tmp_path, monkeypatch):
    seen = {}
    orig = Bench.reference_on

    def spy(self):
        orig(self)
        seen["vref"] = self.sim_model.reference
        seen["proof"] = self.vref_proof

    monkeypatch.setattr(Bench, "reference_on", spy)
    out = [] if cmd == "smoke" else ["-o", str(tmp_path / "results")]  # smoke saves nothing
    cfg = CONFIGS / "jafwak.toml"
    if cmd in ("line-tran", "psrr"):
        # With [buffer], U2's negative rail is on OUT2, so --vref ext is refused (test_buffer_split);
        # the external reference itself is still tested on the single-rail buffer.
        text = cfg.read_text()
        start = text.index("[buffer]")
        cfg = tmp_path / "jafwak_single.toml"
        cfg.write_text(text[:start] + text[text.index("\n[", start + 1) + 1 :])
    r = runner.invoke(app, [cmd, "-c", str(cfg), "--sim", "--vref", "ext", *out])
    assert r.exit_code == 0, r.output
    assert seen["vref"] == pytest.approx(1.204)
    assert seen["proof"]["vout_moved_v"] > 0.01


def test_ext_run_folder_and_summary_record_the_reference(tmp_path):
    r = runner.invoke(
        app,
        [
            "load-reg",
            "-c",
            str(CONFIGS / "jafwak.toml"),
            "--sim",
            "--save-sim",
            "--vref",
            "ext",
            "-o",
            str(tmp_path),
        ],
    )
    assert r.exit_code == 0, r.output
    (run,) = (tmp_path / "load-reg").glob("*_extref_SIM")
    vref = json.loads((run / "summary.json").read_text())["vref"]
    assert vref["source"] == "external" and vref["external_v"] == 1.204
    assert "proof" in vref
    assert json.loads((run / "meta.json").read_text())["vref"] == vref


@pytest.mark.parametrize(
    ("args", "msg"),
    [
        (["-c", str(CONFIGS / "demo_board.toml"), "--vref", "ext"], "[vref]"),
        (["--vref", "maybe"], "'bgr' or 'ext'"),
    ],
)
def test_vref_option_errors_exit_2(args, msg):
    base = ["-c", str(CONFIGS / "jafwak.toml")] if "-c" not in args else []
    r = runner.invoke(app, ["load-reg", *base, *args, "--sim"])
    assert r.exit_code == 2
    assert msg in r.output


# --- compare ------------------------------------------------------------------
def _run(tmp_path, cmd, *extra):
    r = runner.invoke(
        app, [cmd, "-c", str(CONFIGS / "jafwak.toml"), "--sim", "--save-sim", "-o", str(tmp_path), *extra]
    )
    assert r.exit_code in (0, 3), r.output
    return max((tmp_path / cmd).glob("2*"), key=lambda p: p.stat().st_mtime_ns)


def _compare(tmp_path, a, b):
    return runner.invoke(app, ["compare", str(a), str(b), "-o", str(tmp_path / "cmp")])


def test_compare_bgr_against_external(tmp_path):
    a = _run(tmp_path, "load-reg")
    b = _run(tmp_path, "load-reg", "--vref", "ext")
    r = _compare(tmp_path, a, b)
    assert r.exit_code == 0, r.output
    (out,) = (tmp_path / "cmp" / "compare").glob("*_compare_load-reg_SIM")
    s = json.loads((out / "summary.json").read_text())
    assert s["valid"] and s["runs"]["b"]["vref"]["source"] == "external"
    sa = json.loads((a / "summary.json").read_text())
    sb = json.loads((b / "summary.json").read_text())
    assert s["values"]["v_at_i_min"]["diff"] == pytest.approx(sb["v_at_i_min"] - sa["v_at_i_min"])
    assert s["values"]["v_at_i_min"]["diff"] == pytest.approx(0.004, abs=1e-3)  # 1.204 vs 1.200 V
    assert (out / "plot.png").exists() and (out / "compare.csv").exists()


def test_compare_refuses_different_tests(tmp_path):
    r = _compare(tmp_path, _run(tmp_path, "load-reg"), _run(tmp_path, "line-reg"))
    assert r.exit_code == 2
    assert "can't compare" in r.output


def test_compare_with_different_settings_is_invalid(tmp_path):
    a = _run(tmp_path, "load-reg")
    b = _run(tmp_path, "load-reg", "--vref", "ext", "--vin", "1.7")
    r = _compare(tmp_path, a, b)
    assert r.exit_code == 3
    assert "load_reg.vin: 1.8 -> 1.7" in r.output
    assert list((tmp_path / "cmp" / "compare").glob("*_INVALID"))


def test_compare_of_an_invalid_run_is_invalid(tmp_path, monkeypatch):
    from ldo_bench.procedures import load_reg

    a = _run(tmp_path, "load-reg")
    orig = load_reg.run
    monkeypatch.setattr(load_reg, "run", lambda b, lr: (r := orig(b, lr), r.issues.append("probe off"))[0])
    b = _run(tmp_path, "load-reg", "--vref", "ext")
    r = _compare(tmp_path, a, b)
    assert r.exit_code == 3
    assert "is invalid: probe off" in r.output


def test_compare_same_reference_is_noted(tmp_path):
    a = _run(tmp_path, "psrr")
    b = _run(tmp_path, "psrr")
    r = _compare(tmp_path, a, b)
    assert r.exit_code == 0, r.output
    assert "repeatability" in r.output


# --- driver -------------------------------------------------------------------
class _FakeTransport:
    name = "fake"

    def __init__(self, errors=()):
        self.sent, self.errors = [], list(errors)

    def write(self, cmd):
        self.sent.append(cmd)

    def query(self, cmd):
        self.sent.append(cmd)
        if cmd == "*IDN?":
            return "Agilent Technologies,E3648A,0,1.7-5.0-1.0"
        if cmd == "SYST:ERR?":
            return self.errors.pop(0) if self.errors else '+0,"No error"'
        return "+0.00000E+00"


def test_second_output_selects_itself_before_every_command():
    t = _FakeTransport()
    out = AgilentE36xx(t, channel=1).aux(2)
    out.setup(current_limit=0.005, ovp=1.5)
    out.set_voltage(1.204)
    out.measure_voltage()
    cmds = t.sent[t.sent.index("INST:SEL OUT2") :]
    assert cmds[:3] == ["INST:SEL OUT2", "VOLT 0", "CURR 0.0050"]
    assert "VOLT:PROT 1.500" in cmds
    for c in ("VOLT 1.2040", "MEAS:VOLT?"):
        assert cmds[cmds.index(c) - 1] == "INST:SEL OUT2"


def test_second_output_ovp_rejection_stops():
    t = _FakeTransport()
    out = AgilentE36xx(t, channel=1).aux(2)
    t.errors = ['+0,"No error"', '-222,"Data out of range"']  # CURR ok, then the OVP refused
    with pytest.raises(InstrumentError, match=r"vref\.ovp"):
        out.setup(current_limit=0.005, ovp=1.5)


def test_no_spare_output_for_the_main_channel():
    with pytest.raises(InstrumentError, match="no spare output"):
        AgilentE36xx(_FakeTransport(), channel=2).aux(2)
