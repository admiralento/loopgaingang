"""Run folders: raw data (CSV), metadata (JSON) and plots (PNG) for every run.

Layout (the root is --out, default `results`):

results/<test>/<stamp>_<dut>_<test>[_SIM][_INVALID]/
    e.g. results/load-reg/20261106-143012_JAFWAK_load-reg/
    data.csv      one row per measurement point
    *.csv         extra tables some tests write
    summary.json  headline numbers (valid = false for an _INVALID run)
    meta.json     config, instrument IDs, git commit, package versions
    plot.png      main plot; some tests add more plot*.png (e.g. plot_full.png)
results/latest.png, results/<test>/latest.png, results/<test>/latest_full.png, results/latest_run.txt
    copies of the newest run's plots (see RunDir.publish_latest)

<stamp> is the local start time YYYYmmdd-HHMMSS; <dut> is dut.name made filename-safe.
_SIM: made with --sim --save-sim. _INVALID: the test found a setup problem.
"""

from __future__ import annotations

import contextlib
import csv
import json
import os
import platform
import re
import shutil
import subprocess
from datetime import datetime
from importlib import metadata
from pathlib import Path
from typing import Any

from ldo_bench.config import BenchConfig

PACKAGES = ("ldo-bench", "numpy", "pyvisa", "pyvisa-py", "pydwf", "matplotlib")


def _slug(s: str) -> str:
    """`s` with anything but letters, digits, '.', '_' and '-' replaced by '-' ('dut' if empty)."""
    return re.sub(r"[^A-Za-z0-9._-]+", "-", s).strip("-") or "dut"


def _git_info() -> dict[str, Any]:
    """Commit of this package's checkout and whether it had uncommitted changes (None if no git)."""

    def git(*args: str) -> str | None:
        try:
            out = subprocess.run(
                ["git", *args], capture_output=True, text=True, timeout=5, cwd=Path(__file__).parent
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return out.stdout.strip() if out.returncode == 0 else None

    return {"commit": git("rev-parse", "HEAD"), "dirty": bool(git("status", "--porcelain"))}


def _versions() -> dict[str, str]:
    """Python, OS and installed versions of PACKAGES (missing ones are left out)."""
    out = {"python": platform.python_version(), "platform": platform.platform()}
    for p in PACKAGES:
        with contextlib.suppress(metadata.PackageNotFoundError):
            out[p] = metadata.version(p)
    return out


class RunDir:
    """One run's folder, created on construction (see the module docstring for the layout).

    root: the results folder; test: the test name (subfolder and suffix); cfg: the validated
    config (copied into meta.json); config_path: the TOML file it came from; simulated / invalid
    add _SIM / _INVALID to the folder name.
    """

    def __init__(
        self,
        root: Path,
        test: str,
        cfg: BenchConfig,
        *,
        config_path: Path | None = None,
        simulated: bool = False,
        invalid: bool = False,
    ) -> None:
        self.started = datetime.now()
        self.root = Path(root)
        self.test = test
        self.cfg = cfg
        self.config_path = config_path
        stamp = self.started.strftime("%Y%m%d-%H%M%S")
        # never mistake a simulated run, or one with setup problems, for a valid measurement
        suffix = ("_SIM" if simulated else "") + ("_INVALID" if invalid else "")
        # one folder per test: results/load-reg/..., results/line-tran/...
        self.path = Path(root) / test / f"{stamp}_{_slug(cfg.dut.name)}_{test}{suffix}"
        self.path.mkdir(parents=True, exist_ok=False)  # never overwrite an earlier run

    def file(self, name: str) -> Path:
        """Path of `name` inside the run folder."""
        return self.path / name

    def write_csv(self, name: str, rows: list[dict[str, Any]]) -> Path:
        """Write rows (dicts) as CSV; the first row's keys are the header. Empty rows -> empty file."""
        p = self.file(name)
        if not rows:
            p.write_text("")
            return p
        with p.open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
        return p

    def write_json(self, name: str, data: dict[str, Any]) -> Path:
        """Write `data` as indented JSON; anything not JSON-serialisable is written as str()."""
        p = self.file(name)
        p.write_text(json.dumps(data, indent=2, default=str) + "\n")
        return p

    def write_meta(self, *, idns: dict[str, str], sim: bool, extra: dict[str, Any] | None = None) -> Path:
        """Write meta.json: start / finish time, instrument IDs, full config, git commit, versions."""
        meta = {
            "test": self.test,
            "started": self.started.isoformat(timespec="seconds"),
            "finished": datetime.now().isoformat(timespec="seconds"),
            "sim": sim,
            "instruments": idns,
            "config_file": str(self.config_path) if self.config_path else None,
            "config": self.cfg.model_dump(),
            "git": _git_info(),
            "versions": _versions(),
            **(extra or {}),
        }
        return self.write_json("meta.json", meta)

    def save_fig(self, fig, name: str = "plot.png") -> Path:
        """Save a matplotlib figure into the run folder (150 dpi)."""
        p = self.file(name)
        fig.savefig(p, dpi=150)
        return p

    def publish_latest(self) -> list[Path]:
        """Copy this run's plots to fixed names, so an image left open in an editor shows the newest run.

        results/latest.png                    plot.png of the most recent run of any test
        results/<test>/latest.png             plot.png of the most recent run of this test
        results/<test>/latest_full.png        plot_full.png, for tests that make one
        results/latest_run.txt                which run folder those came from

        Copies, not symlinks (Windows needs admin rights for symlinks); each is written to a
        temporary name and then swapped in, so a viewer never reads a half-written image.
        """
        targets = {
            "plot.png": [self.root / "latest.png", self.path.parent / "latest.png"],
            "plot_full.png": [self.path.parent / "latest_full.png"],
        }
        written = []
        for src_name, dests in targets.items():
            src = self.file(src_name)
            if not src.exists():
                continue
            for dest in dests:
                tmp = dest.with_name(f".{dest.name}.tmp")
                shutil.copyfile(src, tmp)
                os.replace(tmp, dest)
                written.append(dest)
        note = self.root / "latest_run.txt"
        tmp = note.with_name(".latest_run.txt.tmp")
        tmp.write_text(f"{self.path}\n")
        os.replace(tmp, note)
        return written
