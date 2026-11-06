"""Line transient: step VIN with the AD2 wavegen through the BUF634 and measure VOUT.

Signal path: W1 -> VIN_FG / V_SIG SMA -> BUF634 (unity gain) -> VIN. The PSU
powers the buffer rail (VS_BUFF / VDD_BUF), which sits above the LDO's VIN
limit, so before raising it we check that the PSU is *not* on VIN. The
buffer's offset is calibrated out using VIN measured at the board, so the
steps land on vin_low / vin_high. The scope triggers on VIN itself.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np

from ldo_bench.analysis import SettlingResult, settling_analysis
from ldo_bench.bench import Bench
from ldo_bench.config import AnalysisConfig, LineTranConfig
from ldo_bench.instruments.base import ChannelSetup, InstrumentError
from ldo_bench.procedures.transient import averaged_step, plot_edges

log = logging.getLogger(__name__)

NAME = "line-tran"
INTERLOCK_RAIL = 1.0  # V on the buffer rail while checking the PSU isn't on VIN
INTERLOCK_FRACTION = 0.6  # VIN above this fraction of the rail means the PSU is on VIN
CAL_TOLERANCE = 0.005  # V, how close the calibrated VIN levels must land
CAL_ITERATIONS = 4


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

    @property
    def title(self) -> str:
        word = "VIN step up" if self.edge == "rising" else "VIN step down"
        return f"{word}: {self.v_from:.2f} → {self.v_to:.2f} V"

    @property
    def rejection(self) -> float:
        """Peak VOUT deviation per volt of VIN step (V/V)."""
        return abs(self.metrics.deviation / (self.v_to - self.v_from))


@dataclass
class LineTranResult:
    rail: float
    load_code: int
    i_load: float
    w1_low: float  # wavegen setpoints after calibration
    w1_high: float
    averages: int
    edges: list[EdgeResult]

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
            m = e.metrics
            ts = (
                "did not settle in the capture"
                if np.isnan(m.settling_time)
                else f"{m.settling_time * 1e6:.2f} us"
            )
            lines += [
                f"  {e.title}",
                f"    nominal   {m.nominal:.5f} V",
                f"    peak      {m.deviation * 1e3:+.2f} mV  ({e.rejection * 1e3:.1f} mV/V of VIN step, "
                f"band ±{m.band * 1e3:.2f} mV)",
                f"    settling  {ts}  (to the {'pre-step' if m.reference == 'pre' else 'final'} level)",
                f"    noise     {m.noise * 1e3:.3f} mV rms"
                + ("" if m.trustworthy else "  ! above band/3: settling time limited by noise"),
            ]
            shift = m.final - m.nominal
            if m.reference == "pre" and np.isfinite(shift) and abs(shift) > m.band:
                lines.append(
                    f"    ! VOUT ends {shift * 1e3:+.2f} mV from the pre-step level (DC line regulation), "
                    f'outside the band. Set analysis.settle_reference = "post" to measure to the final level.'
                )
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


def _calibrate(bench: Bench, target: float) -> tuple[float, float]:
    """W1 setpoint that puts VIN at `target` through the buffer. Returns (w1, measured VIN)."""
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
            return w, vin
        w += err
    raise InstrumentError(
        f"could not set VIN to {target:.3f} V through the buffer (got {vin:.3f} V with W1 at {w:.3f} V). "
        "Check the buffer rail headroom (line_tran.psu_voltage), JP4, and the W1 -> buffer input wiring."
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
    w_lo, v_lo = _calibrate(bench, lt.vin_low)
    w_hi, v_hi = _calibrate(bench, lt.vin_high)
    log.info("VIN levels %.4f / %.4f V (W1 %.4f / %.4f V)", v_lo, v_hi, w_lo, w_hi)
    vout_dc, _ = bench.measure_dc(0.05)
    i_load = bench.loadbank.current(lt.load_code, vout_dc.mean)

    results = []
    for edge in lt.edges:
        (w_from, v_from), (w_to, v_to) = ((w_lo, v_lo), (w_hi, v_hi))[:: 1 if edge == "rising" else -1]
        cap = averaged_step(
            bench,
            lt,
            setups=setups,
            trigger_channel=a.ref_channel,
            trigger_level=(v_lo + v_hi) / 2,
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
        i0 = m.step_index if m.step_index >= 0 else cap.trigger_index
        t = (np.arange(len(vout)) - i0) / cap.sample_rate
        results.append(EdgeResult(edge, v_from, v_to, m, t, vout, smoothed, vin, (v_lo + v_hi) / 2))
        log.info("%s: peak %+.2f mV, settling %.2f us", edge, m.deviation * 1e3, m.settling_time * 1e6)

    bench.wavegen_off()
    bench.loadbank.off()
    return LineTranResult(lt.psu_voltage, lt.load_code, i_load, w_lo, w_hi, lt.averages, results)


def plot(res: LineTranResult, dut: str):
    return plot_edges(
        res.edges,
        ref_label="VIN, scope 2 (V)",
        suptitle=f"Line transient: {dut}  ·  {res.i_load * 1e3:.1f} mA load  ·  {res.averages} averages  ·  "
        "shaded = settling band",
    )
