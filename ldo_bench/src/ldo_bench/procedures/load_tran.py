"""Load transient: step the load bank between two codes and measure VOUT's response.

The AD2 triggers on scope ref channel, wired to a load bank DIO pin that changes
between the two codes (the same setup as the WaveForms workspaces: Scope 2+ on
DIO 4 / J4). Each edge is captured `averages` times and averaged; the response
repeats on every step, so averaging removes noise without blurring it. The
analysis is the Python port of load_tran_script.js.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np

from ldo_bench.analysis import SettlingResult, settling_analysis
from ldo_bench.bench import Bench
from ldo_bench.config import AnalysisConfig, LoadTranConfig
from ldo_bench.instruments.base import ChannelSetup, InstrumentError
from ldo_bench.procedures.transient import averaged_step, plot_edges, validate_capture

log = logging.getLogger(__name__)

NAME = "load-tran"


@dataclass
class EdgeResult:
    edge: str  # "rising" = load stepped up (code_low -> code_high)
    code_from: int
    code_to: int
    i_from: float
    i_to: float
    metrics: SettlingResult
    t: np.ndarray  # s, 0 at the step
    vout: np.ndarray
    vout_smoothed: np.ndarray
    ref: np.ndarray
    trigger_level: float | None = None

    @property
    def title(self) -> str:
        word = "Load step up" if self.edge == "rising" else "Load step down"
        return f"{word}: {self.i_from * 1e3:.0f} → {self.i_to * 1e3:.0f} mA"


@dataclass
class LoadTranResult:
    vin: float
    trigger_pin: int
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
                    "code_from": e.code_from,
                    "code_to": e.code_to,
                    "i_from_a": e.i_from,
                    "i_to_a": e.i_to,
                    "nominal_v": m.nominal,
                    "peak_v": m.peak,
                    "deviation_v": m.deviation,
                    "band_v": m.band,
                    "settle_level_v": m.settle_level,
                    "settling_time_s": m.settling_time,
                    "noise_v": m.noise,
                    "final_v": m.final,
                    "trustworthy": m.trustworthy,
                    "sample_rate_hz": m.sample_rate,
                }
            )
        return out

    def extra_csvs(self) -> dict[str, list[dict]]:
        return {
            f"waveform_{e.edge}.csv": [
                {"t_s": float(t), "vout_v": float(v), "vout_smoothed_v": float(f), "ref_v": float(r)}
                for t, v, f, r in zip(e.t, e.vout, e.vout_smoothed, e.ref, strict=True)
            ]
            for e in self.edges
        }

    def summary(self) -> dict:
        return {
            "vin": self.vin,
            "trigger_pin": self.trigger_pin,
            "averages": self.averages,
            "edges": {
                e.edge: {**e.metrics.as_dict(), "i_from": e.i_from, "i_to": e.i_to} for e in self.edges
            },
        }

    def report(self) -> str:
        lines = [f"Load transient at VIN = {self.vin:.3f} V ({self.averages} averages per edge):"]
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
                f"    peak      {m.deviation * 1e3:+.2f} mV  (band ±{m.band * 1e3:.2f} mV)",
                f"    settling  {ts}  (to the {'pre-step' if m.reference == 'pre' else 'final'} level)",
                f"    noise     {m.noise * 1e3:.3f} mV rms"
                + ("" if m.trustworthy else "  ! above band/3: settling time limited by noise"),
            ]
            shift = m.final - m.nominal
            if m.reference == "pre" and np.isfinite(shift) and abs(shift) > m.band:
                lines.append(
                    f"    ! VOUT ends {shift * 1e3:+.2f} mV from the pre-step level (DC load regulation), "
                    f"outside the ±{m.band * 1e3:.2f} mV band, so it can never settle back to it. "
                    'Set analysis.settle_reference = "post" to measure settling to the final level.'
                )
        return "\n".join(lines)


def trigger_setup(bench: Bench, lt: LoadTranConfig) -> tuple[int, bool]:
    """(DIO pin on the ref channel, whether it rises when the load steps up)."""
    bank = bench.loadbank
    if lt.trigger_pin is None:
        return bank.trigger_pin(lt.code_low, lt.code_high)
    bit = bank.bit_for_pin(lt.trigger_pin)
    if not (lt.code_low ^ lt.code_high) >> bit & 1:
        raise InstrumentError(
            f"trigger_pin DIO {lt.trigger_pin} (bit {bit}) is the same in codes {lt.code_low} and "
            f"{lt.code_high}: it never moves, so the scope can't trigger on it"
        )
    return lt.trigger_pin, bank.pin_edge(bit, lt.code_high)


def wiring(bench: Bench, pin: int) -> list[str]:
    a, lb = bench.cfg.ad2, bench.cfg.loadbank
    bit = lb.dio_pins.index(pin)
    return [
        "PSU -> VIN",
        f"Scope {a.vout_channel}+ -> VOUT at the output pins, {a.vout_channel}- -> GND",
        f"Scope {a.ref_channel}+ -> DIO {pin} (load bank bit B{bit}, trigger), {a.ref_channel}- -> GND",
        f"DIO {lb.dio_pins} -> load bank bits B0..B{len(lb.dio_pins) - 1}, AD2 GND -> board GND",
    ]


def run(bench: Bench, lt: LoadTranConfig, an: AnalysisConfig) -> LoadTranResult:
    bank, a = bench.loadbank, bench.cfg.ad2
    pin, rises_with_load = trigger_setup(bench, lt)
    bench.confirm_wiring("load transient", wiring(bench, pin), ref_probe=("dio", pin))
    setups = {
        a.vout_channel: ChannelSetup(a.vout.range, a.vout.offset),
        a.ref_channel: ChannelSetup(a.ref.range, a.ref.offset),
    }
    bank.set(lt.code_low)
    bench.vin_from_psu(lt.vin)
    vout_est, _ = bench.measure_dc(0.05)
    bench.check_vout(vout_est.mean, test="load transient", source=f"scope {a.vout_channel}")
    v_nom = vout_est.mean

    results = []
    for edge in lt.edges:
        c_from, c_to = (lt.code_low, lt.code_high) if edge == "rising" else (lt.code_high, lt.code_low)
        pin_rises = rises_with_load if edge == "rising" else not rises_with_load
        trig_edge = "rising" if pin_rises else "falling"
        cap = averaged_step(
            bench,
            lt,
            setups=setups,
            trigger_channel=a.ref_channel,
            trigger_level=lt.dio_high_level / 2,
            trigger_edge=trig_edge,
            start=lambda c=c_from: bank.set(c),
            step=lambda c=c_to: bank.set(c),
            label=f"load step {edge}",
        )
        vout, ref = cap.channel(a.vout_channel), cap.channel(a.ref_channel)
        m, smoothed = settling_analysis(
            vout,
            ref,
            cap.sample_rate,
            trig_edge,
            settle_fraction=an.settle_fraction,
            smooth_time=an.smooth_time,
            peak_window=an.peak_window,
            blank_time=an.blank_time,
            reference=an.settle_reference,
        )
        validate_capture(
            bench,
            m,
            ref,
            expected_swing=lt.dio_high_level,
            test=f"load transient ({edge})",
            ref_desc=f"DIO {pin}",
        )
        i0 = m.step_index
        t = (np.arange(len(vout)) - i0) / cap.sample_rate
        results.append(
            EdgeResult(
                edge,
                c_from,
                c_to,
                bank.current(c_from, v_nom),
                bank.current(c_to, v_nom),
                m,
                t,
                vout,
                smoothed,
                ref,
                lt.dio_high_level / 2,
            )
        )
        log.info("%s: peak %+.2f mV, settling %.2f us", edge, m.deviation * 1e3, m.settling_time * 1e6)
    bank.off()
    return LoadTranResult(lt.vin, pin, lt.averages, results)


def plot(res: LoadTranResult, dut: str):
    return plot_edges(
        res.edges,
        ref_label=f"DIO {res.trigger_pin}, scope 2 (V)",
        suptitle=f"Load transient: {dut}  ·  VIN {res.vin:.2f} V  ·  {res.averages} averages  ·  "
        "shaded = settling band",
    )
