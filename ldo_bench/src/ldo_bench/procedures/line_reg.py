"""Line regulation: sweep VIN at a fixed load and record VOUT.

The sweep starts low (in or near dropout) and goes up, so the same run gives
the dropout knee and the in-regulation slope. VIN is measured at the board
with the AD2 and that measured value is used for the fit, so cable drop does
not distort it. VOUT comes from the DMM when one is configured.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np

from ldo_bench import plotting
from ldo_bench.analysis import LineRegulation, line_regulation
from ldo_bench.bench import Bench
from ldo_bench.config import LineRegConfig

log = logging.getLogger(__name__)

NAME = "line-reg"
KNEE_STEPS = 3  # default fit starts this many sweep steps above the dropout point


@dataclass
class LineRegResult:
    load_code: int
    i_load: float
    rows: list[dict]
    reg: LineRegulation
    vout_source: str

    def summary(self) -> dict:
        return {
            "load_code": self.load_code,
            "i_load_a": self.i_load,
            "points": len(self.rows),
            "vout_source": self.vout_source,
            **self.reg.as_dict(),
        }

    def report(self) -> str:
        r = self.reg
        if np.isfinite(r.dropout_vin):
            drop = (
                f"VIN {r.dropout_vin:.3f} V, dropout voltage {r.dropout_voltage * 1e3:.0f} mV "
                f"(VOUT {r.dropout_fraction * 100:g} % low)"
            )
        else:
            drop = "not reached in this sweep"
        return (
            f"Line regulation at {self.i_load * 1e3:.1f} mA (code {self.load_code}), "
            f"VOUT from {self.vout_source.upper()}:\n"
            f"  VOUT       {r.v_ref:.5f} V at the highest VIN\n"
            f"  slope      {r.slope * 1e3:.4f} mV/V  ({r.percent_per_v:.4f} %/V, R^2 {r.r2:.4f})\n"
            f"  fit range  VIN {r.fit_vin_min:.3f} to {r.fit_vin_max:.3f} V  "
            f"(dVOUT {r.delta_v * 1e3:.3f} mV)\n"
            f"  dropout    {drop}"
        )


def sweep_points(lr: LineRegConfig) -> np.ndarray:
    n = int(np.floor((lr.vin_stop - lr.vin_start) / lr.vin_step + 1e-9)) + 1
    pts = lr.vin_start + lr.vin_step * np.arange(n)
    if pts[-1] < lr.vin_stop - 1e-9:
        pts = np.append(pts, lr.vin_stop)
    return np.round(pts, 6)


def wiring(bench: Bench) -> list[str]:
    a, lb = bench.cfg.ad2, bench.cfg.loadbank
    return [
        "PSU -> VIN (DC input, not through the buffer)",
        bench.vout_wiring(),
        f"Scope {a.ref_channel}+ -> VIN at the board, {a.ref_channel}- -> GND",
        f"DIO {lb.dio_pins} -> load bank bits B0..B{len(lb.dio_pins) - 1}",
    ]


def run(bench: Bench, lr: LineRegConfig) -> LineRegResult:
    bank = bench.loadbank
    vins = sweep_points(lr)
    bench.confirm_wiring("line regulation", wiring(bench), ref_probe=("vin", 0))

    bank.set(lr.load_code)
    rows = []
    for k, v in enumerate(vins):
        bench.vin_from_psu(float(v))
        p = bench.measure_point(lr.dc_window)
        row = {
            "vin_set_v": float(v),
            "vin_board_v": p.vin,
            "vout_v": p.vout,
            "vout_std_v": p.vout_std,
            "vout_source": p.source,
            "vout_ad2_v": p.vout_ad2,
            "load_code": lr.load_code,
            "i_est_a": bank.current(lr.load_code, p.vout),
        }
        if lr.psu_readback:
            row["v_psu_v"] = bench.psu.measure_voltage()
            row["i_psu_a"] = bench.psu.measure_current()
        rows.append(row)
        log.info("[%2d/%d] VIN %.3f V  VOUT %.5f V", k + 1, len(vins), p.vin, p.vout)
    bank.off()

    x = np.array([r["vin_board_v"] for r in rows])
    y = np.array([r["vout_v"] for r in rows])
    reg = line_regulation(
        x,
        y,
        dropout_fraction=lr.dropout_fraction,
        fit_vin_min=lr.fit_vin_min,
        knee_margin=KNEE_STEPS * lr.vin_step,
    )
    i_load = bank.current(lr.load_code, reg.v_ref)
    return LineRegResult(lr.load_code, i_load, rows, reg, rows[0]["vout_source"])


def plot(res: LineRegResult, dut: str):
    r = res.reg
    x = np.array([row["vin_board_v"] for row in res.rows])
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
    top.set_xlabel("VIN at the board (V)")
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
    bot.set_xlabel("VIN at the board (V)")
    bot.set_ylabel(f"VOUT minus {ref:.4f} V (mV)")
    bot.set_title("In regulation (fit range)", fontsize=9.5, pad=6)
    bot.legend(loc="best")
    return fig
