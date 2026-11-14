"""psu-hold: hold the supply at one voltage, printing its V and I readback, until Ctrl-C.

The supply is set up the way every run does it (current limit and OVP first, output on at 0 V,
then a ramp to `voltage`), then held. Every `interval` seconds the supply's own voltage and current
readback are printed. Ctrl-C ends the hold; so does the hard timeout, `max_minutes` (at most 60),
so a forgotten hold can't stay on. Either way the Bench shutdown turns the output off.

Where the supply goes decides the ceiling (`to`): 'vin' (a board's VIN) goes through
bench.vin_from_psu, at most min(dut.vin_max, psu.v_max); 'bench' (nothing but a load or a meter)
through bench.psu_output, at most psu.v_max. The current limit is psu.current_limit, or a lower
`current_limit` (--ilim); the Bench refuses anything higher.

Nothing is saved: the readings are printed as they come and summarised at the end.

Checks on each reading (from the readback only; the supply's CV / CC mode isn't queried):
- current at or above `near_limit` x the limit: flagged "near the current limit", where the supply
  may be in constant current and holding less than the setpoint. Recorded, not a setup problem.
- voltage more than `v_tolerance` from the setpoint while the current isn't near the limit: a setup
  problem (the output isn't following: OVP tripped, output off from the front panel, wrong output).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Literal

import numpy as np

from ldo_bench import wiring as w
from ldo_bench.bench import Bench
from ldo_bench.config import PsuDriveConfig, PsuHoldConfig
from ldo_bench.procedures import INVALID

log = logging.getLogger(__name__)

NAME = "psu-hold"


def wiring(bench: Bench, dc: PsuDriveConfig) -> list[str]:
    """The supply on a board's VIN ('vin') or on a load / meter of your own ('bench')."""
    cfg = bench.cfg
    if dc.to == "vin":
        return w.psu_on_vin(cfg)
    return [*w.psu(cfg, "your load / meter +", "your load / meter -"), "Nothing from a board's VIN on it"]


def apply(bench: Bench, dc: PsuDriveConfig, volts: float) -> None:
    """Ramp the supply to `volts` (V) through the Bench method for where it goes (dc.to)."""
    if dc.to == "vin":
        bench.vin_from_psu(volts)
    else:
        bench.psu_output(volts)


def read(bench: Bench, dc: PsuDriveConfig, set_v: float, limit: float) -> dict:
    """One readback at setpoint `set_v` (V): v_psu (V), i_psu (A), near_limit, and off_setpoint (bool)."""
    v, i = bench.psu.measure_voltage(), bench.psu.measure_current()
    near = i >= dc.near_limit * limit
    return {
        "set_v": set_v,
        "v_psu": v,
        "i_psu": i,
        "near_limit": near,
        "off_setpoint": not near and abs(v - set_v) > dc.v_tolerance,
    }


def off_setpoint_issue(rows: list[dict], dc: PsuDriveConfig, ch: int) -> list[str]:
    """The setup problem for readings off the setpoint with the current not near the limit (or [])."""
    bad = [r for r in rows if r["off_setpoint"]]
    if not bad:
        return []
    r = bad[0]
    return [
        f"PSU OUT{ch} readback {r['v_psu']:.4f} V with {r['set_v']:.4f} V set and {r['i_psu'] * 1e3:.2f} mA "
        f"(not near the limit), more than {dc.v_tolerance * 1e3:g} mV off ({len(bad)} of {len(rows)} "
        "readings): is the output on (OVP tripped? front panel?) and is this the output the config names?"
    ]


@dataclass
class HoldResult:
    """Every readback (rows: t_s, set_v, v_psu, i_psu, near_limit, off_setpoint) and how the hold ended."""

    voltage: float  # V held
    limit_a: float  # A, the current limit used
    to: str
    rows: list[dict]
    stopped_by: Literal["ctrl-c", "timeout"]
    issues: list[str] = field(default_factory=list)

    def report(self) -> str:
        lines = [
            f"PSU hold: {self.voltage:g} V on {'VIN' if self.to == 'vin' else 'the bench'}, "
            f"current limit {self.limit_a * 1e3:g} mA",
            *([f"  {INVALID}"] if self.issues else []),
        ]
        if not self.rows:
            return "\n".join([*lines, "  no readings were taken"])
        v = np.array([r["v_psu"] for r in self.rows])
        i = np.array([r["i_psu"] for r in self.rows]) * 1e3
        near = sum(r["near_limit"] for r in self.rows)
        how = "Ctrl-C" if self.stopped_by == "ctrl-c" else "the hard timeout"
        lines += [
            f"  held {self.rows[-1]['t_s']:.0f} s, {len(self.rows)} readings, ended by {how}",
            f"  V readback {v.min():.4f} .. {v.max():.4f} V (mean {v.mean():.4f} V)",
            f"  I readback {i.min():.3f} .. {i.max():.3f} mA (mean {i.mean():.3f} mA)",
        ]
        if near:
            lines.append(
                f"  {near} of {len(self.rows)} readings near the current limit: the supply may have been in "
                "constant current, below the setpoint"
            )
        return "\n".join(lines)


def run(bench: Bench, hc: PsuHoldConfig) -> HoldResult:
    """Hold hc.voltage until Ctrl-C or hc.max_minutes (see the module docstring)."""
    bench.confirm_wiring(f"PSU hold at {hc.voltage:g} V", wiring(bench, hc), ref_probe=("vin", 0))
    limit = bench.set_psu_limit(amps=hc.current_limit)
    apply(bench, hc, hc.voltage)
    log.info(
        "Holding %g V (limit %g mA) for up to %g min: Ctrl-C to stop",
        hc.voltage,
        limit * 1e3,
        hc.max_minutes,
    )
    rows: list[dict] = []
    stopped_by: Literal["ctrl-c", "timeout"] = "timeout"
    t0 = bench.now()
    try:
        while (t := bench.now() - t0) <= hc.max_minutes * 60:
            row = {"t_s": t, **read(bench, hc, hc.voltage, limit)}
            rows.append(row)
            flag = "  NEAR LIMIT" if row["near_limit"] else ("  OFF SETPOINT" if row["off_setpoint"] else "")
            log.info("%7.1f s  %8.4f V  %9.3f mA%s", t, row["v_psu"], row["i_psu"] * 1e3, flag)
            bench.sleep(hc.interval)
    except KeyboardInterrupt:
        stopped_by = "ctrl-c"  # the normal way to end it; the Bench shutdown still runs
    log.info("hold ended (%s) after %d readings", stopped_by, len(rows))
    issues = off_setpoint_issue(rows, hc, bench.cfg.psu.channel)
    return HoldResult(hc.voltage, limit, hc.to, rows, stopped_by, issues)
