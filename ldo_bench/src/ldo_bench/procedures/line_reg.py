"""Line regulation: sweep VIN at a fixed load and record VOUT.

The sweep starts low (in or near dropout) and goes up, so the same run gives
the dropout knee and the in-regulation slope. VIN is measured at the board
with the AD2 and that measured value is used for the fit, so cable drop does
not distort it. VOUT comes from the DMM when one is configured.

With line_reg.vin_source = "psu" and a DMM, VIN is the PSU's own readback instead and no scope
probes are needed (the DMM measures VOUT); that readback includes cable drop and the PSU's
readback error.

run() steps:
1. Confirm the wiring, set the load code (held for the whole sweep).
2. For each VIN in `sweep_points` (vin_start to vin_stop, low to high): PSU to that VIN, then one
   `bench.measure_point` (VOUT, VIN, optionally the PSU's V / I readback).
3. Load bank off. `analysis.line_regulation` on (measured VIN, VOUT): VOUT at the highest VIN is
   the reference; the dropout point is where VOUT falls `dropout_fraction` below it; the slope
   (dVOUT/dVIN, V/V) is a straight-line fit from fit_vin_min, or by default from KNEE_STEPS sweep
   steps above the dropout point, to the top of the sweep.

Validity check: at every point with VIN at least 0.5 V above vout_nominal (or at the top point
if the sweep stops lower), where the LDO should be regulating, VOUT must be near nominal
(bench.vout_issue). Points below that are in or near
dropout and are allowed to be low. Any failure makes the run invalid and the plot shows only the
measured data, with no fit.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np

from ldo_bench import plotting
from ldo_bench.analysis import LineRegulation, dropout_strays, line_regulation
from ldo_bench.bench import Bench
from ldo_bench.config import LineRegConfig

log = logging.getLogger(__name__)

NAME = "line-reg"
KNEE_STEPS = 3  # default fit starts this many sweep steps above the dropout point (clear of the knee's curve)


@dataclass
class LineRegResult:
    """A VIN sweep at one load. `rows` is data.csv: one dict per VIN point (volts, amps)."""

    load_code: int  # load bank code held during the sweep
    i_load: float  # A, that code's current at the nominal VOUT
    rows: list[dict]
    reg: LineRegulation  # slope, fit range, dropout (see analysis.LineRegulation)
    vout_source: str  # "dmm" or "ad2"
    vin_source: str = "ad2"  # "ad2" = measured at the board, "psu" = PSU readback
    vout_nominal: float = 1.0  # V
    issues: list[str] = field(default_factory=list)  # setup problems: non-empty = not valid
    notes: list[str] = field(default_factory=list)  # worth knowing, not a setup problem

    @property
    def vin_label(self) -> str:
        return "VIN at the board (V)" if self.vin_source == "ad2" else "VIN, PSU readback (V)"

    def summary(self) -> dict:
        return {
            "valid": not self.issues,
            "issues": self.issues,
            "notes": self.notes,
            "vin_source": self.vin_source,
            "load_code": self.load_code,
            "i_load_a": self.i_load,
            "points": len(self.rows),
            "vout_source": self.vout_source,
            **self.reg.as_dict(),
        }

    def report(self) -> str:
        r = self.reg
        if self.issues:
            v = [row["vout_v"] for row in self.rows]
            return "\n".join(
                [
                    f"Line regulation (code {self.load_code}), VOUT from {self.vout_source.upper()}:",
                    "  NOT A VALID MEASUREMENT (data saved and plotted as measured):",
                    *(f"    - {i}" for i in self.issues),
                    f"  measured VOUT ranged {min(v):.4f} to {max(v):.4f} V "
                    f"(expected ~{self.vout_nominal} V)",
                ]
            )
        if np.isfinite(r.dropout_vin):
            drop = (
                f"VIN {r.dropout_vin:.3f} V, dropout voltage {r.dropout_voltage * 1e3:.0f} mV "
                f"(VOUT {r.dropout_fraction * 100:g} % low)"
            )
        else:
            drop = "not reached in this sweep"
        return (
            f"Line regulation at {self.i_load * 1e3:.1f} mA (code {self.load_code}), "
            f"VOUT from {self.vout_source.upper()}, VIN from {self.vin_source.upper()}:\n"
            f"  VOUT       {r.v_ref:.5f} V at the highest VIN\n"
            f"  slope      {r.slope * 1e3:.4f} mV/V  ({r.percent_per_v:.4f} %/V, R^2 {r.r2:.4f})\n"
            f"  fit range  VIN {r.fit_vin_min:.3f} to {r.fit_vin_max:.3f} V  "
            f"(dVOUT {r.delta_v * 1e3:.3f} mV)\n"
            f"  dropout    {drop}" + "".join(f"\n  note: {n}" for n in self.notes)
        )


def sweep_points(lr: LineRegConfig) -> np.ndarray:
    """VIN setpoints (V): vin_start to vin_stop in vin_step, always ending exactly on vin_stop."""
    # 1e-9 so float error doesn't drop the last whole step
    n = int(np.floor((lr.vin_stop - lr.vin_start) / lr.vin_step + 1e-9)) + 1
    pts = lr.vin_start + lr.vin_step * np.arange(n)
    if pts[-1] < lr.vin_stop - 1e-9:
        pts = np.append(pts, lr.vin_stop)
    return np.round(pts, 6)


def wiring(bench: Bench, lr: LineRegConfig) -> list[str]:
    """Wiring lines shown before the run; the scope lines depend on vin_source and the DMM."""
    a, lb = bench.cfg.ad2, bench.cfg.loadbank
    lines = ["PSU -> VIN (DC input, not through the buffer)"]
    if lr.vin_source == "psu" and bench.dmm is not None:
        lines.append("DMM V+/COM -> VOUT / GND at the output pins (no scope probes needed)")
    else:
        lines.append(bench.vout_wiring())
    if lr.vin_source == "ad2":
        lines.append(f"Scope {a.ref_channel}+ -> VIN at the board, {a.ref_channel}- -> GND")
    if lr.load_code:
        lines.append(f"DIO {lb.dio_pins} -> load bank bits B0..B{len(lb.dio_pins) - 1}, AD2 GND -> board GND")
    return lines


def run(bench: Bench, lr: LineRegConfig) -> LineRegResult:
    """Sweep VIN and fit line regulation (see the module docstring for the steps and checks).

    lr: the [line_reg] section. Every setpoint goes through bench.vin_from_psu, which refuses
    anything above dut.vin_max. Setup problems are recorded in `issues`, never raised.
    """
    bank = bench.loadbank
    vins = sweep_points(lr)
    bench.confirm_wiring("line regulation", wiring(bench, lr), ref_probe=("vin", 0))

    bank.set(lr.load_code)
    rows = []
    # Low VIN points are in dropout (VOUT legitimately low): check VOUT at the first point with
    # clear headroom instead, or at the top of the sweep.
    v_check = min(bench.cfg.dut.vout_nominal + 0.5, float(vins[-1]))
    bad = []  # (VIN, VOUT, source) where VOUT should be regulated but isn't near nominal
    vin_bad: list[str] = []  # VIN measured off its setpoint
    agree_bad: list[str] = []  # AD2 VOUT disagrees with the DMM (scope 1 off VOUT)
    if bench.vref_external:
        # --vref ext: turn the reference on (and prove it) with full headroom, then sweep
        bench.vin_from_psu(float(max(vins)))
        bench.reference_on()
    for k, v in enumerate(vins):
        bench.vin_from_psu(float(v))
        p = bench.measure_point(lr.dc_window, vin_source=lr.vin_source)
        if v >= v_check - 1e-9 and bench.vout_issue(p.vout, source=p.source.upper()):
            bad.append((float(v), p.vout, p.source.upper()))
        vin_src = f"scope {bench.cfg.ad2.ref_channel}" if lr.vin_source == "ad2" else "PSU readback"
        if (vi := bench.vin_issue(p.vin, float(v), source=vin_src)) is not None:
            vin_bad.append(vi)
        if (ai := bench.vout_agreement_issue(p)) is not None:
            agree_bad.append(f"at VIN {float(v):.2f} V: {ai}")
        row = {
            "vin_set_v": float(v),
            "vin_v": p.vin,
            "vin_source": lr.vin_source,
            "vout_v": p.vout,
            "vout_std_v": p.vout_std,
            "vout_source": p.source,
            "vout_ad2_v": p.vout_ad2,
            "dmm_range_v": p.dmm_range,
            "load_code": lr.load_code,
            "i_est_a": bank.current(lr.load_code, p.vout),  # bank resistors at the measured VOUT
        }
        if lr.psu_readback:
            # With vin_source="psu" the voltage readback is already vin_v.
            row["v_psu_v"] = p.vin if lr.vin_source == "psu" else bench.psu.measure_voltage()
            row["i_psu_a"] = bench.psu.measure_current()
        rows.append(row)
        log.info("[%2d/%d] VIN %.3f V  VOUT %.5f V", k + 1, len(vins), p.vin, p.vout)
    bank.off()

    x = np.array([r["vin_v"] for r in rows])
    y = np.array([r["vout_v"] for r in rows])
    reg = line_regulation(
        x,
        y,
        dropout_fraction=lr.dropout_fraction,
        fit_vin_min=lr.fit_vin_min,
        knee_margin=KNEE_STEPS * lr.vin_step,
    )
    issues = []
    if bad:
        n_reg = sum(1 for r in rows if r["vin_set_v"] >= v_check - 1e-9)
        vb, vo, src = bad[0]
        issues.append(
            f"VOUT out of range at {len(bad)} of {n_reg} points with VIN >= {v_check:.2f} V (at VIN "
            f"{vb:.2f} V it reads {vo:.4f} V, expected {bench.cfg.dut.vout_nominal} V +/-"
            f"{bench.cfg.dut.vout_tolerance:.0%}): is the {src} on VOUT / GND and the LDO enabled?"
        )
        log.warning("line regulation: %s", issues[0])
    if vin_bad:
        issues.append(f"{vin_bad[0]} ({len(vin_bad)} of {len(rows)} points)")
    if agree_bad:
        issues.append(f"{agree_bad[0]} ({len(agree_bad)} of {len(rows)} points)")
    strays = dropout_strays(x, y, lr.dropout_fraction)
    if strays:
        issues.append(
            f"VOUT dips below the dropout threshold at VIN {', '.join(f'{s:.2f}' for s in strays)} V, above "
            "the dropout knee: a bad reading or the LDO misbehaving there (the knee is taken from the bottom "
            "of the sweep)"
        )
    if lr.fit_vin_min is not None and np.isfinite(reg.dropout_vin) and lr.fit_vin_min < reg.dropout_vin:
        issues.append(
            f"line_reg.fit_vin_min ({lr.fit_vin_min} V) is below the dropout point ({reg.dropout_vin:.3f} V): "
            "the fit includes dropout points"
        )
    if not np.isfinite(reg.slope):
        issues.append(
            f"no line regulation fit: fewer than 2 points above {reg.dropout_vin + KNEE_STEPS * lr.vin_step:.2f} V "
            "(the dropout knee + margin). Raise vin_stop or lower the load"
            if np.isfinite(reg.dropout_vin)
            else "no line regulation fit: fewer than 2 points in the sweep"
        )
    for i in issues[1 if bad else 0 :]:  # the VOUT issue (if any) was logged above
        log.warning("line regulation: %s", i)
    notes = []
    in_fit = [r for r in rows if r["vin_v"] >= reg.fit_vin_min - 1e-9]
    ranges = sorted({r["dmm_range_v"] for r in in_fit if np.isfinite(r["dmm_range_v"])})
    if len(ranges) > 1:
        switch = next(r["vin_v"] for r in in_fit if r["dmm_range_v"] != in_fit[0]["dmm_range_v"])
        notes.append(
            f"the DMM autoranged inside the fit ({' / '.join(f'{g:g} V' for g in ranges)} ranges, switching near "
            f"VIN {switch:.2f} V): a range-to-range offset may bend the slope. Fixing dmm.range avoids it, at the "
            "cost of resolution"
        )
    i_load = bank.current(lr.load_code, bench.cfg.dut.vout_nominal)
    return LineRegResult(
        lr.load_code,
        i_load,
        rows,
        reg,
        rows[0]["vout_source"],
        lr.vin_source,
        bench.cfg.dut.vout_nominal,
        issues,
        notes,
    )


def _plot_invalid(res: LineRegResult, dut: str):
    """The whole sweep as measured (absolute VOUT vs VIN) with the expected VOUT; no fit, no dropout."""
    x = np.array([row["vin_v"] for row in res.rows])
    y = np.array([row["vout_v"] for row in res.rows])
    order = np.argsort(x)
    fig, ax = plotting.figure(size=(7.0, 4.6))
    ax.axhline(
        res.vout_nominal,
        color=plotting.CRITICAL,
        lw=1.2,
        ls=(0, (5, 3)),
        label=f"Expected VOUT {res.vout_nominal:.3f} V",
        zorder=2,
    )
    ax.plot(
        x[order],
        y[order],
        color=plotting.SERIES[0],
        marker="o",
        ms=3.5,
        mec=plotting.SURFACE,
        mew=0.8,
        label=f"Measured ({res.vout_source.upper()})",
        zorder=3,
    )
    ax.set_xlabel(res.vin_label)
    ax.set_ylabel("VOUT (V)")
    ax.set_title("SETUP PROBLEM, not a valid measurement", pad=20, color=plotting.CRITICAL)
    plotting.subtitle(
        ax, f"{res.i_load * 1e3:.1f} mA load  ·  no fit: the measured VOUT isn't the LDO output"
    )
    ax.legend(loc="center right")
    plotting.issue_box(ax, res.issues)
    return fig


def plot(res: LineRegResult, dut: str):
    """Top: VOUT over the whole sweep with the dropout point and fit range. Bottom: the fit range
    zoomed in, as mV from VOUT at the highest VIN, with the fit line. Invalid runs: _plot_invalid.
    """
    if res.issues:
        return _plot_invalid(res, dut)
    r = res.reg
    x = np.array([row["vin_v"] for row in res.rows])
    y = np.array([row["vout_v"] for row in res.rows])
    order = np.argsort(x)
    x, y = x[order], y[order]

    fig, (top, bot) = plotting.figure(2, 1, size=(7.0, 6.4), height_ratios=[1.1, 1])
    marker = {"marker": "o", "ms": 3.5, "mec": plotting.SURFACE, "mew": 0.8}

    # Top: the whole sweep, with the dropout knee and the fit range.
    top.axvspan(r.fit_vin_min, r.fit_vin_max, color=plotting.GRID, alpha=0.6, lw=0, zorder=0)
    top.plot(x, y, color=plotting.SERIES[0], **marker, zorder=3)
    if np.isfinite(r.dropout_vin):
        top.axvline(r.dropout_vin, color=plotting.INK_2, lw=1, ls=(0, (2, 2)), zorder=2)
        top.annotate(
            f"dropout {r.dropout_voltage * 1e3:.0f} mV",
            (r.dropout_vin, r.v_ref * (1 - r.dropout_fraction)),
            xytext=(6, -14),
            textcoords="offset points",
            fontsize=8,
            color=plotting.INK_2,
        )
    top.set_ylabel("VOUT (V)")
    top.set_xlabel(res.vin_label)
    top.set_title(f"Line regulation: {dut}", pad=20)
    plotting.subtitle(
        top,
        f"{res.i_load * 1e3:.1f} mA load  ·  VOUT by {res.vout_source.upper()}  ·  "
        f"slope {r.slope * 1e3:.3f} mV/V ({r.percent_per_v:.4f} %/V)  ·  shaded = fit range",
    )

    # Bottom: zoom on the fit range, in mV.
    m = x >= r.fit_vin_min - 1e-9
    ref = r.v_ref
    xs = np.array([r.fit_vin_min, r.fit_vin_max])
    bot.plot(
        xs,
        (r.slope * xs + r.intercept - ref) * 1e3,
        color=plotting.INK_2,
        lw=1.2,
        ls=(0, (4, 3)),
        label="Linear fit",
        zorder=2,
    )
    bot.plot(x[m], (y[m] - ref) * 1e3, color=plotting.SERIES[0], **marker, label="Measured", zorder=3)
    bot.set_xlabel(res.vin_label)
    bot.set_ylabel(f"VOUT minus {ref:.4f} V (mV)")
    bot.set_title("In regulation (fit range)", fontsize=9.5, pad=6)
    bot.legend(loc="best")
    return fig
