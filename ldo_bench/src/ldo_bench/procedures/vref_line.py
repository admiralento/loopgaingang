"""vref-line: the BGR output (VREF, TP3) against VIN, with the LDO on.

First bring-up plan, Test 2.7: with EN high, step VIN down from 1.90 V and read VREF at TP3 with the
DMM (on its highest-input-resistance range, so it doesn't load the BGR). That gives:
  - the BGR's line sensitivity: dVREF/dVIN (mV/V) while it has headroom
  - the lowest VIN it works at: where VREF first falls `drop_fraction` below its value at vin_high
    (simulated ~1.56 V). Below it, the BGR rather than the pass device can set the LDO's dropout.

The DMM is the VREF reference. AD2 scope vout channel on J13 pin 4 (OUTA) reads VOUT, which shows
the LDO is enabled and following VREF (unity gain); scope ref channel on J13 pin 1 (INA) reads VIN
at the board (vin_source "ad2").

run() steps:
1. Confirm the wiring. EN low (--enable dio), VIN to vin_high, EN high.
2. For each VIN from vin_high down to vin_low: PSU to that VIN, wait settle_time, one
   `bench.measure_point` (VREF from the DMM, VOUT and VIN from the AD2).
3. EN low, load bank off. `analysis.line_regulation` on (VIN, VREF): the same fit and knee as
   line-reg, applied to VREF.

Validity checks (issues: the run is invalid, the data is still saved):
  - VREF at vin_high within vref_tolerance of vref_nominal (DMM on TP3, LDO enabled, JP2 side)
  - VOUT on the AD2 near vout_nominal at vin_high (the LDO enabled, scope on OUTA)
  - measured VIN within 2 % + 20 mV of its setpoint at every point
  - VREF below the knee threshold above the knee (a bad reading), or too few points to fit
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np

from ldo_bench import plotting
from ldo_bench import wiring as w
from ldo_bench.analysis import LineRegulation, dropout_strays, line_regulation
from ldo_bench.analysis import sweep_points as analysis_sweep
from ldo_bench.bench import Bench
from ldo_bench.config import VrefLineConfig
from ldo_bench.instruments.base import InstrumentError

log = logging.getLogger(__name__)

NAME = "vref-line"
KNEE_STEPS = 3  # the fit starts this many steps above the knee, as in line-reg


@dataclass
class VrefLineResult:
    """A downward VIN sweep with VREF on the DMM. `rows` is data.csv (volts)."""

    rows: list[dict]
    reg: LineRegulation  # the line-reg analysis applied to VREF: v_ref, slope, dropout_vin = BGR minimum VIN
    load_code: int
    vref_nominal: float  # V, expected VREF
    vin_source: str = "ad2"
    issues: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def vout_minus_vref(self) -> float:
        """VOUT (AD2) minus VREF (DMM) at vin_high (V); NaN without the AD2 reading."""
        r = self.rows[0]
        return r["vout_v"] - r["vref_v"]

    def summary(self) -> dict:
        r = self.reg
        return {
            "valid": not self.issues,
            "issues": self.issues,
            "notes": self.notes,
            "vin_source": self.vin_source,
            "load_code": self.load_code,
            "points": len(self.rows),
            "vref_at_vin_high_v": r.v_ref,
            "line_sensitivity_mv_per_v": r.slope * 1e3,
            "line_sensitivity_r2": r.r2,
            "fit_vin_min_v": r.fit_vin_min,
            "fit_vin_max_v": r.fit_vin_max,
            "bgr_min_vin_v": r.dropout_vin,  # NaN: VREF never fell drop_fraction in this sweep
            "drop_fraction": r.dropout_fraction,
            "vout_minus_vref_v": self.vout_minus_vref,
        }

    def report(self) -> str:
        r = self.reg
        head = (
            f"VREF vs VIN (BGR), VIN {self.rows[0]['vin_set_v']:.2f} -> {self.rows[-1]['vin_set_v']:.2f} V:"
        )
        if self.issues:
            return "\n".join(
                [head, "  NOT A VALID MEASUREMENT (data saved and plotted as measured):"]
                + [f"    - {i}" for i in self.issues]
            )
        knee = (
            f"VIN {r.dropout_vin:.3f} V (VREF {r.dropout_fraction:.0%} below its value at the top)"
            if np.isfinite(r.dropout_vin)
            else f"not reached: VREF stayed within {r.dropout_fraction:.0%} down to the bottom of the sweep"
        )
        lines = [
            head,
            f"  VREF          {r.v_ref:.5f} V at the highest VIN (DMM)",
            f"  line sens.    {r.slope * 1e3:.3f} mV/V  (R^2 {r.r2:.4f}, VIN {r.fit_vin_min:.2f} to {r.fit_vin_max:.2f} V)",
            f"  BGR min VIN   {knee}",
        ]
        if np.isfinite(self.vout_minus_vref):
            lines.append(
                f"  VOUT - VREF   {self.vout_minus_vref * 1e3:+.1f} mV at the top (VOUT on the AD2: its DC error, "
                "~10 mV, is included)"
            )
        lines += [f"  note: {n}" for n in self.notes]
        return "\n".join(lines)


def sweep_points(vl: VrefLineConfig) -> np.ndarray:
    """VIN setpoints (V): vin_high down to vin_low in vin_step, ending exactly on vin_low."""
    return analysis_sweep(vl.vin_high, vl.vin_low, vl.vin_step)


def wiring(bench: Bench, vl: VrefLineConfig) -> list[str]:
    """Wiring lines shown before the run, one connection per line (JAFWAK)."""
    cfg = bench.cfg
    lines = [
        *w.psu_on_vin(cfg),
        "Fit JP2 (the BGR drives VREF_IN)",
        w.dmm("VREF (TP3)", "GND (TP2)", "the run sets its input to 10 GOhm"),
    ]
    if vl.vin_source == "ad2":
        lines += [w.scope_vout(cfg), w.scope_vin(cfg)]
    if not bench.enable_dio:
        lines += w.enable_static(cfg)
    if vl.load_code:
        lines += w.load_bank(cfg)
    return [*lines, w.ad2_gnd(cfg)]


def run(bench: Bench, vl: VrefLineConfig) -> VrefLineResult:
    """Sweep VIN down and read VREF (see the module docstring for the steps and checks).

    vl: the [vref_line] section. Every setpoint goes through bench.vin_from_psu (dut.vin_max).
    Raises InstrumentError without a DMM; setup problems are recorded in `issues`, never raised.
    """
    if bench.dmm is None:
        raise InstrumentError("vref-line reads VREF with the DMM: enable [dmm] (and don't pass --no-dmm)")
    bank = bench.loadbank
    vins = sweep_points(vl)
    bench.confirm_wiring("VREF vs VIN", wiring(bench, vl), ref_probe=("vin", 0), dmm_probe="vref")

    bench.ldo_enable(False)  # --enable dio only: EN held low while VIN comes up
    bank.set(vl.load_code)
    bench.vin_from_psu(float(vins[0]))
    bench.ldo_enable(True)
    rows, vin_bad = [], []
    for k, v in enumerate(vins):
        bench.vin_from_psu(float(v))
        bench.sleep(vl.settle_time)
        p = bench.measure_point(vl.dc_window, vin_source=vl.vin_source)
        src = f"scope {bench.cfg.ad2.ref_channel}" if vl.vin_source == "ad2" else "PSU readback"
        if (vi := bench.vin_issue(p.vin, float(v), source=src)) is not None:
            vin_bad.append(vi)
        rows.append(
            {
                "vin_set_v": float(v),
                "vin_v": p.vin,
                "vin_source": vl.vin_source,
                "vref_v": p.vout,  # the DMM is on TP3
                "vref_std_v": p.vout_std,
                "dmm_range_v": p.dmm_range,
                "vout_v": p.vout_ad2,  # AD2 on OUTA; NaN with vin_source "psu"
                "load_code": vl.load_code,
            }
        )
        log.info("[%2d/%d] VIN %.3f V  VREF %.5f V  VOUT %.4f V", k + 1, len(vins), p.vin, p.vout, p.vout_ad2)
    bench.ldo_enable(False)
    bank.off()

    x = np.array([r["vin_v"] for r in rows])
    y = np.array([r["vref_v"] for r in rows])
    reg = line_regulation(x, y, dropout_fraction=vl.drop_fraction, knee_margin=KNEE_STEPS * vl.vin_step)
    issues = []
    top = rows[0]
    if abs(top["vref_v"] - vl.vref_nominal) > vl.vref_tolerance * vl.vref_nominal:
        issues.append(
            f"VREF reads {top['vref_v']:.4f} V at VIN {top['vin_set_v']:.2f} V, expected ~{vl.vref_nominal} V "
            f"(+/-{vl.vref_tolerance:.0%}): is the DMM on VREF (TP3) and GND, JP2 closed and the LDO enabled?"
        )
    if np.isfinite(top["vout_v"]) and (vo := bench.vout_issue(top["vout_v"], source="AD2 on OUTA")):
        issues.append(f"at VIN {top['vin_set_v']:.2f} V, {vo}")
    if vin_bad:
        issues.append(f"{vin_bad[0]} ({len(vin_bad)} of {len(rows)} points)")
    strays = dropout_strays(x, y, vl.drop_fraction)
    if strays and not issues:
        issues.append(
            f"VREF dips below the knee threshold at VIN {', '.join(f'{s:.2f}' for s in strays)} V, above the "
            "knee: a bad reading, or the BGR misbehaving there"
        )
    if not np.isfinite(reg.slope) and not issues:
        issues.append(
            "no line-sensitivity fit: fewer than 2 points above the knee. Raise vin_high or widen the sweep"
        )
    for i in issues:
        log.warning("vref-line: %s", i)
    notes = []
    if not issues and not np.isfinite(reg.dropout_vin):
        notes.append(f"VREF never fell {vl.drop_fraction:.0%}: the BGR still works at VIN {vins[-1]:.2f} V")
    return VrefLineResult(rows, reg, vl.load_code, vl.vref_nominal, vl.vin_source, issues, notes)


def plot(res: VrefLineResult, dut: str):
    """Top: VREF (DMM) and VOUT (AD2) against VIN, with the knee. Bottom: VREF in mV from its top
    value over the fit range, with the fit line. Invalid runs show the data as measured and the issues.
    """
    x = np.array([r["vin_v"] for r in res.rows])
    y = np.array([r["vref_v"] for r in res.rows])
    vo = np.array([r["vout_v"] for r in res.rows])
    order = np.argsort(x)
    x, y, vo = x[order], y[order], vo[order]
    marker = {"marker": "o", "ms": 3.2, "mec": plotting.SURFACE, "mew": 0.8}
    if res.issues:
        fig, ax = plotting.figure(size=(7.0, 4.6))
        ax.axhline(res.vref_nominal, color=plotting.CRITICAL, lw=1.2, ls=(0, (5, 3)), label="Expected VREF")
        ax.plot(x, y, color=plotting.SERIES[0], **marker, label="VREF (DMM)")
        ax.set_xlabel("VIN (V)")
        ax.set_ylabel("VREF (V)")
        ax.set_title("SETUP PROBLEM, not a valid measurement", pad=20, color=plotting.CRITICAL)
        ax.legend(loc="center right")
        plotting.issue_box(ax, res.issues)
        return fig
    r = res.reg
    fig, (top, bot) = plotting.figure(2, 1, size=(7.0, 6.4), height_ratios=[1.1, 1])
    top.axvspan(r.fit_vin_min, r.fit_vin_max, color=plotting.GRID, alpha=0.6, lw=0, zorder=0)
    top.plot(x, y, color=plotting.SERIES[0], **marker, label="VREF (DMM, TP3)", zorder=3)
    if np.isfinite(vo).any():
        top.plot(x, vo, color=plotting.SERIES[1], lw=1.2, label="VOUT (AD2, OUTA)", zorder=2)
    if np.isfinite(r.dropout_vin):
        top.axvline(r.dropout_vin, color=plotting.INK_2, lw=1, ls=(0, (2, 2)))
        top.annotate(
            f"BGR min VIN {r.dropout_vin:.3f} V",
            (r.dropout_vin, r.v_ref * (1 - r.dropout_fraction)),
            xytext=(6, -14),
            textcoords="offset points",
            fontsize=8,
            color=plotting.INK_2,
        )
    top.set_xlabel("VIN (V)")
    top.set_ylabel("V")
    top.set_title(f"VREF vs VIN: {dut}", pad=20)
    plotting.subtitle(top, f"line sensitivity {r.slope * 1e3:.3f} mV/V  ·  shaded = fit range")
    top.legend(loc="lower right")
    m = x >= r.fit_vin_min - 1e-9
    xs = np.array([r.fit_vin_min, r.fit_vin_max])
    bot.plot(
        xs,
        (r.slope * xs + r.intercept - r.v_ref) * 1e3,
        color=plotting.INK_2,
        lw=1.2,
        ls=(0, (4, 3)),
        label="Linear fit",
    )
    bot.plot(x[m], (y[m] - r.v_ref) * 1e3, color=plotting.SERIES[0], **marker, label="Measured")
    bot.set_xlabel("VIN (V)")
    bot.set_ylabel(f"VREF minus {r.v_ref:.4f} V (mV)")
    bot.set_title("With headroom (fit range)", fontsize=9.5, pad=6)
    bot.legend(loc="best")
    return fig
