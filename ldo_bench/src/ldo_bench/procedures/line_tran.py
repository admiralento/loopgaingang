"""line-tran: step VIN with W1 through the buffer (U2) and measure VOUT; also the buffer helpers psrr uses.

W1 drives the buffer's input, the buffer drives VIN, and the PSU powers the buffer's rail, which goes
above the LDO's VIN limit. So nothing goes above dut.vin_max until `interlock` has proven the PSU is
on the rail and VIN follows W1. W1's setpoints are calibrated on VIN measured at the board, so the
steps land on vin_low / vin_high; the scope triggers on VIN. docs/tests.md has the setup.

Two buffer paths, picked by the config:
  - single rail (no [buffer]): interlock = rail at 1 V with W1 off (VIN must stay low), then the
    rail at dut.vin_max with W1 stepped between proof_levels (VIN must follow).
  - split supply ([buffer], JAFWAK's reworked U2): W1 holds U2's input at DC 0 V before either rail
    comes up and is never turned off while they are up (`input_idle` sets 0 V instead). The rails
    ramp together through Bench.buffer_rails_on (VIN checked after every step), then the same
    proof_levels with VS_BUFF at dut.vin_max; Bench.buffer_rails_off at the end.

run(): load code; EN low (--enable dio); interlock; rail to psu_voltage; with --enable dio, W1 to
vin_low then EN high (so calibration sees the LDO's load); calibrate W1 for vin_low and vin_high (an
unreachable level is an issue, VIN over the limit a hard stop); VOUT DC check; each edge averaged,
then settling_analysis and the capture checks as in load-tran, plus the peak per volt of VIN step;
EN low before the rails come down. Without --enable dio the LDO is tied on and EN_AIN follows VIN.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np

from ldo_bench import wiring as w
from ldo_bench.analysis import SettlingResult, settling_analysis
from ldo_bench.bench import Bench
from ldo_bench.config import AnalysisConfig, LineTranConfig
from ldo_bench.instruments.base import InstrumentError
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

NAME = "line-tran"
INTERLOCK_RAIL = 1.0  # V on the buffer rail while checking the PSU isn't on VIN
INTERLOCK_FRACTION = 0.6  # VIN above this fraction of the rail means the PSU is on VIN
CAL_TOLERANCE = 0.005  # V, how close the calibrated VIN levels must land
CAL_ITERATIONS = 4
PROOF_FOLLOW = 0.5  # VIN must move by at least this fraction of the W1 proof step
CAL_MAX_CORRECTION = 0.25  # V: W1 never goes further than this from the target chasing a dead buffer


@dataclass
class EdgeResult:
    """One VIN step (up or down), averaged, with its settling metrics."""

    edge: str  # "rising" = VIN stepped up
    v_from: float  # measured VIN levels (V)
    v_to: float
    metrics: SettlingResult
    t: np.ndarray  # s, 0 at the step
    vout: np.ndarray  # V, averaged VOUT
    vout_smoothed: np.ndarray  # V, VOUT after glitch removal and the moving average
    ref: np.ndarray  # V, VIN at the LDO input (scope ref channel)
    trigger_level: float | None = None  # V, the AD2 trigger level on VIN
    issues: list[str] = field(default_factory=list)  # setup problems: non-empty = not a valid edge
    vout_channel: int = 1  # AD2 scope channel on VOUT (for the plot label)
    xcheck: XCheck | None = None  # the 54621A's view of the same edge, if enabled

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
    """The whole line-transient run: one EdgeResult per edge in line_tran.edges.

    `issues` combines the setup issues (calibration, VOUT check) with each edge's own; any issue
    makes the run invalid.
    """

    rail: float  # V, buffer rail (PSU setpoint)
    load_code: int  # load bank code held during the run
    i_load: float  # A, that code's current at the nominal VOUT
    w1_low: float  # wavegen setpoints after calibration
    w1_high: float
    averages: int  # captures averaged per edge
    edges: list[EdgeResult]
    vout_nominal: float = 1.0  # V, dut.vout_nominal (drawn on invalid plots)
    setup_issues: list[str] = field(default_factory=list)  # found before any capture
    plot_window: float | None = None  # s after the step shown in plot.png (None = automatic)

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
                    **xcheck_row(e.xcheck),
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
        } | {
            f"waveform_xcheck_{e.edge}.csv": xcheck_csv(e.xcheck) for e in self.edges if e.xcheck is not None
        }

    def summary(self) -> dict:
        xcheck = {e.edge: xcheck_row(e.xcheck) for e in self.edges if e.xcheck is not None}
        return {
            "xcheck": xcheck,
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


def buffer_wiring(bench: Bench, load_code: int, note: str = "") -> list[str]:
    """Wiring for line-tran and psrr (split or single-rail buffer), one connection per line."""
    cfg = bench.cfg
    lines = [
        *w.buffer_supply(cfg),
        w.scope_vout(cfg, note),
        w.scope_vin(cfg, "VIN at the LDO input; the trigger"),
    ]
    if (
        not bench.enable_dio
    ):  # tied on: EN_AIN = VIN, so EN steps with VIN (--enable dio: confirm_wiring adds the DIO path)
        lines += w.enable_static(cfg)
    if load_code:
        lines += w.load_bank(cfg, supply_given=cfg.buffer is not None)  # the PSU powers VS_BUFF here
    return [*lines, w.ad2_gnd(cfg)]


def buffer_up(bench: Bench, rail: float) -> None:
    """Buffer rail to `rail` (V): the split supply (W1 at 0 V first) with [buffer], else the single rail."""
    if bench.cfg.buffer is not None:
        bench.vin_from_wavegen(0.0)
        bench.buffer_rails_on(rail)
    else:
        bench.buffer_rail(rail)


def input_idle(bench: Bench) -> None:
    """Stop driving VIN: W1 to DC 0 V with the split buffer (its input must never float), else W1 off."""
    if bench.cfg.buffer is not None:
        bench.vin_from_wavegen(0.0)
    else:
        bench.wavegen_off()


def buffer_down(bench: Bench) -> None:
    """End of a buffer test: the split supply down with W1 at 0 V (W1 off last), else just W1 off."""
    if bench.cfg.buffer is not None:
        bench.buffer_rails_off()
    else:
        bench.wavegen_off()


def wiring(bench: Bench, lt: LineTranConfig) -> list[str]:
    """Wiring lines shown before the run."""
    return buffer_wiring(bench, lt.load_code)


def vin_limit(bench: Bench) -> float:
    """Highest VIN reading (V) allowed: dut.vin_max less the AD2's measurement error there."""
    vmax = bench.cfg.dut.vin_max
    return vmax - bench.cfg.ad2.vin_margin(vmax)


def interlock(bench: Bench, rail: float, proof_levels: tuple[float, float] = (0.1, 0.3)) -> None:
    """Refuse to raise the rail unless the PSU is proven to be on the buffer rail and VIN is measured.

    Two stages, both at rail voltages that are safe for the LDO even if it is wired wrong:

    1. W1 off, rail at INTERLOCK_RAIL (1 V). If VIN reads above INTERLOCK_FRACTION of the rail,
       the PSU leads are on VIN: stop.
    2. Rail at min(rail, dut.vin_max), W1 stepped between the two proof levels. VIN must follow W1
       (move by at least PROOF_FOLLOW of the step). It can't if the PSU is on VIN (VIN sits at the
       rail) or the VIN probe is off VIN or floating (VIN reads ~0 V either way), which stage 1
       alone can't tell from a correct bench. Then stop, before the rail goes above vin_max.

    proof_levels must be within what the buffer can drive with its rail at dut.vin_max. Also used
    by psrr. With [buffer] the split-supply version runs instead (_interlock_split).
    """
    if bench.cfg.buffer is not None:
        _interlock_split(bench, rail, proof_levels)
        return
    bench.wavegen_off()
    # stage 1: never above the real rail target, so a low psu_voltage is not exceeded by the check
    v_check = min(INTERLOCK_RAIL, rail)
    bench.buffer_rail(v_check)
    vin = bench.measure_vin_fine()
    log.info("interlock: rail %.2f V with W1 off -> VIN %.3f V", v_check, vin)
    if vin > INTERLOCK_FRACTION * v_check:
        raise InstrumentError(
            f"with the buffer rail at {v_check:.2f} V and W1 off, VIN reads {vin:.3f} V: the PSU looks "
            f"connected to VIN, not to the buffer rail. Stopping before raising it to {rail:.2f} V "
            f"(above dut.vin_max {bench.cfg.dut.vin_max} V). Move the PSU leads to VS_BUFF / VDD_BUF."
        )
    # stage 2: prove VIN follows W1 with the rail no higher than the LDO can take
    v_proof = min(rail, bench.cfg.dut.vin_max)
    bench.buffer_rail(v_proof)
    lo, hi = proof_levels
    seen = []
    for level in (lo, hi):
        bench.vin_from_wavegen(level)
        bench.sleep(0.02)
        seen.append(bench.measure_vin_fine())
    bench.wavegen_off()
    moved = seen[1] - seen[0]
    log.info(
        "interlock: rail %.2f V, W1 %.2f / %.2f V -> VIN %.3f / %.3f V", v_proof, lo, hi, seen[0], seen[1]
    )
    if moved < PROOF_FOLLOW * (hi - lo):
        raise InstrumentError(
            f"with the buffer rail at {v_proof:.2f} V, stepping W1 {lo:.2f} -> {hi:.2f} V moved VIN by only "
            f"{moved * 1e3:.0f} mV ({seen[0]:.3f} -> {seen[1]:.3f} V), so VIN isn't proven to come from the "
            f"buffer. Stopping before raising the rail to {rail:.2f} V. Check: PSU leads on VS_BUFF / VDD_BUF "
            f"(not VIN), scope {bench.cfg.ad2.ref_channel} on VIN, JP4 to the buffer, W{bench.cfg.ad2.wavegen_channel} "
            "on the buffer input. If all of that is right, the buffer can't drive the proof levels at this "
            "rail: adjust proof_levels."
        )


def _interlock_split(bench: Bench, rail: float, proof_levels: tuple[float, float]) -> None:
    """The interlock for the split-supply buffer: rails up to min(rail, dut.vin_max), then the proof.

    1. W1 at DC 0 V, then Bench.buffer_rails_on: VS_BUFF and V- ramp together with VIN read after
       every step. VIN following VS_BUFF (PSU on VIN), going negative (V- reversed onto VIN), or
       not held near 0 V (V- missing, JP4, W1) stops it with the rails down.
    2. The proof levels with VS_BUFF at min(rail, dut.vin_max), as in the single-rail interlock: VIN
       must follow W1 (catches a VIN probe that's off VIN, which reads ~0 V and passes step 1).
       W1 goes back to DC 0 V, never off.
    """
    v_proof = min(rail, bench.cfg.dut.vin_max)
    buffer_up(bench, v_proof)
    lo, hi = proof_levels
    seen = []
    for level in (lo, hi):
        bench.vin_from_wavegen(level)
        bench.sleep(0.02)
        seen.append(bench.measure_vin_fine())
    bench.vin_from_wavegen(0.0)
    moved = seen[1] - seen[0]
    log.info("interlock: VS_BUFF %.2f V, W1 %.2f / %.2f V -> VIN %.3f / %.3f V", v_proof, lo, hi, *seen)
    if moved < PROOF_FOLLOW * (hi - lo):
        bench.buffer_rails_off()
        raise InstrumentError(
            f"with VS_BUFF at {v_proof:.2f} V, stepping W1 {lo:.2f} -> {hi:.2f} V moved VIN by only "
            f"{moved * 1e3:.0f} mV ({seen[0]:.3f} -> {seen[1]:.3f} V), so VIN isn't proven to come from U2. "
            f'Rails are down. Check: scope {bench.cfg.ad2.ref_channel} on J13 pin 1 (INA), JP4 1-2 "FG", '
            f"W{bench.cfg.ad2.wavegen_channel} on VIN_FG (J3). If all of that is right, U2 can't drive the "
            "proof levels at this VS_BUFF: adjust proof_levels."
        )


def calibrate(bench: Bench, target: float, limit: float | None = None) -> tuple[float, float, str | None]:
    """W1 setpoint that puts VIN at `target` through the buffer: (w1, measured VIN, issue or None).

    The correction is capped (CAL_MAX_CORRECTION) so a buffer that isn't driving VIN can't make us
    push W1 up. A VIN reading above dut.vin_max less the AD2's error margin is a hard stop, at
    every setpoint, including the last one.

    Simple fixed-point iteration: W1 += (target - measured VIN), up to CAL_ITERATIONS times, done
    when VIN is within CAL_TOLERANCE. The buffer is unity gain, so a few passes remove its offset.
    W1 starts CAL_MAX_CORRECTION below the target and comes up, so a positive buffer offset can't
    put VIN above the target before the first reading (bench, 2026-11-17: W1 started at a 1.85 V
    target with U2 +65 mV high gave VIN 1.915 V, over dut.vin_max).
    If it doesn't converge, the last setpoint is applied, measured, and the issue says so. Also
    used by psrr for its DC level. `limit` (V) replaces the VIN hard stop: buffer-check, with the
    chip disconnected, uses buffer.check_peak_max. W1 stays within dut.vin_max either way.
    """
    what = (
        f"above dut.vin_max ({bench.cfg.dut.vin_max} V) less the AD2's error margin"
        if limit is None
        else f"above the {limit:.3f} V limit"
    )
    limit = vin_limit(bench) if limit is None else limit

    def apply(w: float) -> float:
        bench.vin_from_wavegen(w)
        bench.sleep(0.02)  # let VIN settle at the new setpoint before measuring
        v = bench.measure_vin_fine()
        if v > limit:
            input_idle(bench)
            raise InstrumentError(f"VIN reached {v:.3f} V with W1 at {w:.3f} V, {what}: stopping")
        return v

    w = max(target - CAL_MAX_CORRECTION, 0.0)  # from below: the first correction lands on the target
    for _ in range(CAL_ITERATIONS):
        vin = apply(w)
        err = target - vin
        if abs(err) <= CAL_TOLERANCE:
            return w, vin, None
        # capped near the target, and never above the DUT's VIN limit (a weak buffer becomes an issue)
        w_max = min(target + CAL_MAX_CORRECTION, bench.cfg.dut.vin_max)
        w = min(max(w + err, target - CAL_MAX_CORRECTION, 0.0), w_max)
    vin = apply(w)  # the setpoint left applied is the one the reported VIN was measured at
    if abs(target - vin) <= CAL_TOLERANCE:
        return w, vin, None
    return (
        w,
        vin,
        f"VIN reached {vin:.3f} V, not {target:.3f} V, with W1 at {w:.3f} V: check the buffer rail "
        "headroom (psu_voltage), JP4, and the W1 -> buffer input wiring",
    )


def run(bench: Bench, lt: LineTranConfig, an: AnalysisConfig) -> LineTranResult:
    """Measure each VIN edge in lt.edges (see the module docstring for the steps).

    lt: the [line_tran] section. an: the [analysis] settings for settling_analysis. The interlock
    and dut.vin_max raise InstrumentError (the run stops); everything else is recorded as issues.
    """
    a = bench.cfg.ad2
    bench.confirm_wiring("line transient", wiring(bench, lt), ref_probe=("vin", 0), psu_to="rail")
    setups = bench.scope_setups()
    bench.loadbank.set(lt.load_code)
    bench.ldo_enable(False)  # --enable dio only: EN held low while the rails and VIN come up
    interlock(bench, lt.psu_voltage, lt.proof_levels)  # must pass before the rail goes above dut.vin_max
    buffer_up(bench, lt.psu_voltage)
    if bench.enable_dio:  # VIN up first, then EN: calibrate with the LDO (and its load) drawing from U2
        bench.vin_from_wavegen(lt.vin_low)
        bench.ldo_enable(True)
    w_lo, v_lo, cal_lo = calibrate(bench, lt.vin_low)
    w_hi, v_hi, cal_hi = calibrate(bench, lt.vin_high)
    log.info("VIN levels %.4f / %.4f V (W1 %.4f / %.4f V)", v_lo, v_hi, w_lo, w_hi)
    bench.vin_from_wavegen(w_lo)
    bench.reference_on()  # --vref ext only: after the interlock and calibration, with VIN up
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
        # rising: low -> high; falling: the same pair reversed. VIN moves the same way as the edge,
        # so `edge` is also the scope trigger direction.
        (w_from, v_from), (w_to, v_to) = ((w_lo, v_lo), (w_hi, v_hi))[:: 1 if edge == "rising" else -1]
        sc = averaged_step(
            bench,
            lt,
            setups=setups,
            trigger_channel=a.ref_channel,
            trigger_level=trig_level,
            trigger_edge=edge,
            start=lambda w=w_from: bench.vin_from_wavegen(w),
            step=lambda w=w_to: bench.vin_from_wavegen(w),
            label=f"VIN step {edge}",
            # VIN step at 1/8 of the screen (at least 0.4 V full scale), centred on the trigger level
            xscope=XScopeSetup(ref_range=max(8 * abs(lt.vin_high - lt.vin_low), 0.4), ref_offset=trig_level),
        )
        cap, trig_issues = sc.cap, sc.issues
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
            EdgeResult(
                edge,
                v_from,
                v_to,
                m,
                t,
                vout,
                smoothed,
                vin,
                trig_level,
                issues,
                a.vout_channel,
                analyse_xcheck(
                    bench, sc, an, edge, expected_swing=abs(lt.vin_high - lt.vin_low), ref_desc="VIN"
                ),
            )
        )
        if issues:
            log.warning("VIN step %s: %d setup issue(s), see the plot", edge, len(issues))
        else:
            log.info("%s: peak %+.2f mV, settling %.2f us", edge, m.deviation * 1e3, m.settling_time * 1e6)

    bench.ldo_enable(False)  # --enable dio only: EN low before the rails come down
    buffer_down(bench)
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
        lt.plot_window,
    )


def plot(res: LineTranResult, dut: str, full: bool = False, per_edge: bool = False):
    """plot.png: one column per edge (VIN on top, VOUT below), zoomed on the step.

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
        ref_label="VIN, AD2 (V)",
        suptitle=f"Line transient: {dut}  ·  {res.i_load * 1e3:.1f} mA load  ·  {res.averages} averages  ·  "
        "shaded = settling band" + span,
    )


def extra_plots(res, dut: str) -> dict:
    """Saved next to plot.png: the whole capture window, and each edge zoomed to its own settling time."""
    return {"plot_full.png": plot(res, dut, full=True), "plot_zoom.png": plot(res, dut, per_edge=True)}
