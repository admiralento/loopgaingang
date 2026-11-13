import pytest

from ldo_bench.bench import open_bench
from ldo_bench.instruments.base import InstrumentError
from ldo_bench.procedures import iq


def test_vin_ceiling_enforced(jafwak_single):
    vmax = jafwak_single.dut.vin_max
    with open_bench(jafwak_single, sim=True) as b:
        with pytest.raises(InstrumentError, match=rf"limit {vmax}"):
            b.vin_from_psu(vmax + 0.1)
        b.buffer_rail(4.5)  # buffer rail may exceed the DUT's VIN limit
        with pytest.raises(InstrumentError):
            b.vin_from_wavegen(vmax + 0.1)


def test_shutdown_runs_on_exception(jafwak):
    b = open_bench(jafwak, sim=True)
    m = b.sim_model
    with pytest.raises(RuntimeError), b:
        b.vin_from_psu(1.8)
        b.loadbank.set(b.loadbank.max_allowed_code)
        b.vin_from_wavegen(1.5)
        raise RuntimeError("boom")
    assert not m.psu_on and m.psu_v == 0.0
    assert m.awg_v == {}
    assert m.conductance() == 0.0


def test_shutdown_continues_past_failures(jafwak):
    b = open_bench(jafwak, sim=True)
    b.vin_from_psu(1.8)
    b.awg.off = lambda ch: (_ for _ in ()).throw(InstrumentError("usb gone"))
    b.shutdown()
    assert not b.sim_model.psu_on


def test_dc_measurement_tracks_load(bench):
    bench.vin_from_psu(1.8)
    bench.loadbank.set(0)
    v0, ref = bench.measure_dc(0.1)
    bench.loadbank.set(bench.loadbank.max_allowed_code)
    v1, _ = bench.measure_dc(0.1)
    assert ref.mean == pytest.approx(1.8, abs=1e-3)
    assert v0.mean == pytest.approx(bench.cfg.dut.vout_nominal, abs=5e-3)
    assert v1.mean < v0.mean


def test_dut_current_limit_enforced_at_runtime(jafwak):
    with open_bench(jafwak, sim=True) as b:
        assert b.loadbank.allowed_codes() == [0, 1, 2, 3, 4]  # <= 20 mA at 1.2 V
        assert b.loadbank.max_allowed_code == 4
        with pytest.raises(InstrumentError, match=r"dut\.i_max"):
            b.loadbank.set(5)  # 24 mA
        with b.loadbank.unrestricted():  # loadbank-check: no DUT connected
            b.loadbank.set(31)
        with pytest.raises(InstrumentError):
            b.loadbank.set(31)  # limit back on afterwards


def test_dut_current_limit_enforced_in_config(jafwak):
    from ldo_bench.config import BenchConfig

    d = jafwak.model_dump()
    d["load_tran"]["code_high"] = 17
    with pytest.raises(ValueError, match=r"dut\.i_max"):
        BenchConfig.model_validate(d)


def test_vout_issue(jafwak):
    with open_bench(jafwak, sim=True) as b:
        assert b.vout_issue(1.19, source="scope 1") is None  # within 20 % of 1.2 V
        msg = b.vout_issue(-0.0004, source="scope 1")  # floating input, like the 15:09 run
        assert msg and "should be at 1.2 V" in msg


def test_sweep_points_end_on_stop_either_way():
    from ldo_bench.analysis import sweep_points

    assert list(sweep_points(1.4, 1.9, 0.15)) == pytest.approx([1.4, 1.55, 1.7, 1.85, 1.9])
    assert list(sweep_points(1.9, 1.4, 0.2)) == pytest.approx([1.9, 1.7, 1.5, 1.4])
    assert list(sweep_points(1.25, 1.35, 0.05)) == pytest.approx([1.25, 1.3, 1.35])  # no float-error drop


def test_iq_sweep_ends_on_vin_stop(jafwak):

    cfg = jafwak.iq.model_copy(update={"vin_step": 0.15})
    with open_bench(jafwak, sim=True) as b:
        res = iq.run(b, cfg)
    assert max(p.vin for p in res.points) == pytest.approx(cfg.vin_stop, abs=0.01)  # PSU readback


def test_wavegen_off_leaves_vin_undriven(jafwak_single):
    """After W1 stops driving VIN, nothing may treat VIN as up (the external reference needs it)."""
    with open_bench(jafwak_single, sim=True) as b:
        b.confirm_wiring("t", [], ref_probe=("vin", 0), psu_to="rail")
        b.vin_from_wavegen(1.8)
        b.wavegen_off()
        assert b._vin_level is None and b._vin_from is None


def test_buffer_current_limit_not_silently_ignored(jafwak):
    """VS_BUFF must get buffer.rail_current_limit: a PSU already set up with another limit is refused."""
    with open_bench(jafwak, sim=True) as b:
        b.supply_off()  # sets the PSU up with psu.current_limit
        b.confirm_wiring("t", [], ref_probe=("vin", 0), psu_to="rail")
        b.vin_from_wavegen(0.0)
        with pytest.raises(InstrumentError, match="current limit"):
            b.buffer_rails_on(1.9)


def test_unexpected_error_still_reports_a_failed_shutdown(monkeypatch):
    from pathlib import Path

    from typer.testing import CliRunner

    from ldo_bench.bench import Bench
    from ldo_bench.cli import app
    from ldo_bench.procedures import load_reg

    monkeypatch.setattr(load_reg, "run", lambda b, lr: (_ for _ in ()).throw(ValueError("a bug")))
    real = Bench.shutdown

    def failing_shutdown(self):
        real(self)
        self.shutdown_errors.append("PSU output off: no reply")

    monkeypatch.setattr(Bench, "shutdown", failing_shutdown)
    cfg = str(Path(__file__).parents[1] / "configs" / "jafwak.toml")
    r = CliRunner().invoke(app, ["load-reg", "-c", cfg, "--sim"])
    assert r.exit_code != 0 and "SHUTDOWN FAILED" in r.output


def test_vout_comes_from_the_dmm_when_there_is_one(jafwak):
    with open_bench(jafwak, sim=True) as b:
        read = b.dmm.read_dcv
        b.dmm.read_dcv = lambda: read() + 5e-3  # the DMM reads 5 mV above the AD2
        b.vin_from_psu(1.8)
        p = b.measure_point(0.05)
    assert p.source == "dmm" and p.vout - p.vout_ad2 == pytest.approx(5e-3, abs=1e-3)
