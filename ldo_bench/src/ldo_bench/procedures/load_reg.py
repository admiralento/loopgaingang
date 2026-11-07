"""Load regulation: step the load bank at fixed VIN and record VOUT.

VOUT and VIN are measured at the board with the AD2 (DC-averaged), not from
the supply's readback, so cable drop does not count against the LDO. Load
current is estimated from the bank resistors and the measured VOUT; the PSU's
current readback is recorded too (it adds the LDO's quiescent current).
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
    vin_set: float
    rows: list[dict]
    reg: LoadRegulation
    vout_source: str = "ad2"
    vout_nominal: float = 1.0
    issues: list[str] = field(default_factory=list)

    def summary(self) -> dict:
        return {
            "valid": not self.issues,
            "issues": self.issues,
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
        )


def wiring(bench: Bench) -> list[str]:
    a, lb = bench.cfg.ad2, bench.cfg.loadbank
    return [
        "PSU -> VIN (DC input, not through the buffer)",
        bench.vout_wiring(),
        f"Scope {a.ref_channel}+ -> VIN at the board, {a.ref_channel}- -> GND",
        f"DIO {lb.dio_pins} -> load bank bits B0..B{len(lb.dio_pins) - 1}",
    ]


def run(bench: Bench, lr: LoadRegConfig) -> LoadRegResult:
    bank = bench.loadbank
    codes = lr.codes or bank.allowed_codes()  # [] = every code within dut.i_max
    bench.confirm_wiring("load regulation", wiring(bench), ref_probe=("vin", 0))

    bank.off()
    bench.vin_from_psu(lr.vin)
    rows = []
    for k, code in enumerate(codes):
        bank.set(code)
        bench.sleep(lr.settle_time)
        p = bench.measure_point(lr.dc_window)
        # every point: catches a probe that comes loose mid-sweep
        issue = bench.vout_issue(p.vout, source=p.source.upper())
        row = {
            "code": code,
            "i_est_a": bank.current(code, p.vout),
            "i_expected_a": bank.current(code, bench.cfg.dut.vout_nominal),
            "vout_ok": issue is None,
            "vout_v": p.vout,
            "vout_std_v": p.vout_std,
            "vout_source": p.source,
            "vout_ad2_v": p.vout_ad2,
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
    i = np.array([r["i_est_a"] for r in rows])
    v = np.array([r["vout_v"] for r in rows])
    return LoadRegResult(
        lr.vin, rows, load_regulation(i, v), rows[0]["vout_source"], bench.cfg.dut.vout_nominal, issues
    )


def _plot_invalid(res: LoadRegResult, dut: str):
    """Measured VOUT in absolute volts against the expected load current, with the expected VOUT."""
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
        ax, f"Load regulation: {dut}  ·  VIN {res.vin_set:.2f} V  ·  no fit: this VOUT isn't the LDO output"
    )
    ax.legend(loc="center right")
    plotting.issue_box(ax, res.issues)
    return fig


def plot(res: LoadRegResult, dut: str):
    if res.issues:
        return _plot_invalid(res, dut)
    r = res.reg
    i_ma = np.array([row["i_est_a"] for row in res.rows]) * 1e3
    dv_mv = (np.array([row["vout_v"] for row in res.rows]) - r.v_at_i_min) * 1e3
    order = np.argsort(i_ma)

    fig, ax = plotting.figure()
    xs = np.array([i_ma.min(), i_ma.max()])
    fit_mv = (r.slope * xs * 1e-3 + r.intercept - r.v_at_i_min) * 1e3
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
