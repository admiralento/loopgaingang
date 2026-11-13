"""Load transient: step the load bank between two codes and measure VOUT's response.

The AD2 triggers on scope ref channel, wired to a load bank DIO pin that changes
between the two codes (the same setup as the WaveForms workspaces: Scope 2+ on
DIO 4 / J4). Each edge is captured `averages` times and averaged; the response
repeats on every step, so averaging removes noise without blurring it. The
analysis is the Python port of load_tran_script.js.

Signal path: PSU -> VIN (DC); AD2 DIO -> load bank gates; scope vout channel on
VOUT, scope ref channel on the one DIO pin that changes between the codes (the
trigger). The optional 54621A watches the same two signals as a cross-check.

run() steps:
1. Pick the trigger pin (trigger_pin, else the highest bit that differs between
   code_low and code_high) and
   whether it rises when the load steps up; confirm the wiring.
2. Load bank to code_low, PSU ramps VIN to `vin`, and a quick DC read of VOUT
   checks it is near nominal (a setup issue if not; the run continues).
3. For each edge in `edges`: `averaged_step` toggles the bank between the two
   codes and averages the captures; `settling_analysis` gives nominal, peak
   deviation, settling time (to within settle_fraction of the peak deviation)
   and pre-step noise; `capture_issues` checks the data (step present, ref
   swing = dio_high_level, VOUT near nominal).
4. Load bank off. The PSU stays on until the bench shuts down.

Step currents in the title / CSV are the bank's resistor currents at the
nominal VOUT (what the step should be), not a measurement.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np

from ldo_bench import wiring as w
from ldo_bench.analysis import SettlingResult, settling_analysis
from ldo_bench.bench import Bench
from ldo_bench.config import AnalysisConfig, LoadTranConfig
from ldo_bench.instruments.base import ChannelSetup, InstrumentError
from ldo_bench.procedures.transient import (
    XCheck,
    XScopeSetup,
    analyse_xcheck,
    averaged_step,
    capture_issues,
    edge_report,
    plot_edges,
    xcheck_csv,
    xcheck_row,
)

log = logging.getLogger(__name__)

NAME = "load-tran"


@dataclass
class EdgeResult:
    """One load step (up or down), averaged, with its settling metrics."""

    edge: str  # "rising" = load stepped up (code_low -> code_high)
    code_from: int  # load bank code before / after the step
    code_to: int
    i_from: float  # A, bank current for those codes at the nominal VOUT
    i_to: float
    metrics: SettlingResult
    t: np.ndarray  # s, 0 at the step
    vout: np.ndarray  # V, averaged VOUT
    vout_smoothed: np.ndarray  # V, VOUT after glitch removal and the moving average
    ref: np.ndarray  # V, the trigger DIO pin as the scope saw it
    trigger_level: float | None = None  # V, the AD2 trigger level (drawn on the plot)
    issues: list[str] = field(default_factory=list)  # setup problems: non-empty = not a valid edge
    vout_channel: int = 1  # AD2 scope channel on VOUT (for the plot label)
    xcheck: XCheck | None = None  # the 54621A's view of the same edge, if enabled

    @property
    def title(self) -> str:
        word = "Load step up" if self.edge == "rising" else "Load step down"
        # currents are the bank's at the nominal VOUT: what this step should be
        mA = f"{self.i_from * 1e3:.0f} → {self.i_to * 1e3:.0f} mA"
        return f"{word}: codes {self.code_from} → {self.code_to} ({mA})"


@dataclass
class LoadTranResult:
    """The whole load-transient run: one EdgeResult per edge in load_tran.edges.

    `issues` combines the setup issues with each edge's own (prefixed by the edge); any issue
    makes the run invalid. `rows` is data.csv (one row per edge), `extra_csvs` the waveforms.
    """

    vin: float  # V, PSU setpoint on VIN
    trigger_pin: int  # DIO pin on the scope ref channel
    averages: int  # captures averaged per edge
    edges: list[EdgeResult]
    vout_nominal: float = 1.0  # V, dut.vout_nominal (drawn on invalid plots)
    setup_issues: list[str] = field(default_factory=list)  # found before any capture
    plot_window: float | None = None  # s after the step shown in plot.png (None = automatic)
    supply_current: dict | None = None  # supply readback at code_low / code_high (A), see _supply_current

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
                    "valid": not e.issues,
                    "issues": " | ".join(e.issues),
                    **xcheck_row(e.xcheck),
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
        } | {
            f"waveform_xcheck_{e.edge}.csv": xcheck_csv(e.xcheck) for e in self.edges if e.xcheck is not None
        }

    def summary(self) -> dict:
        xcheck = {e.edge: xcheck_row(e.xcheck) for e in self.edges if e.xcheck is not None}
        return {
            "xcheck": xcheck,
            "valid": not self.issues,
            "issues": self.issues,
            "vin": self.vin,
            "trigger_pin": self.trigger_pin,
            "averages": self.averages,
            "supply_current": self.supply_current,
            "edges": {
                e.edge: {**e.metrics.as_dict(), "i_from": e.i_from, "i_to": e.i_to} for e in self.edges
            },
        }

    def report(self) -> str:
        lines = [f"Load transient at VIN = {self.vin:.3f} V ({self.averages} averages per edge):"]
        for e in self.edges:
            lines += edge_report(e)
        return "\n".join(lines)


def _supply_current(bench: Bench, lt: LoadTranConfig) -> tuple[dict, str | None]:
    """Supply current at code_low and code_high, and a setup problem if it doesn't rise as it should.

    The rise must match what the bank draws between the two codes at the nominal VOUT, within
    loadbank_check's tolerances (the difference cancels the readback offset and the LDO's own
    current). With VIN not reaching the board, or the load bank not connected, it barely moves:
    otherwise a missing VIN only shows up as "VOUT missing" (bench, 2026-11-10). Leaves the bank
    at code_low. Returns ({i_low_a, i_high_a, expected_rise_a} in A, issue or None).
    """
    bank, lc, v_nom = bench.loadbank, bench.cfg.loadbank_check, bench.cfg.dut.vout_nominal
    readings = {}
    for name, code in (("i_high_a", lt.code_high), ("i_low_a", lt.code_low)):
        bank.set(code)
        bench.sleep(bench.cfg.psu.settle_time)
        readings[name] = float(bench.psu.measure_current())
    expected = bank.current(lt.code_high, v_nom) - bank.current(lt.code_low, v_nom)
    out = {"i_low_a": readings["i_low_a"], "i_high_a": readings["i_high_a"], "expected_rise_a": expected}
    seen = readings["i_high_a"] - readings["i_low_a"]
    if abs(seen - expected) <= max(lc.tolerance * abs(expected), lc.abs_tolerance):
        return out, None
    return out, (
        f"supply current {readings['i_low_a'] * 1e3:.1f} mA at code {lt.code_low} and "
        f"{readings['i_high_a'] * 1e3:.1f} mA at code {lt.code_high}: it rose {seen * 1e3:.1f} mA, but "
        f"the load bank should draw {expected * 1e3:.1f} mA more. Is VIN reaching the board (supply "
        "leads, output on) and the load bank wired (DIO pins, AD2 GND)?"
    )


def trigger_setup(bench: Bench, lt: LoadTranConfig) -> tuple[int, bool]:
    """(DIO pin on the ref channel, whether it rises when the load steps up).

    With no trigger_pin configured the load bank picks the highest bit that differs between the codes.
    A configured pin that doesn't change between them is refused: the scope could never trigger.
    """
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
    """Wiring lines shown before the run; `pin` is the trigger DIO from trigger_setup."""
    cfg = bench.cfg
    bit = w.bank_bits(cfg)[cfg.loadbank.dio_pins.index(pin)]
    lines = [
        *w.psu_on_vin(cfg),
        w.scope_vout(cfg, "short ground lead"),
        w.scope(cfg, "ref", bit, note=f"DIO {pin}, the trigger"),
    ]
    if not bench.enable_dio:
        lines += w.enable_static(cfg)
    return [*lines, *w.load_bank(cfg), w.ad2_gnd(cfg)]


def run(bench: Bench, lt: LoadTranConfig, an: AnalysisConfig) -> LoadTranResult:
    """Measure each load edge in lt.edges (see the module docstring for the steps).

    lt: the [load_tran] section (codes, VIN, capture settings). an: the [analysis] settings for
    settling_analysis. Setup problems are recorded in the result, not raised; a trigger pin that
    never moves raises InstrumentError before anything is powered.
    """
    bank, a = bench.loadbank, bench.cfg.ad2
    pin, rises_with_load = trigger_setup(bench, lt)
    bench.confirm_wiring("load transient", wiring(bench, pin), ref_probe=("dio", pin))
    setups = {
        a.vout_channel: ChannelSetup(a.vout.range, a.vout.offset),
        a.ref_channel: ChannelSetup(a.ref.range, a.ref.offset),
    }
    bench.ldo_enable(False)  # --enable dio only: EN held low while VIN comes up
    bank.set(lt.code_low)
    bench.vin_from_psu(lt.vin)
    bench.ldo_enable(True)
    bench.reference_on()  # --vref ext only
    vout_est, _ = bench.measure_dc(0.05)
    setup_issues = [i for i in [bench.vout_issue(vout_est.mean, source=f"scope {a.vout_channel}")] if i]
    supply, supply_issue = _supply_current(bench, lt)
    if supply_issue:
        setup_issues.append(supply_issue)
    for i in setup_issues:
        log.warning("load transient: %s (running anyway; the data will show it)", i)
    v_nom = bench.cfg.dut.vout_nominal  # step currents are labelled at the nominal VOUT

    results = []
    for edge in lt.edges:
        c_from, c_to = (lt.code_low, lt.code_high) if edge == "rising" else (lt.code_high, lt.code_low)
        # the trigger pin may fall when the load rises (an active-low bank, or a configured pin
        # whose bit turns off), so the scope edge is the pin's direction, not the load's
        pin_rises = rises_with_load if edge == "rising" else not rises_with_load
        trig_edge = "rising" if pin_rises else "falling"
        sc = averaged_step(
            bench,
            lt,
            setups=setups,
            trigger_channel=a.ref_channel,
            trigger_level=lt.dio_high_level / 2,
            trigger_edge=trig_edge,
            start=lambda c=c_from: bank.set(c),
            step=lambda c=c_to: bank.set(c),
            label=f"load step {edge}",
            # DIO edge at a quarter of the screen height, centred on the trigger level
            xscope=XScopeSetup(ref_range=4 * lt.dio_high_level, ref_offset=lt.dio_high_level / 2),
        )
        cap, trig_issues = sc.cap, sc.issues
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
        issues = (
            setup_issues
            + trig_issues
            + capture_issues(bench, m, cap, ref, expected_swing=lt.dio_high_level, ref_desc=f"DIO {pin}")
        )
        issues = list(dict.fromkeys(issues))  # same problem reported once
        # time axis: from the step found in the data, else from where the scope was set to trigger
        i0 = m.step_index if m.step_index >= 0 else cap.trigger_index
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
                issues,
                a.vout_channel,
                analyse_xcheck(
                    bench, sc, an, trig_edge, expected_swing=lt.dio_high_level, ref_desc=f"DIO {pin}"
                ),
            )
        )
        if issues:
            log.warning("load step %s: %d setup issue(s), see the plot", edge, len(issues))
        else:
            log.info("%s: peak %+.2f mV, settling %.2f us", edge, m.deviation * 1e3, m.settling_time * 1e6)
    bench.ldo_enable(False)
    bank.off()
    return LoadTranResult(
        lt.vin, pin, lt.averages, results, bench.cfg.dut.vout_nominal, setup_issues, lt.plot_window, supply
    )


def plot(res: LoadTranResult, dut: str, full: bool = False, per_edge: bool = False):
    """plot.png: one column per edge (trigger DIO on top, VOUT below), zoomed on the step.

    full=True shows the whole capture instead (plot_full.png); per_edge=True sizes each edge's span to
    its own settling time (plot_zoom.png).
    """
    span = ""
    if full and res.edges:
        t = res.edges[0].t * 1e3
        span = f"\nFull capture: {t[0]:.2f} to {t[-1]:.2f} ms around the step"
    elif per_edge:
        span = "\nZoom: each edge's span set by its own settling time (time scales differ)"
    return plot_edges(
        res.edges,
        full=full,
        window=res.plot_window,
        per_edge=per_edge,
        vout_nominal=res.vout_nominal,
        ref_label=f"DIO {res.trigger_pin}, AD2 (V)",
        suptitle=f"Load transient: {dut}  ·  VIN {res.vin:.2f} V  ·  {res.averages} averages  ·  "
        "shaded = settling band" + span,
    )


def extra_plots(res, dut: str) -> dict:
    """Saved next to plot.png: the whole capture window, and each edge zoomed to its own settling time."""
    return {"plot_full.png": plot(res, dut, full=True), "plot_zoom.png": plot(res, dut, per_edge=True)}
