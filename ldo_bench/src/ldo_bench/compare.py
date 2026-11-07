"""Compare two saved runs of the same test, e.g. the BGR run against the --vref ext run.

`ldo-bench compare RUN_A RUN_B` reads each run folder's summary.json, meta.json and data files (it
touches no instrument) and writes a new folder under results/compare/:

    compare.csv   every number in both summaries: A, B and B - A
    summary.json  the same, plus which reference each run used and any problems
    plot.png      both runs overlaid (see plot() for what each test shows)

Nothing is recomputed: the numbers are the runs' own. The comparison is only as good as the runs,
so it is marked invalid (an `_INVALID` folder, exit 3) if:
  - either run was invalid
  - the runs differ in anything but the reference: the test's config section, [dut] or
    [loadbank], or a text field of the summary such as where VOUT was measured
A run of a different test or board is refused outright (exit 2).
"""

from __future__ import annotations

import csv
import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from ldo_bench import plotting

NAME = "compare"
SUPPORTED = ("load-reg", "line-reg", "load-tran", "line-tran", "psrr", "iq")
# Summary keys that describe the run, not a result: compared as settings, not listed as numbers.
SKIP_KEYS = {"valid", "issues", "notes", "vref", "xcheck"}
# Config sections that must match (besides the test's own) for the comparison to mean anything.
SAME_SECTIONS = ("dut", "loadbank")


@dataclass
class Run:
    """One saved run folder, as read back."""

    path: Path
    test: str
    dut: str
    sim: bool
    summary: dict[str, Any]
    config: dict[str, Any]

    @property
    def valid(self) -> bool:
        return bool(self.summary.get("valid", True))

    @property
    def vref(self) -> dict[str, Any]:
        """summary['vref'], or 'bgr' for a run saved before the external reference existed."""
        return self.summary.get("vref") or {"source": "bgr"}

    @property
    def label(self) -> str:
        """Legend label: which reference VOUT followed."""
        v = self.vref
        if v.get("source") == "external":
            return f"External VREF {v.get('external_v', float('nan')):.3f} V"
        bgr = v.get("bgr_measured_v")
        return f"BGR (measured {bgr:.3f} V)" if bgr else "BGR"

    def csv(self, name: str) -> list[dict[str, str]]:
        """Rows of one of the run's CSV files (empty if it has none)."""
        p = self.path / name
        if not p.exists() or p.stat().st_size == 0:
            return []
        with p.open(newline="") as f:
            return list(csv.DictReader(f))


def load_run(path: Path) -> Run:
    """Read a run folder. Raises ValueError if it isn't one (no summary.json / meta.json)."""
    path = Path(path)
    try:
        summary = json.loads((path / "summary.json").read_text())
        meta = json.loads((path / "meta.json").read_text())
    except FileNotFoundError as e:
        raise ValueError(f"{path} is not a run folder (no {Path(e.filename).name})") from e
    except json.JSONDecodeError as e:
        raise ValueError(f"{path}: {e}") from e
    return Run(
        path,
        meta["test"],
        meta["config"]["dut"]["name"],
        bool(meta.get("sim")),
        summary,
        meta["config"],
    )


def flatten(d: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    """{'edges': {'rising': {'peak': 1.2}}} -> {'edges.rising.peak': 1.2}. Lists are left out."""
    out: dict[str, Any] = {}
    for k, v in d.items():
        if not prefix and k in SKIP_KEYS:
            continue
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            out |= flatten(v, f"{key}.")
        elif not isinstance(v, list):
            out[key] = v
    return out


def _number(v: Any) -> float | None:
    """v as a float if it's a number (not a bool), else None. NaN stays NaN."""
    if isinstance(v, bool) or not isinstance(v, int | float):
        return None
    return float(v)


def _differences(a: Any, b: Any, path: str) -> list[str]:
    """Where two config values differ, as 'path: a -> b' lines."""
    if isinstance(a, dict) and isinstance(b, dict):
        out = []
        for k in sorted(set(a) | set(b)):
            out += _differences(a.get(k), b.get(k), f"{path}.{k}")
        return out
    return [] if a == b else [f"{path}: {a!r} -> {b!r}"]


@dataclass
class CompareResult:
    """Two runs side by side. rows: key, a, b, diff (b - a) for every number in both summaries."""

    a: Run
    b: Run
    rows: list[dict[str, Any]]
    issues: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def test(self) -> str:
        return self.a.test

    def summary(self) -> dict[str, Any]:
        return {
            "valid": not self.issues,
            "issues": self.issues,
            "notes": self.notes,
            "test": self.test,
            "runs": {
                "a": {"path": str(self.a.path), "vref": self.a.vref, "valid": self.a.valid},
                "b": {"path": str(self.b.path), "vref": self.b.vref, "valid": self.b.valid},
            },
            "values": {r["key"]: {"a": r["a"], "b": r["b"], "diff": r["diff"]} for r in self.rows},
        }

    def report(self) -> str:
        w = max([len(r["key"]) for r in self.rows] + [8])
        lines = [
            f"{self.test} on {self.a.dut}:",
            f"  A  {self.a.label:<26} {self.a.path.name}",
            f"  B  {self.b.label:<26} {self.b.path.name}",
            "",
            f"  {'':<{w}} {'A':>14} {'B':>14} {'B - A':>14}",
        ]
        for r in self.rows:
            lines.append(f"  {r['key']:<{w}} {_fmt(r['a']):>14} {_fmt(r['b']):>14} {_fmt(r['diff']):>14}")
        lines += [f"  note: {n}" for n in self.notes]
        return "\n".join(lines)


def _fmt(v: float) -> str:
    return "nan" if v is None or math.isnan(v) else f"{v:.6g}"


def compare(a: Run, b: Run) -> CompareResult:
    """Line up two runs of the same test on the same board (ValueError otherwise)."""
    if a.test != b.test:
        raise ValueError(f"can't compare a {a.test} run with a {b.test} run")
    if a.test not in SUPPORTED:
        raise ValueError(f"compare supports {', '.join(SUPPORTED)}, not {a.test}")
    if a.dut != b.dut:
        raise ValueError(f"the runs are of different boards ({a.dut} and {b.dut})")
    issues = []
    for run, name in ((a, "A"), (b, "B")):
        if not run.valid:
            issues.append(
                f"run {name} ({run.path.name}) is invalid: {'; '.join(run.summary.get('issues', []))}"
            )
    section = a.test.replace("-", "_")
    diffs = []
    for s in (section, *SAME_SECTIONS):
        diffs += _differences(a.config.get(s), b.config.get(s), s)
    fa, fb = flatten(a.summary), flatten(b.summary)
    for k in sorted(set(fa) & set(fb)):
        if isinstance(fa[k], str) and fa[k] != fb[k]:
            diffs.append(f"summary {k}: {fa[k]!r} -> {fb[k]!r}")
    if diffs:
        issues.append(
            "the runs differ in more than the reference, so the difference isn't the reference's: "
            + "; ".join(diffs)
        )
    notes = []
    if a.vref.get("source") == b.vref.get("source"):
        notes.append(
            f"both runs used the same reference ({a.label}): this compares repeatability, not references"
        )
    if a.sim or b.sim:
        notes.append("at least one run is simulated")
    rows = []
    for k in fa:  # summary order
        if k not in fb:
            continue
        va, vb = _number(fa[k]), _number(fb[k])
        if va is None or vb is None:
            continue
        rows.append({"key": k, "a": va, "b": vb, "diff": vb - va})
    return CompareResult(a, b, rows, issues, notes)


# --- Plot -------------------------------------------------------------------
def _col(rows: list[dict[str, str]], name: str) -> np.ndarray:
    return np.array([float(r[name]) if r.get(name) not in (None, "") else np.nan for r in rows])


def _dc(ax, run: Run, color: str, x: str, x_scale: float, ref: str, unit: str) -> None:
    """ΔVOUT (mV) against x, from the point given by ref ('min' / 'max' x)."""
    rows = run.csv("data.csv")
    if not rows:
        return
    xs, v = _col(rows, x) * x_scale, _col(rows, "vout_v")
    order = np.argsort(xs)
    xs, v = xs[order], v[order]
    v0 = v[0] if ref == "min" else v[-1]
    ax.plot(
        xs,
        (v - v0) * 1e3,
        color=color,
        marker="o",
        ms=4,
        mec=plotting.SURFACE,
        mew=1,
        label=f"{run.label}: {v0:.4f} V at {xs[0] if ref == 'min' else xs[-1]:.3g} {unit}",
    )


def _transient(axes, run: Run, color: str) -> None:
    """VOUT minus its pre-step level (mV) against time (µs), one axes per edge."""
    for ax, edge in zip(axes, ("rising", "falling"), strict=False):
        rows = run.csv(f"waveform_{edge}.csv")
        if not rows:
            continue
        t, v = _col(rows, "t_s") * 1e6, _col(rows, "vout_smoothed_v")
        base = np.nanmean(v[t < 0]) if np.any(t < 0) else v[0]
        ax.plot(t, (v - base) * 1e3, color=color, lw=1.2, label=f"{run.label} ({base:.4f} V before)")
        ax.set_title(f"{edge} edge", fontsize=9)


def plot(res: CompareResult, dut: str):
    """Both runs overlaid, in the test's natural axes.

    load-reg / line-reg: VOUT change (mV) from the lightest load / highest VIN, so the reference's DC
    offset doesn't hide the regulation (the absolute levels are in the legend and the table).
    load-tran / line-tran: each edge's smoothed VOUT minus its pre-step level. psrr: PSRR (dB) vs
    frequency. iq: current vs VIN, enabled and disabled.
    """
    t = res.test
    colors = (plotting.SERIES[0], plotting.SERIES[1])
    if t in ("load-tran", "line-tran", "iq"):
        fig, axes = plotting.figure(1, 2, size=(10.0, 4.2))
    else:
        fig, ax = plotting.figure()
        axes = [ax]
    for run, c in zip((res.a, res.b), colors, strict=True):
        if t == "load-reg":
            _dc(axes[0], run, c, "i_est_a", 1e3, "min", "mA")
        elif t == "line-reg":
            _dc(axes[0], run, c, "vin_v", 1.0, "max", "V")
        elif t == "psrr":
            rows = run.csv("data.csv")
            if rows:
                axes[0].semilogx(
                    _col(rows, "frequency_hz"),
                    _col(rows, "psrr_db"),
                    color=c,
                    marker="o",
                    ms=4,
                    mec=plotting.SURFACE,
                    mew=1,
                    label=run.label,
                )
        elif t == "iq":
            rows = run.csv("data.csv")
            for ax, state in zip(axes, ("enabled", "disabled"), strict=True):
                sel = [r for r in rows if r["state"] == state]
                if sel:
                    ax.plot(
                        _col(sel, "vin_v"),
                        _col(sel, "current_a") * 1e6,
                        color=c,
                        marker="o",
                        ms=4,
                        mec=plotting.SURFACE,
                        mew=1,
                        label=run.label,
                    )
                ax.set_title(state, fontsize=9)
        else:
            _transient(axes, run, c)
    labels = {
        "load-reg": ("Load current (mA)", "VOUT change from no load (mV)"),
        "line-reg": ("VIN (V)", "VOUT change from the highest VIN (mV)"),
        "psrr": ("Frequency (Hz)", "PSRR (dB)"),
        "iq": ("VIN (V)", "Supply current (µA)"),
        "load-tran": ("Time from the step (µs)", "VOUT change (mV)"),
        "line-tran": ("Time from the step (µs)", "VOUT change (mV)"),
    }[t]
    for ax in axes:
        ax.set_xlabel(labels[0])
        ax.set_ylabel(labels[1])
        ax.legend(loc="best")
    fig.suptitle(
        f"{t}: {dut}, {res.a.label} vs {res.b.label}", x=0.01, ha="left", fontsize=11, fontweight="bold"
    )
    if res.issues:
        plotting.issue_box(axes[0], res.issues)
    return fig
