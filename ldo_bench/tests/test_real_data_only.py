"""Everything saved is what was measured; setup problems are flagged, not hidden or fixed up.

Simulated runs save nothing unless --save-sim. Runs with setup problems finish, save the real
data under an _INVALID folder, list the problems on the plot and exit with code 3.
"""

from pathlib import Path

import pytest
from typer.testing import CliRunner

from ldo_bench.bench import open_bench
from ldo_bench.cli import app
from ldo_bench.instruments.base import DcReading
from ldo_bench.procedures import line_tran, load_reg, load_tran

CONFIGS = Path(__file__).parents[1] / "configs"
runner = CliRunner()


def runs(test_dir: Path) -> list[Path]:
    """Run folders in results/<test>/ (that folder also holds latest.png / latest_full.png)."""
    return [p for p in test_dir.iterdir() if p.is_dir()]


@pytest.mark.parametrize(
    "cmd", ["load-reg", "line-reg", "load-tran", "line-tran", "psu-check", "loadbank-check"]
)
def test_sim_saves_nothing_by_default(cmd, tmp_path):
    cfg = "jafwak.toml" if cmd == "line-tran" else "demo_board.toml"
    r = runner.invoke(app, [cmd, "-c", str(CONFIGS / cfg), "--sim", "-o", str(tmp_path)])
    assert r.exit_code == 0, r.output
    assert "SIMULATED" in r.output and "nothing saved" in r.output
    assert list(tmp_path.iterdir()) == []


def test_save_sim_marks_the_folder(tmp_path):
    r = runner.invoke(
        app, ["load-tran", "-c", str(CONFIGS / "demo_board.toml"), "--sim", "--save-sim", "-o", str(tmp_path)]
    )
    assert r.exit_code == 0, r.output
    (run,) = runs(tmp_path / "load-tran")
    assert run.name.endswith("_SIM") and (run / "plot.png").exists()


def test_setup_problem_saved_as_invalid(tmp_path, monkeypatch):
    from ldo_bench.instruments import sim

    monkeypatch.setattr(
        sim.SimAnalogDiscovery, "measure_dc", lambda self, d, s: {c: DcReading(0.0, 1e-4, 8192) for c in s}
    )
    r = runner.invoke(
        app, ["load-tran", "-c", str(CONFIGS / "demo_board.toml"), "--sim", "--save-sim", "-o", str(tmp_path)]
    )
    assert r.exit_code == 3 and "NOT a valid measurement" in r.output
    (run,) = runs(tmp_path / "load-tran")
    assert run.name.endswith("_SIM_INVALID") and (run / "plot.png").exists()
    import json

    assert json.loads((run / "summary.json").read_text())["valid"] is False


def test_load_tran_trigger_on_wrong_signal(cfg, monkeypatch):
    lt = cfg.load_tran.model_copy(update={"averages": 2})
    with open_bench(cfg, sim=True) as b:
        real = b.scope.capture

        def small_ref(**k):
            c = real(**k)
            c.ch2[:] = c.ch2 * 0.2  # 0.66 V swing: not the 3.3 V DIO pin
            return c

        monkeypatch.setattr(b.scope, "capture", small_ref)
        res = load_tran.run(b, lt, cfg.analysis)
    assert any("wrong signal" in i for i in res.issues)


def test_line_tran_vin_swing_must_match(jafwak, monkeypatch):
    lt = jafwak.line_tran.model_copy(update={"averages": 2})
    with open_bench(jafwak, sim=True) as b:
        real = b.scope.capture

        def big_ref(**k):
            c = real(**k)
            c.ch2[:] = (c.ch2 - c.ch2.mean()) * 3 + c.ch2.mean()  # 0.6 V swing, commanded 0.2 V
            return c

        monkeypatch.setattr(b.scope, "capture", big_ref)
        res = line_tran.run(b, lt, jafwak.analysis)
    assert any("wrong signal" in i for i in res.issues)


def test_load_reg_flags_probe_coming_loose(cfg, monkeypatch):
    with open_bench(cfg, sim=True, use_dmm=False) as b:
        real, n = b.scope.measure_dc, {"calls": 0}

        def loose(duration, setups):
            n["calls"] += 1
            r = real(duration, setups)
            if n["calls"] > 3:  # probe falls off after a few points
                r[cfg.ad2.vout_channel] = DcReading(0.0, 1e-4, 8192)
            return r

        monkeypatch.setattr(b.scope, "measure_dc", loose)
        res = load_reg.run(b, cfg.load_reg)
    assert any("out of range at" in i for i in res.issues)
    assert len(res.rows) == len(b.loadbank.allowed_codes())  # every point measured and kept
    load_reg.plot(res, "x")


def test_full_capture_plot_and_window(tmp_path):
    import json

    r = runner.invoke(
        app,
        [
            "load-tran",
            "-c",
            str(CONFIGS / "demo_board.toml"),
            "--sim",
            "--save-sim",
            "--window-ms",
            "5",
            "-o",
            str(tmp_path),
        ],
    )
    assert r.exit_code in (0, 3), r.output  # sim's DC shift may trip the 'pre' settle note, not validity
    (run,) = runs(tmp_path / "load-tran")
    assert (run / "plot.png").exists() and (run / "plot_full.png").exists()
    cfg = json.loads((run / "meta.json").read_text())["config"]["load_tran"]
    assert cfg["n_samples"] / cfg["sample_rate"] == pytest.approx(5e-3)  # 5 ms window


def test_latest_images_follow_the_newest_run(tmp_path):
    base = ["-c", str(CONFIGS / "demo_board.toml"), "--sim", "--save-sim", "-o", str(tmp_path)]
    assert runner.invoke(app, ["load-tran", *base]).exit_code in (0, 3)
    tran_latest = (tmp_path / "latest.png").read_bytes()
    (tran_run,) = runs(tmp_path / "load-tran")
    assert tran_latest == (tran_run / "plot.png").read_bytes()
    assert (tmp_path / "load-tran" / "latest_full.png").exists()

    assert runner.invoke(app, ["load-reg", *base]).exit_code in (0, 3)
    (reg_run,) = runs(tmp_path / "load-reg")
    assert (tmp_path / "latest.png").read_bytes() == (reg_run / "plot.png").read_bytes()  # newest test wins
    assert (tmp_path / "load-tran" / "latest.png").read_bytes() == tran_latest  # per-test copy kept
    assert (tmp_path / "latest_run.txt").read_text().strip() == str(reg_run)
    assert not list(tmp_path.rglob(".*.tmp"))  # no temp files left behind
