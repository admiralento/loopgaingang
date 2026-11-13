"""psu-check: compare the PSU's setpoint and readback against the DMM.

Wire the DMM straight across the PSU output (nothing else connected) and the
PSU steps through `psu_check.points`. Useful as a quick sanity / calibration
check before a measurement session. It also shows how far off the PSU's own
readback is, which is why the regulation tests measure at the board instead.

run() steps, for each setpoint in `points`: PSU output to it (only psu.v_max
applies, no DUT), wait settle_time, read the PSU's voltage readback and the
mean of `readings` DMM readings. Two errors per point:
- output error = DMM - setpoint (how well the PSU programs its output)
- readback error = PSU readback - DMM (how well it measures it)
Each is compared with the model's spec from e36xx.ACCURACY (% of value + offset);
for a model with no entry there, the errors are recorded with no pass / fail.

Validity check: at setpoints of 0.2 V and up, a DMM reading more than 20 % from
the setpoint is far beyond any spec, so the DMM isn't on this output. That is a
setup issue (the run is invalid) but the sweep continues so the plot shows it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np

from ldo_bench import plotting
from ldo_bench import wiring as w
from ldo_bench.bench import Bench
from ldo_bench.config import PsuCheckConfig
from ldo_bench.instruments.base import InstrumentError
from ldo_bench.instruments.e36xx import ACCURACY, model_of
from ldo_bench.procedures import INVALID

log = logging.getLogger(__name__)

NAME = "psu-check"


def _limit(spec: tuple[float, float] | None, v: float) -> float:
    """Allowed error (V) for a (percent of value, offset in V) spec; NaN if there is no spec."""
    return float("nan") if spec is None else spec[0] / 100 * abs(v) + spec[1]


@dataclass
class PsuCheckResult:
    """One row per setpoint: set_v, dmm_v, readback_v, the two errors, their limits and ok flags (V)."""

    model: str | None  # PSU model from its *IDN?, None if not recognised
    channel: int  # PSU output channel tested
    rows: list[dict]
    issues: list[str] = field(default_factory=list)  # setup problems: non-empty = not valid

    @property
    def passed(self) -> bool | None:
        """True / False against the spec; None when there is no spec for this model."""
        if self.issues:
            return False
        if self.model not in ACCURACY:
            return None
        return all(r["program_ok"] and r["readback_ok"] for r in self.rows)

    def summary(self) -> dict:
        out = np.array([r["output_error_v"] for r in self.rows])
        rb = np.array([r["readback_error_v"] for r in self.rows])
        return {
            "model": self.model,
            "channel": self.channel,
            "points": len(self.rows),
            "valid": not self.issues,
            "issues": self.issues,
            "passed": self.passed,
            "max_abs_output_error_v": float(np.max(np.abs(out))),
            "max_abs_readback_error_v": float(np.max(np.abs(rb))),
            "accuracy_spec": ACCURACY.get(self.model or ""),
        }

    def report(self) -> str:
        lines = [
            f"PSU check: {self.model or 'unknown model'} OUT{self.channel} against the DMM",
            *([f"  {INVALID}"] if self.issues else []),
            f"  {'set V':>6} {'DMM V':>10} {'readback V':>11} {'output err':>11} {'readback err':>13}",
        ]
        for r in self.rows:
            flag = "" if r["program_ok"] and r["readback_ok"] else "  OUT OF SPEC"
            if self.model not in ACCURACY:
                flag = ""
            lines.append(
                f"  {r['set_v']:6.2f} {r['dmm_v']:10.6f} {r['readback_v']:11.5f} "
                f"{r['output_error_v'] * 1e3:8.3f} mV {r['readback_error_v'] * 1e3:10.3f} mV{flag}"
            )
        verdict = {True: "PASS: all points within spec", False: "FAIL: see OUT OF SPEC rows"}
        lines.append(f"  {verdict.get(self.passed, 'no accuracy spec for this model')}")
        lines.append("  output err = DMM - setpoint;  readback err = PSU readback - DMM")
        return "\n".join(lines)


def run(bench: Bench, pc: PsuCheckConfig) -> PsuCheckResult:
    """Step the PSU through pc.points and compare with the DMM (see the module docstring).

    pc: the [psu_check] section. Needs the DMM (InstrumentError without it).
    """
    if bench.dmm is None:
        raise InstrumentError("psu-check needs the DMM: enable [dmm] in the config (and don't pass --no-dmm)")
    ch = bench.cfg.psu.channel
    bench.confirm_wiring(
        "PSU check",
        [
            w.dmm(f"PSU OUT{ch} +", f"PSU OUT{ch} -", "straight on the terminals"),
            "Nothing else on the PSU output",
        ],
        ref_probe=("vin", 0),
        dmm_probe="psu",
    )
    model = model_of(bench.psu.idn)
    spec = ACCURACY.get(model or "", {})
    rows = []
    off_target: list[str] = []
    for k, v in enumerate(pc.points):
        bench.psu_output(v)
        bench.sleep(pc.settle_time)
        readback = bench.psu.measure_voltage()
        dmm = float(np.mean([bench.dmm.read_dcv() for _ in range(pc.readings)]))
        # near 0 V a 20 % window is only a few tens of mV, close to the PSU's offset spec: skip those.
        # Only the first such point is reported.
        if v >= 0.2 and abs(dmm - v) > 0.2 * v and not off_target:
            # Way beyond any spec: the DMM isn't on this output. Keep going so the plot shows it.
            off_target.append(
                f"DMM reads {dmm:.4f} V with OUT{ch} set to {v:.3f} V (PSU readback {readback:.4f} V): "
                f"the DMM isn't connected across OUT{ch}"
            )
            log.warning("psu-check: %s", off_target[0])
        out_err, rb_err = dmm - v, readback - dmm
        prog_lim, rb_lim = _limit(spec.get("programming"), v), _limit(spec.get("readback"), dmm)
        rows.append(
            {
                "set_v": v,
                "dmm_v": dmm,
                "readback_v": readback,
                "output_error_v": out_err,
                "readback_error_v": rb_err,
                "program_limit_v": prog_lim,
                "readback_limit_v": rb_lim,
                # `not >` so a NaN limit (no spec) counts as ok
                "program_ok": not abs(out_err) > prog_lim,
                "readback_ok": not abs(rb_err) > rb_lim,
            }
        )
        log.info("[%d/%d] set %.3f V  DMM %.6f V  readback %.5f V", k + 1, len(pc.points), v, dmm, readback)
    return PsuCheckResult(model, ch, rows, off_target)


def plot(res: PsuCheckResult, dut: str):
    """Output error (top) and readback error (bottom) in mV against setpoint, with the spec limits."""
    del dut  # the DUT isn't involved in this test
    v = np.array([r["set_v"] for r in res.rows])
    panels = [
        ("output_error_v", "program_limit_v", "Output: DMM minus setpoint", plotting.SERIES[0]),
        ("readback_error_v", "readback_limit_v", "Readback: PSU readback minus DMM", plotting.SERIES[1]),
    ]
    fig, axes = plotting.figure(2, 1, size=(7.0, 5.6), sharex=True)
    for ax, (key, lim_key, title, color) in zip(axes, panels, strict=True):
        err = np.array([r[key] for r in res.rows]) * 1e3
        lim = np.array([r[lim_key] for r in res.rows]) * 1e3
        if np.all(np.isfinite(lim)):
            for sign in (1, -1):
                ax.plot(v, sign * lim, color=plotting.INK_2, lw=1, ls=(0, (4, 3)), zorder=2)
            ax.text(v[-1], lim[-1], " spec", color=plotting.INK_2, fontsize=8, va="center")
        ax.axhline(0, color=plotting.AXIS, lw=0.8, zorder=1)
        ax.plot(v, err, color=color, marker="o", ms=4, mec=plotting.SURFACE, mew=1, zorder=3)
        ax.set_ylabel("Error (mV)")
        ax.set_title(title, fontsize=9.5, pad=6)
    axes[-1].set_xlabel("Setpoint (V)")
    verdict = {True: "within spec", False: "OUT OF SPEC", None: "no spec for this model"}[res.passed]
    if res.issues:
        verdict = "SETUP PROBLEM, not a valid measurement"
        plotting.issue_box(axes[0], res.issues)
    fig.suptitle(
        f"PSU check: {res.model or 'PSU'} OUT{res.channel} vs DMM  ·  {verdict}",
        x=0.01,
        ha="left",
        fontsize=11,
        fontweight="bold",
        color=plotting.CRITICAL if res.issues else plotting.INK,
    )
    return fig
