"""Line transient: step VIN with the AD2 wavegen through the BUF634 and measure VOUT.

Signal path: W1 -> VIN_FG / V_SIG SMA -> BUF634 (unity gain) -> VIN. The PSU
powers the buffer rail (VS_BUFF / VDD_BUF), which sits above the LDO's VIN
limit, so before raising it we check that the PSU is *not* on VIN. The
buffer's offset is calibrated out using VIN measured at the board, so the
steps land on vin_low / vin_high. The scope triggers on VIN itself.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np

from ldo_bench.analysis import SettlingResult, settling_analysis
from ldo_bench.bench import Bench
from ldo_bench.config import AnalysisConfig, LineTranConfig
from ldo_bench.instruments.base import ChannelSetup, InstrumentError
from ldo_bench.procedures.transient import averaged_step, capture_issues, edge_report, plot_edges

log = logging.getLogger(__name__)

NAME = "line-tran"
INTERLOCK_RAIL = 1.0  # V on the buffer rail while checking the PSU isn't on VIN
INTERLOCK_FRACTION = 0.6  # VIN above this fraction of the rail means the PSU is on VIN
CAL_TOLERANCE = 0.005  # V, how close the calibrated VIN levels must land
CAL_ITERATIONS = 4
CAL_MAX_CORRECTION = 0.25  # V: W1 never goes further than this from the target chasing a dead buffer


@dataclass
class EdgeResult:
    edge: str  # "rising" = VIN stepped up
    v_from: float  # measured VIN levels (V)
    v_to: float
    metrics: SettlingResult
    t: np.ndarray
    vout: np.ndarray
    vout_smoothed: np.ndarray
    ref: np.ndarray
    trigger_level: float | None = None
    issues: list[str] = field(default_factory=list)
    vout_channel: int = 1

    @property
    def title(self) -> str:
        word = "VIN step up" if self.edge == "rising" else "VIN step down"
        return f"{word}: {self.v_from:.2f} → {self.v_to:.2f} V"

    @property
    def rejection(self) -> float:
        """Peak VOUT deviation per volt of VIN step (V/V)."""
        dv = self.v_to - self.v_from
        return abs(self.metrics.deviation / dv) if dv else float("nan")


@dataclass
class LineTranResult:
    rail: float
    load_code: int
    i_load: float
    w1_low: float  # wavegen setpoints after calibration
    w1_high: float
    averages: int
    edges: list[EdgeResult]
    vout_nominal: float = 1.0
    setup_issues: list[str] = field(default_factory=list)  # found before any capture

    @property
    def issues(self) -> list[str]:
        out = list(self.setup_issues)
        for e in self.edges:
            out += [f"{e.edge}: {i}" for i in e.issues if i not in self.setup_issues]
        return out

    @property
    def rows(self) -> list[dict]:
        out = []
        for e in self.edges:
            m = e.metrics
            out.append(
                {
                    "edge": e.edge,
                    "vin_from_v": e.v_from,
                    "vin_to_v": e.v_to,
                    "nominal_v": m.nominal,
                    "peak_v": m.peak,
                    "deviation_v": m.deviation,
                    "deviation_per_vin_v_per_v": e.rejection,
                    "band_v": m.band,
                    "settle_level_v": m.settle_level,
                    "settling_time_s": m.settling_time,
                    "noise_v": m.noise,
                    "final_v": m.final,
                    "trustworthy": m.trustworthy,
                    "load_code": self.load_code,
                    "i_load_a": self.i_load,
                    "valid": not e.issues,
                    "issues": " | ".join(e.issues),
                }
            )
        return out

    def extra_csvs(self) -> dict[str, list[dict]]:
        return {
            f"waveform_{e.edge}.csv": [
                {"t_s": float(t), "vout_v": float(v), "vout_smoothed_v": float(f), "vin_v": float(r)}
                for t, v, f, r in zip(e.t, e.vout, e.vout_smoothed, e.ref, strict=True)
            ]
            for e in self.edges
        }

    def summary(self) -> dict:
        return {
            "valid": not self.issues,
            "issues": self.issues,
            "buffer_rail_v": self.rail,
            "load_code": self.load_code,
            "i_load_a": self.i_load,
            "w1_low_v": self.w1_low,
            "w1_high_v": self.w1_high,
            "averages": self.averages,
            "edges": {
                e.edge: {
                    **e.metrics.as_dict(),
                    "vin_from": e.v_from,
                    "vin_to": e.v_to,
                    "rejection": e.rejection,
                }
                for e in self.edges
            },
        }

    def report(self) -> str:
        lines = [
            f"Line transient at {self.i_load * 1e3:.1f} mA (code {self.load_code}), "
            f"buffer rail {self.rail:.2f} V, {self.averages} averages per edge:"
        ]
        for e in self.edges:
            extra = [f"    per VIN   {e.rejection * 1e3:.1f} mV of VOUT per V of VIN step"]
            lines += edge_report(e, extra)
        return "\n".join(lines)


def wiring(bench: Bench) -> list[str]:
    a, lb = bench.cfg.ad2, bench.cfg.loadbank
    lines = [
        f"PSU OUT{bench.cfg.psu.channel} -> buffer rail VS_BUFF / VDD_BUF and GND. NOT on VIN: "
        "the rail goes above the LDO's VIN limit",
        f"AD2 W{a.wavegen_channel} -> buffer input SMA (VIN_FG / V_SIG); buffer output onto VIN (JP4)",
        f"Scope {a.vout_channel}+ -> VOUT at the output pins, {a.vout_channel}- -> GND",
        f"Scope {a.ref_channel}+ -> VIN at the LDO input (trigger + VIN level), {a.ref_channel}- -> GND",
    ]
    if bench.cfg.line_tran and bench.cfg.line_tran.load_code:
        lines.append(f"DIO {lb.dio_pins} -> load bank bits B0..B{len(lb.dio_pins) - 1}, AD2 GND -> board GND")
    return lines


def _measure_vin(bench: Bench) -> float:
    _, vin = bench.measure_dc(0.05)
    return vin.mean


def _interlock(bench: Bench, rail: float) -> None:
    """Refuse to raise the rail if the PSU is wired to VIN instead of the buffer rail."""
    bench.wavegen_off()
    v_check = min(INTERLOCK_RAIL, rail)
    bench.buffer_rail(v_check)
    vin = _measure_vin(bench)
    log.info("interlock: rail %.2f V with W1 off -> VIN %.3f V", v_check, vin)
    if vin > INTERLOCK_FRACTION * v_check:
        raise InstrumentError(
            f"with the buffer rail at {v_check:.2f} V and W1 off, VIN reads {vin:.3f} V: the PSU looks "
            f"connected to VIN, not to the buffer rail. Stopping before raising it to {rail:.2f} V "
            f"(above dut.vin_max {bench.cfg.dut.vin_max} V). Move the PSU leads to VS_BUFF / VDD_BUF."
        )


def _calibrate(bench: Bench, target: float) -> tuple[float, float, str | None]:
    """W1 setpoint that puts VIN at `target` through the buffer: (w1, measured VIN, issue or None).

    The correction is capped (CAL_MAX_CORRECTION) so a buffer that isn't driving VIN can't make us
    push W1 up; going over dut.vin_max is a hard stop.
    """
    w = target
    vin = float("nan")
    for _ in range(CAL_ITERATIONS):
        bench.vin_from_wavegen(w)
        bench.sleep(0.02)
        vin = _measure_vin(bench)
        if vin > bench.cfg.dut.vin_max:
            raise InstrumentError(f"VIN reached {vin:.3f} V, above dut.vin_max: stopping")
        err = target - vin
        if abs(err) <= CAL_TOLERANCE:
            return w, vin, None
        # capped near the target, and never above the DUT's VIN limit (a weak buffer becomes an issue)
        w_max = min(target + CAL_MAX_CORRECTION, bench.cfg.dut.vin_max)
        w = min(max(w + err, target - CAL_MAX_CORRECTION, 0.0), w_max)
    bench.vin_from_wavegen(w)
    return (
        w,
        vin,
        f"VIN reached {vin:.3f} V, not {target:.3f} V, with W1 at {w:.3f} V: check the buffer rail "
        "headroom (line_tran.psu_voltage), JP4, and the W1 -> buffer input wiring",
    )


def run(bench: Bench, lt: LineTranConfig, an: AnalysisConfig) -> LineTranResult:
    a = bench.cfg.ad2
    bench.confirm_wiring("line transient", wiring(bench), ref_probe=("vin", 0), psu_to="rail")
    setups = {
        a.vout_channel: ChannelSetup(a.vout.range, a.vout.offset),
        a.ref_channel: ChannelSetup(a.ref.range, a.ref.offset),
    }
    bench.loadbank.set(lt.load_code)
    _interlock(bench, lt.psu_voltage)
    bench.buffer_rail(lt.psu_voltage)
    w_lo, v_lo, cal_lo = _calibrate(bench, lt.vin_low)
    w_hi, v_hi, cal_hi = _calibrate(bench, lt.vin_high)
    log.info("VIN levels %.4f / %.4f V (W1 %.4f / %.4f V)", v_lo, v_hi, w_lo, w_hi)
    bench.vin_from_wavegen(w_lo)
    vout_dc, _ = bench.measure_dc(0.05)
    setup_issues = [
        i for i in (cal_lo, cal_hi, bench.vout_issue(vout_dc.mean, source=f"scope {a.vout_channel}")) if i
    ]
    for i in setup_issues:
        log.warning("line transient: %s (running anyway; the data will show it)", i)
    i_load = bench.loadbank.current(lt.load_code, bench.cfg.dut.vout_nominal)
    # trigger halfway between the commanded levels (the measured ones may be wrong if the buffer is)
    trig_level = (lt.vin_low + lt.vin_high) / 2

    results = []
    for edge in lt.edges:
        (w_from, v_from), (w_to, v_to) = ((w_lo, v_lo), (w_hi, v_hi))[:: 1 if edge == "rising" else -1]
        cap, trig_issues = averaged_step(
            bench,
            lt,
            setups=setups,
            trigger_channel=a.ref_channel,
            trigger_level=trig_level,
            trigger_edge=edge,
            start=lambda w=w_from: bench.vin_from_wavegen(w),
            step=lambda w=w_to: bench.vin_from_wavegen(w),
            label=f"VIN step {edge}",
        )
        vout, vin = cap.channel(a.vout_channel), cap.channel(a.ref_channel)
        m, smoothed = settling_analysis(
            vout,
            vin,
            cap.sample_rate,
            edge,
            settle_fraction=an.settle_fraction,
            smooth_time=an.smooth_time,
            peak_window=an.peak_window,
            blank_time=an.blank_time,
            reference=an.settle_reference,
        )
        issues = (
            setup_issues
            + trig_issues
            + capture_issues(bench, m, cap, vin, expected_swing=abs(lt.vin_high - lt.vin_low), ref_desc="VIN")
        )
        issues = list(dict.fromkeys(issues))  # same problem reported once
        i0 = m.step_index if m.step_index >= 0 else cap.trigger_index
        t = (np.arange(len(vout)) - i0) / cap.sample_rate
        results.append(
            EdgeResult(edge, v_from, v_to, m, t, vout, smoothed, vin, trig_level, issues, a.vout_channel)
        )
        if issues:
            log.warning("VIN step %s: %d setup issue(s), see the plot", edge, len(issues))
        else:
            log.info("%s: peak %+.2f mV, settling %.2f us", edge, m.deviation * 1e3, m.settling_time * 1e6)

    bench.wavegen_off()
    bench.loadbank.off()
    return LineTranResult(
        lt.psu_voltage,
        lt.load_code,
        i_load,
        w_lo,
        w_hi,
        lt.averages,
        results,
        bench.cfg.dut.vout_nominal,
        setup_issues,
    )


def plot(res: LineTranResult, dut: str):
    return plot_edges(
        res.edges,
        vout_nominal=res.vout_nominal,
        ref_label="VIN, scope 2 (V)",
        suptitle=f"Line transient: {dut}  ·  {res.i_load * 1e3:.1f} mA load  ·  {res.averages} averages  ·  "
        "shaded = settling band",
    )
