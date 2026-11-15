"""psu-sweep: step the supply from start to stop (and back with updown), reading its V and I at each step.

The supply comes up the way every run does it (current limit and OVP first, output on at 0 V), then
steps through PsuSweepConfig.setpoints(). At each setpoint it waits `dwell` seconds and reads the
supply's own voltage and current. Useful for an I-V curve of whatever is on the supply, e.g. a
board's input current vs VIN.

The ceiling and the current limit are as in psu-hold (see psu_hold.py): 'vin' goes through
bench.vin_from_psu (min(dut.vin_max, psu.v_max)), 'bench' through bench.psu_output (psu.v_max), and
the limit is psu.current_limit or a lower `current_limit`.

Ctrl-C stops the sweep; the points so far are kept and saved (summary.json: complete = false) and
the Bench shutdown still runs. The checks are psu-hold's: a reading near the current limit is
flagged, not a problem; a readback off the setpoint with the current not near the limit is a setup
problem (the run is saved _INVALID).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np

from ldo_bench import plotting
from ldo_bench.bench import Bench
from ldo_bench.config import PsuSweepConfig
from ldo_bench.procedures import INVALID
from ldo_bench.procedures.psu_hold import apply, off_setpoint_issue, read, wiring

log = logging.getLogger(__name__)

NAME = "psu-sweep"


@dataclass
class SweepResult:
    """One row per setpoint: index, direction ('up' / 'down'), set_v, v_psu, i_psu, near_limit, off_setpoint."""

    limit_a: float  # A, the current limit used
    to: str
    planned: int  # setpoints in the full sweep
    rows: list[dict]
    issues: list[str] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        """True if every planned setpoint was measured (False after Ctrl-C)."""
        return len(self.rows) == self.planned

    def summary(self) -> dict:
        i = [r["i_psu"] for r in self.rows]
        return {
            "valid": not self.issues,
            "issues": self.issues,
            "to": self.to,
            "current_limit_a": self.limit_a,
            "points": len(self.rows),
            "planned_points": self.planned,
            "complete": self.complete,
            "near_limit_points": sum(r["near_limit"] for r in self.rows),
            "i_max_a": max(i) if i else None,
        }

    def report(self) -> str:
        lines = [
            f"PSU sweep on {'VIN' if self.to == 'vin' else 'the bench'}, current limit {self.limit_a * 1e3:g} mA",
            *([f"  {INVALID}"] if self.issues else []),
        ]
        if not self.complete:
            lines.append(f"  stopped with Ctrl-C after {len(self.rows)} of {self.planned} setpoints")
        lines.append(f"  {'dir':>4} {'set V':>7} {'V psu':>8} {'I psu mA':>9}")
        for r in self.rows:
            flag = "  NEAR LIMIT" if r["near_limit"] else ("  OFF SETPOINT" if r["off_setpoint"] else "")
            lines.append(
                f"  {r['direction']:>4} {r['set_v']:7.3f} {r['v_psu']:8.4f} {r['i_psu'] * 1e3:9.3f}{flag}"
            )
        if near := sum(r["near_limit"] for r in self.rows):
            lines.append(
                f"  {near} points near the current limit: the supply may have been in constant current"
            )
        return "\n".join(lines)


def run(bench: Bench, sc: PsuSweepConfig) -> SweepResult:
    """Step through sc.setpoints() (see the module docstring); Ctrl-C keeps the points so far."""
    points = sc.setpoints()
    bench.confirm_wiring(
        f"PSU sweep {sc.start:g} -> {sc.stop:g} V{' and back' if sc.updown else ''}",
        wiring(bench, sc),
        ref_probe=("vin", 0),
    )
    limit = bench.set_psu_limit(amps=sc.current_limit)
    peak = int(np.argmax(points)) if sc.stop > sc.start else int(np.argmin(points))
    rows: list[dict] = []
    try:
        for k, v in enumerate(points):
            apply(bench, sc, v)
            bench.sleep(sc.dwell)
            rising = (k <= peak) == (sc.stop > sc.start)
            row = {"index": k, "direction": "up" if rising else "down", **read(bench, sc, v, limit)}
            rows.append(row)
            log.info(
                "[%d/%d] set %.3f V  %.4f V  %.3f mA%s",
                k + 1,
                len(points),
                v,
                row["v_psu"],
                row["i_psu"] * 1e3,
                "  NEAR LIMIT" if row["near_limit"] else "",
            )
    except KeyboardInterrupt:
        log.warning("psu-sweep: stopped by Ctrl-C after %d of %d points", len(rows), len(points))
    issues = off_setpoint_issue(rows, sc, bench.cfg.psu.channel)
    return SweepResult(limit, sc.to, len(points), rows, issues)


def plot(res: SweepResult, dut: str):
    """Current vs setpoint (top) and readback minus setpoint (bottom), up and down sweeps apart."""
    fig, axes = plotting.figure(2, 1, size=(7.0, 5.6), sharex=True)
    for n, direction in enumerate(("up", "down")):
        rows = [r for r in res.rows if r["direction"] == direction]
        if not rows:
            continue
        v = np.array([r["set_v"] for r in rows])
        i = np.array([r["i_psu"] for r in rows]) * 1e3
        err = np.array([r["v_psu"] - r["set_v"] for r in rows]) * 1e3
        style = {"color": plotting.SERIES[n], "marker": "o", "ms": 3.5, "lw": 1.4, "label": direction}
        axes[0].plot(v, i, **style)
        axes[1].plot(v, err, **style)
        near = [k for k, r in enumerate(rows) if r["near_limit"]]
        if near:
            axes[0].plot(v[near], i[near], ls="none", marker="o", ms=8, mfc="none", mec=plotting.INK_2)
    axes[0].axhline(res.limit_a * 1e3, color=plotting.INK_2, lw=1, ls=(0, (4, 3)))
    axes[0].text(
        axes[0].get_xlim()[0],
        res.limit_a * 1e3,
        " current limit",
        va="bottom",
        fontsize=8,
        color=plotting.INK_2,
    )
    axes[0].set_ylabel("I readback (mA)")
    axes[1].axhline(0, color=plotting.AXIS, lw=0.8)
    axes[1].set_ylabel("V readback - setpoint (mV)")
    axes[1].set_xlabel("Setpoint (V)")
    if any(r["direction"] == "down" for r in res.rows):
        axes[0].legend(frameon=False, fontsize=8)
    if res.issues:
        plotting.issue_box(axes[0], res.issues)
    where = "VIN" if res.to == "vin" else "bench"
    status = "" if res.complete else "  ·  stopped early (Ctrl-C)"
    plotting.suptitle(fig, f"{dut}: PSU sweep on {where}{status}", invalid=bool(res.issues))
    return fig
