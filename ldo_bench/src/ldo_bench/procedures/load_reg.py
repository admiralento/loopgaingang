"""Load regulation: step the load bank at fixed VIN and record VOUT.

VOUT and VIN are measured at the board with the AD2 (DC-averaged), not from
the supply's readback, so cable drop does not count against the LDO. Load
current is estimated from the bank resistors and the measured VOUT; the PSU's
current readback is recorded too (it adds the LDO's quiescent current).
When a DMM is configured it measures VOUT instead of the AD2 (bench.measure_point);
the AD2's reading is still saved as vout_ad2_v.

run() steps:
1. Confirm the wiring. Codes = load_reg.codes, or every code whose estimated
   current is within dut.i_max (the load bank refuses any code above it).
2. Load bank off, PSU ramps VIN to `vin`.
3. For each code: set it, wait settle_time, one measure_point.
4. Load bank off. `analysis.load_regulation` fits VOUT against the estimated
   load current: slope (V/A = mV/mA; -slope is the DC output resistance), and
   dVOUT from the lightest to the heaviest load (also as % of VOUT).

Validity check: VOUT must be near nominal (bench.vout_issue) at every point, which
also catches a probe that comes loose partway through. Any failure makes the run
invalid and the plot shows only the measured data, with no fit.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np

from ldo_bench import plotting
from ldo_bench.analysis import LoadRegulation, load_regulation
from ldo_bench.bench import Bench
from ldo_bench.config import LoadRegConfig

log = logging.getLogger(__name__)

NAME = "load-reg"


@dataclass
class LoadRegResult:
    """A load sweep at one VIN. `rows` is data.csv: one dict per load code (volts, amps)."""

    vin_set: float  # V, PSU setpoint on VIN
    rows: list[dict]
    reg: LoadRegulation  # fit and endpoints (see analysis.LoadRegulation)
    vout_source: str = "ad2"  # "dmm" or "ad2"
    vout_nominal: float = 1.0  # V
    issues: list[str] = field(default_factory=list)  # setup problems: non-empty = not valid
    notes: list[str] = field(default_factory=list)  # worth knowing, not a setup problem

    def summary(self) -> dict:
        return {
            "valid": not self.issues,
            "issues": self.issues,
            "notes": self.notes,
            "vin_set": self.vin_set,
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
                    f"Load regulation at VIN = {self.vin_set:.3f} V (VOUT from {self.vout_source.upper()}):",
                    "  NOT A VALID MEASUREMENT (data saved and plotted as measured):",
                    *(f"    - {i}" for i in self.issues),
                    f"  measured VOUT ranged {min(v):.4f} to {max(v):.4f} V over {len(v)} points "
                    f"(expected ~{self.vout_nominal} V)",
                ]
            )
        return (
            f"Load regulation at VIN = {self.vin_set:.3f} V, {r.i_min * 1e3:.1f} to {r.i_max * 1e3:.1f} mA "
            f"(VOUT from {self.vout_source.upper()}):\n"
            f"  VOUT       {r.v_at_i_min:.5f} V -> {r.v_at_i_max:.5f} V\n"
            f"  dVOUT      {r.delta_v * 1e3:.3f} mV  ({r.percent:.3f} %)\n"
            f"  slope      {r.slope:.4f} mV/mA  (R_out {r.r_out * 1e3:.1f} mOhm, R^2 {r.r2:.4f})"
            + "".join(f"\n  note: {n}" for n in self.notes)
        )


def wiring(bench: Bench) -> list[str]:
    """Wiring lines shown before the run."""
    a, lb = bench.cfg.ad2, bench.cfg.loadbank
    return [
        "PSU -> VIN (DC input, not through the buffer)",
        bench.vout_wiring(),
        f"Scope {a.ref_channel}+ -> VIN at the board, {a.ref_channel}- -> GND",
        f"DIO {lb.dio_pins} -> load bank bits B0..B{len(lb.dio_pins) - 1}",
    ]


def run(bench: Bench, lr: LoadRegConfig) -> LoadRegResult:
    """Step through the load codes and fit load regulation (see the module docstring).

    lr: the [load_reg] section. Setup problems are recorded in `issues`, never raised.
    """
    bank = bench.loadbank
    codes = lr.codes or bank.allowed_codes()  # [] = every code within dut.i_max
    bench.confirm_wiring("load regulation", wiring(bench), ref_probe=("vin", 0))

    bench.ldo_enable(False)  # --enable dio only: EN held low while VIN comes up
    bank.off()
    bench.vin_from_psu(lr.vin)
    bench.ldo_enable(True)
    bench.reference_on()  # --vref ext only: external reference on VREF_IN, proven to set VOUT
    rows = []
    for k, code in enumerate(codes):
        bank.set(code)
        bench.sleep(lr.settle_time)
        p = bench.measure_point(lr.dc_window)
        # every point: catches a probe that comes loose mid-sweep
        issue = bench.vout_issue(p.vout, source=p.source.upper())
        vin_issue = bench.vin_issue(p.vin, lr.vin, source=f"scope {bench.cfg.ad2.ref_channel}")
        agree_issue = bench.vout_agreement_issue(p)
        row = {
            "code": code,
            "i_est_a": bank.current(code, p.vout),  # used for the fit
            "i_expected_a": bank.current(code, bench.cfg.dut.vout_nominal),  # what the code should draw
            "vout_ok": issue is None,
            "vin_issue": vin_issue or "",
            "vout_agree_issue": agree_issue or "",
            "vout_v": p.vout,
            "vout_std_v": p.vout_std,
            "vout_source": p.source,
            "vout_ad2_v": p.vout_ad2,
            "dmm_range_v": p.dmm_range,
            "vin_board_v": p.vin,
            "vin_set_v": lr.vin,
        }
        if lr.psu_readback:
            row["v_psu_v"] = bench.psu.measure_voltage()
            row["i_psu_a"] = bench.psu.measure_current()
        rows.append(row)
        log.info(
            "[%2d/%d] code %2d  I %7.2f mA  VOUT %.5f V",
            k + 1,
            len(codes),
            code,
            row["i_est_a"] * 1e3,
            p.vout,
        )
    bench.ldo_enable(False)
    bank.off()

    bad = [r for r in rows if not r["vout_ok"]]
    issues = []
    if bad:
        first = bad[0]
        issues.append(
            f"VOUT out of range at {len(bad)} of {len(rows)} points (code {first['code']} reads "
            f"{first['vout_v']:.4f} V, expected {bench.cfg.dut.vout_nominal} V +/-"
            f"{bench.cfg.dut.vout_tolerance:.0%}): is the {first['vout_source'].upper()} on VOUT / GND, "
            "the LDO enabled and VIN applied?"
        )
        log.warning("load regulation: %s", issues[0])
    vin_bad = [r for r in rows if r["vin_issue"]]
    if vin_bad:
        issues.append(f"{vin_bad[0]['vin_issue']} ({len(vin_bad)} of {len(rows)} points)")
    agree_bad = [r for r in rows if r["vout_agree_issue"]]
    if agree_bad:
        issues.append(
            f"code {agree_bad[0]['code']}: {agree_bad[0]['vout_agree_issue']} ({len(agree_bad)} of {len(rows)} points)"
        )
    notes = []
    ranges = sorted({r["dmm_range_v"] for r in rows if np.isfinite(r["dmm_range_v"])})
    if len(ranges) > 1:
        notes.append(
            f"the DMM autoranged during the sweep ({' / '.join(f'{g:g} V' for g in ranges)} ranges): a "
            "range-to-range offset may bend the fit. Fixing dmm.range avoids it, at the cost of resolution"
        )
    if lr.psu_readback:
        issues += _load_current_issues(bench, rows)
    else:
        notes.append(
            "load current is the bank estimate (VOUT / R) only: with load_reg.psu_readback off nothing "
            "checks that the load bank draws it (run loadbank-check)"
        )
    i = np.array([r["i_est_a"] for r in rows])
    v = np.array([r["vout_v"] for r in rows])
    return LoadRegResult(
        lr.vin, rows, load_regulation(i, v), rows[0]["vout_source"], bench.cfg.dut.vout_nominal, issues, notes
    )


def _load_current_issues(bench: Bench, rows: list[dict]) -> list[str]:
    """Compare the estimated load current with the rise in the supply's own current reading.

    The fit uses the estimate (VOUT / R); a dead bit, a missing ground or a swapped wire would
    otherwise look like excellent regulation. Each point's supply current minus the lightest load's
    must match its estimate minus the lightest load's, within loadbank_check's tolerances (the
    subtraction cancels the readback offset and the LDO's own quiescent current).
    """
    lc = bench.cfg.loadbank_check
    base = min(rows, key=lambda r: r["i_est_a"])
    for r in rows:
        expected = r["i_est_a"] - base["i_est_a"]
        seen = r["i_psu_a"] - base["i_psu_a"]
        if abs(seen - expected) > max(lc.tolerance * abs(expected), lc.abs_tolerance):
            return [
                f"at code {r['code']} the load bank should draw {expected * 1e3:.1f} mA more than at code "
                f"{base['code']}, but the supply's current rose by {seen * 1e3:.1f} mA: is the load bank "
                "wired (DIO pins, AD2 GND)? Run loadbank-check"
            ]
    return []


def _plot_invalid(res: LoadRegResult, dut: str):
    """Measured VOUT in absolute volts against the expected load current, with the expected VOUT.

    Uses the current at nominal VOUT for x, since the estimate from a wrong VOUT would be wrong too.
    """
    i_ma = np.array([row["i_expected_a"] for row in res.rows]) * 1e3
    v = np.array([row["vout_v"] for row in res.rows])
    order = np.argsort(i_ma)
    fig, ax = plotting.figure()
    ax.axhline(
        res.vout_nominal,
        color=plotting.CRITICAL,
        lw=1.2,
        ls=(0, (5, 3)),
        label=f"Expected VOUT {res.vout_nominal:.3f} V",
        zorder=2,
    )
    ax.plot(
        i_ma[order],
        v[order],
        color=plotting.SERIES[0],
        marker="o",
        ms=4,
        mec=plotting.SURFACE,
        mew=1,
        label=f"Measured ({res.vout_source.upper()})",
        zorder=3,
    )
    lo, hi = min(v.min(), res.vout_nominal), max(v.max(), res.vout_nominal)
    pad = 0.15 * (hi - lo or 0.01)
    ax.set_ylim(lo - pad, hi + pad)
    ax.set_xlabel("Load current at nominal VOUT (mA)")
    ax.set_ylabel("VOUT (V)")
    ax.set_title("SETUP PROBLEM, not a valid measurement", pad=20, color=plotting.CRITICAL)
    plotting.subtitle(
        ax, f"Load regulation: {dut}  ·  VIN {res.vin_set:.2f} V  ·  no fit: see the setup problems"
    )
    ax.legend(loc="center right")
    plotting.issue_box(ax, res.issues)
    return fig


def plot(res: LoadRegResult, dut: str):
    """VOUT change (mV, from VOUT at the lightest load) against load current, with the fit line.

    Invalid runs: _plot_invalid.
    """
    if res.issues:
        return _plot_invalid(res, dut)
    r = res.reg
    i_ma = np.array([row["i_est_a"] for row in res.rows]) * 1e3
    dv_mv = (np.array([row["vout_v"] for row in res.rows]) - r.v_at_i_min) * 1e3
    order = np.argsort(i_ma)

    fig, ax = plotting.figure()
    xs = np.array([i_ma.min(), i_ma.max()])
    fit_mv = (r.slope * xs * 1e-3 + r.intercept - r.v_at_i_min) * 1e3  # xs in mA, slope in V/A
    ax.plot(xs, fit_mv, color=plotting.INK_2, lw=1.2, ls=(0, (4, 3)), label="Linear fit", zorder=2)
    ax.plot(
        i_ma[order],
        dv_mv[order],
        color=plotting.SERIES[0],
        marker="o",
        ms=4,
        mec=plotting.SURFACE,
        mew=1,
        label="Measured",
        zorder=3,
    )
    ax.set_xlabel("Load current (mA)")
    ax.set_ylabel(f"VOUT change from {r.v_at_i_min:.4f} V (mV)")
    ax.set_title(f"Load regulation: {dut}", pad=20)
    plotting.subtitle(
        ax,
        f"VIN {res.vin_set:.2f} V  ·  VOUT by {res.vout_source.upper()}  ·  "
        f"ΔVOUT {r.delta_v * 1e3:.2f} mV ({r.percent:.3f} %) over "
        f"{r.i_min * 1e3:.0f} to {r.i_max * 1e3:.0f} mA  ·  slope {r.slope:.4f} mV/mA",
    )
    ax.legend(loc="best")
    return fig
