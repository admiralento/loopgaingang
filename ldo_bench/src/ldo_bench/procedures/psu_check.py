"""psu-check: compare the PSU's setpoint and readback against the DMM.

Wire the DMM straight across the PSU output (nothing else connected) and the
PSU steps through `psu_check.points`. Useful as a quick sanity / calibration
check before a measurement session. It also shows how far off the PSU's own
readback is, which is why the regulation tests measure at the board instead.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np

from ldo_bench import plotting
from ldo_bench.bench import Bench
from ldo_bench.config import PsuCheckConfig
from ldo_bench.instruments.base import InstrumentError
from ldo_bench.instruments.e36xx import ACCURACY, model_of

log = logging.getLogger(__name__)

NAME = "psu-check"


def _limit(spec: tuple[float, float] | None, v: float) -> float:
    return float("nan") if spec is None else spec[0] / 100 * abs(v) + spec[1]


@dataclass
class PsuCheckResult:
    model: str | None
    channel: int
    rows: list[dict]

    @property
    def passed(self) -> bool | None:
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
            "passed": self.passed,
            "max_abs_output_error_v": float(np.max(np.abs(out))),
            "max_abs_readback_error_v": float(np.max(np.abs(rb))),
            "accuracy_spec": ACCURACY.get(self.model or ""),
        }

    def report(self) -> str:
        lines = [
            f"PSU check: {self.model or 'unknown model'} OUT{self.channel} against the DMM",
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
    if bench.dmm is None:
        raise InstrumentError("psu-check needs the DMM: enable [dmm] in the config (and don't pass --no-dmm)")
    ch = bench.cfg.psu.channel
    bench.confirm_wiring(
        "PSU check",
        [
            f"DMM V+ / COM -> PSU OUT{ch} + / -, directly",
            "Nothing else connected to the PSU output",
        ],
        ref_probe=("vin", 0),
        dmm_probe="psu",
    )
    model = model_of(bench.psu.idn)
    spec = ACCURACY.get(model or "", {})
    rows = []
    for k, v in enumerate(pc.points):
        bench.psu_output(v)
        bench.sleep(pc.settle_time)
        readback = bench.psu.measure_voltage()
        dmm = float(np.mean([bench.dmm.read_dcv() for _ in range(pc.readings)]))
        if v >= 0.2 and abs(dmm - v) > 0.2 * v:
            # Way beyond any spec: the DMM isn't on this output. Stop rather than log fake failures.
            raise InstrumentError(
                f"DMM reads {dmm:.4f} V with OUT{ch} set to {v:.3f} V (PSU readback {readback:.4f} V): "
                f"the DMM isn't connected across OUT{ch}. Check the wiring and try again."
            )
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
                "program_ok": not abs(out_err) > prog_lim,
                "readback_ok": not abs(rb_err) > rb_lim,
            }
        )
        log.info("[%d/%d] set %.3f V  DMM %.6f V  readback %.5f V", k + 1, len(pc.points), v, dmm, readback)
    return PsuCheckResult(model, ch, rows)


def plot(res: PsuCheckResult, dut: str):
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
    fig.suptitle(
        f"PSU check: {res.model or 'PSU'} OUT{res.channel} vs DMM  ·  {verdict}",
        x=0.01,
        ha="left",
        fontsize=11,
        fontweight="bold",
        color=plotting.INK,
    )
    return fig
