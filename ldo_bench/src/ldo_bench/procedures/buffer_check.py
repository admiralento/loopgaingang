"""buffer-check: the reworked U2 on its own, chip disconnected (first bring-up plan, steps I.1-I.5).

JP4 is open, so U2's output (JP4 pin 1) drives only a 100 Ohm dummy load (~18 mA at 1.8 V, about
what the chip draws at 20 mA load). AD2 scope ref channel reads JP4 pin 1. Run it before line-tran or
psrr: it proves the split supply comes up cleanly and that U2 reaches the 1.7-1.9 V range without
clipping, and it records the W1 settings that put U2's output there.

run() steps:
1. Confirm the wiring. W1 to DC 0 V, then Bench.buffer_rails_on(buffer.v_plus): V- and VS_BUFF
   ramp together with JP4 pin 1 read after every step; outside buffer.vin_window everything
   comes down and the run stops (I.2: within ~65 mV of 0 V, never above 0.3 V).
2. The output at W1 = 0 V (U2's offset).
3. Each of buffer.check_levels (1.70 / 1.80 / 1.85 V): calibrate W1 so JP4 pin 1 lands on it
   (line_tran._calibrate, with buffer.check_peak_max as the hard stop since the chip is off). The
   W1 needed, and the gain dVOUT/dW1 across the levels (I.3; the first plan expects 0.93-0.99).
4. A step from the lowest to the highest level and back (W1 DC steps, triggered on the output):
   peak, and 10-90 % edge times (C9 and U2's ~7 Ohm make these tens of us).
5. A sine, check_sine_dc +/- check_sine_amplitude at check_sine_frequency, free-running: its
   amplitude against W1's (gain at that frequency), and its peak.
6. Bench.buffer_rails_off: W1 to 0 V, rails down together, W1 off last.

Issues (the run is invalid; don't connect the chip): a level not reached, anything on JP4 pin 1
above check_peak_max, the sine clipped, a VIN probe that isn't on JP4 pin 1 (the output doesn't follow
W1). The first plan's I.4 (what W1 does when stopped) isn't tested: the scripts never stop W1 while
U2's rails are up.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np

from ldo_bench import plotting
from ldo_bench.bench import Bench
from ldo_bench.config import AD2_MAX_SAMPLE_RATE, BufferConfig
from ldo_bench.instruments.base import InstrumentError
from ldo_bench.procedures.line_tran import _calibrate

log = logging.getLogger(__name__)

NAME = "buffer-check"
STEP_RATE = 1e6  # Hz: 8192 samples = 8.2 ms, room for C9's ~70 us edges and their settling
GAIN_RANGE = (0.93, 0.99)  # first bring-up plan's expected DC gain (datasheet; a note, not an issue)
FOLLOW_MIN = 0.5  # the output must move at least this fraction of W1's change between the levels


@dataclass
class BufferCheckResult:
    """What U2 did on its own. `rows` (data.csv): one row per DC level."""

    idle_v: float  # V on JP4 pin 1 with W1 at 0 V
    rows: list[dict]  # target_v, w1_v, out_v per level
    gain: float  # dOut/dW1 across the levels
    step: dict  # rising / falling: peak_v, t10_90_s
    sine: dict  # dc_v, w1_amplitude_v, out_amplitude_v, gain, peak_v, frequency_hz
    t_step: np.ndarray = field(default_factory=lambda: np.zeros(0))  # s, the rising-step capture (plot)
    v_step: np.ndarray = field(default_factory=lambda: np.zeros(0))
    t_sine: np.ndarray = field(default_factory=lambda: np.zeros(0))
    v_sine: np.ndarray = field(default_factory=lambda: np.zeros(0))
    peak_max: float = 1.95
    issues: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def summary(self) -> dict:
        return {
            "valid": not self.issues,
            "issues": self.issues,
            "notes": self.notes,
            "idle_v": self.idle_v,
            "levels": self.rows,
            "dc_gain": self.gain,
            "step": self.step,
            "sine": self.sine,
            "peak_max_v": self.peak_max,
        }

    def report(self) -> str:
        head = "U2 buffer check (chip disconnected, JP4 open, 100 Ohm dummy load):"
        lines = [head]
        if self.issues:
            lines += ["  NOT READY: don't connect the chip. Problems:"] + [f"    - {i}" for i in self.issues]
        lines.append(f"  W1 at 0 V     output {self.idle_v * 1e3:+.1f} mV (U2's offset)")
        for r in self.rows:
            lines.append(f"  {r['target_v']:.2f} V        W1 {r['w1_v']:.4f} V -> output {r['out_v']:.4f} V")
        lines.append(f"  DC gain       {self.gain:.3f} (first plan expects {GAIN_RANGE[0]}-{GAIN_RANGE[1]})")
        for e in ("rising", "falling"):
            if e in self.step:
                st = self.step[e]
                floor = 2 / STEP_RATE  # an edge within two samples is the sampling, not the edge
                t = st["t10_90_s"]
                edge = (
                    "n/a"
                    if not np.isfinite(t)
                    else f"under {floor * 1e6:.0f} us"
                    if t <= floor
                    else f"{t * 1e6:.1f} us"
                )
                lines.append(f"  step {e:<8} peak {st['peak_v']:.4f} V, 10-90 % {edge}")
        if self.sine:
            s = self.sine
            lines.append(
                f"  sine          {s['frequency_hz']:g} Hz: {s['out_amplitude_v'] * 1e3:.1f} mV pk for W1 "
                f"{s['w1_amplitude_v'] * 1e3:.1f} mV pk (gain {s['gain']:.3f}), peak {s['peak_v']:.4f} V"
            )
        lines += [f"  note: {n}" for n in self.notes]
        if not self.issues:
            lines.append(
                "  READY: use these W1 settings as a cross-check for line-tran / psrr's own calibration."
            )
        return "\n".join(lines)


def wiring(bench: Bench, bu: BufferConfig) -> list[str]:
    """Wiring lines shown before the run (JAFWAK silkscreen labels)."""
    a, n = bench.cfg.ad2, bench.cfg.psu.channel
    return [
        "CHIP DISCONNECTED: JP4 open (no jumper); nothing on VIN_DC (J7)",
        "100 Ohm dummy load from JP4 pin 1 (U2 output) to GND (TP8)",
        f"PSU OUT{n} + -> VS_BUFF (J4), OUT{n} - -> GND (J11)",
        f"PSU OUT{bu.channel} REVERSED: OUT{bu.channel} + -> GND, OUT{bu.channel} - -> U2 pin 4 (-{bu.v_minus:g} V)",
        f"AD2 W{a.wavegen_channel} -> VIN_FG (J3)",
        f"Scope {a.ref_channel}+ -> JP4 pin 1, {a.ref_channel}- -> GND (TP8)",
    ]


def _edge(t: np.ndarray, v: np.ndarray, rising: bool) -> float:
    """10-90 % time (s) of the step in v (V); NaN if it doesn't cross both levels."""
    n = len(v)
    a, b = float(np.median(v[: n // 10])), float(np.median(v[-n // 10 :]))
    lo, hi = (a + 0.1 * (b - a), a + 0.9 * (b - a))

    def first(level: float) -> float:
        k = np.nonzero(v >= level)[0] if rising else np.nonzero(v <= level)[0]
        return float(t[k[0]]) if len(k) else float("nan")

    return first(hi) - first(lo)


def run(bench: Bench, bu: BufferConfig) -> BufferCheckResult:
    """Check U2 on its own (see the module docstring for the steps and checks)."""
    a = bench.cfg.ad2
    bench.confirm_wiring("U2 buffer check", wiring(bench, bu), ref_probe=("vin", 0), psu_to="rail")
    issues, notes = [], []
    bench.vin_from_wavegen(0.0)
    bench.buffer_rails_on(bu.v_plus)  # raises (rails down) if JP4 pin 1 leaves buffer.vin_window
    idle = bench.measure_vin_fine()
    log.info("buffer-check: W1 0 V -> output %.1f mV", idle * 1e3)

    rows = []
    for level in bu.check_levels:
        w, v, issue = _calibrate(bench, level, limit=bu.check_peak_max)
        rows.append({"target_v": level, "w1_v": w, "out_v": v, "reached": issue is None})
        if issue:
            issues.append(f"{level:.2f} V: {issue}")
        log.info("buffer-check: %.2f V -> W1 %.4f V, output %.4f V", level, w, v)
    ws = np.array([r["w1_v"] for r in rows])
    vs = np.array([r["out_v"] for r in rows])
    gain = float(np.polyfit(ws, vs, 1)[0]) if np.ptp(ws) > 0 else float("nan")
    span = float(np.ptp(bu.check_levels))
    if np.ptp(vs) < FOLLOW_MIN * span:
        issues.insert(
            0,
            f"JP4 pin 1 moved {np.ptp(vs) * 1e3:.0f} mV across levels {span * 1e3:.0f} mV apart: is scope "
            f"{a.ref_channel} on JP4 pin 1, and W{a.wavegen_channel} on VIN_FG (J3)?",
        )
    elif np.isfinite(gain) and not GAIN_RANGE[0] <= gain <= GAIN_RANGE[1]:
        notes.append(f"DC gain {gain:.3f} is outside the first plan's {GAIN_RANGE[0]}-{GAIN_RANGE[1]}")

    step, t_step, v_step = {}, np.zeros(0), np.zeros(0)
    sine, t_sine, v_sine = {}, np.zeros(0), np.zeros(0)
    if issues:  # U2 can't reach the levels, or isn't on the probe: a step or sine would mean nothing
        notes.append("the step and sine weren't run: fix the DC problems first")
    else:
        step, t_step, v_step = _steps(bench, rows, issues)
        sine, t_sine, v_sine = _sine(bench, bu, gain, issues)
    bench.vin_from_wavegen(0.0)

    peaks = (
        [s["peak_v"] for s in step.values()] + ([sine["peak_v"]] if sine else []) + [r["out_v"] for r in rows]
    )
    if max(peaks) > bu.check_peak_max:
        issues.append(
            f"JP4 pin 1 reached {max(peaks):.3f} V, above {bu.check_peak_max} V: fix this before connecting the chip"
        )
    bench.buffer_rails_off()
    for i in issues:
        log.warning("buffer-check: %s", i)
    return BufferCheckResult(
        idle, rows, gain, step, sine, t_step, v_step, t_sine, v_sine, bu.check_peak_max, issues, notes
    )


def _steps(bench: Bench, rows: list[dict], issues: list[str]) -> tuple[dict, np.ndarray, np.ndarray]:
    """W1 steps between the lowest and highest levels, both ways, triggered on JP4 pin 1.

    Returns ({edge: {peak_v, t10_90_s}}, t, v of the rising step). A trigger timeout is an issue;
    a Bench limit refusal in the stimulus is re-raised (a safety stop).
    """
    a = bench.cfg.ad2
    vs = [r["out_v"] for r in rows]
    lo_row, hi_row = rows[int(np.argmin(vs))], rows[int(np.argmax(vs))]
    level = (lo_row["out_v"] + hi_row["out_v"]) / 2
    step, t_step, v_step = {}, np.zeros(0), np.zeros(0)
    for edge, (w_from, w_to) in (
        ("rising", (lo_row["w1_v"], hi_row["w1_v"])),
        ("falling", (hi_row["w1_v"], lo_row["w1_v"])),
    ):
        bench.vin_from_wavegen(w_from)
        bench.sleep(0.05)
        refused: list[InstrumentError] = []

        def stimulus(w=w_to, refused=refused) -> None:
            try:
                bench.vin_from_wavegen(w)
            except InstrumentError as e:
                refused.append(e)
                raise

        try:
            cap = bench.scope.capture(
                sample_rate=STEP_RATE,
                n_samples=8192,
                setups=bench.vin_fine_setups(),
                trigger_channel=a.ref_channel,
                trigger_level=level,
                edge=edge,
                pre_trigger=0.1,
                stimulus=stimulus,
                timeout=2.0,
            )
        except InstrumentError as e:
            if refused:
                raise refused[0] from e
            issues.append(f"the {edge} step never triggered on JP4 pin 1 (scope {a.ref_channel}): {e}")
            continue
        v = cap.channel(a.ref_channel)
        step[edge] = {"peak_v": float(v.max()), "t10_90_s": _edge(cap.time, v, edge == "rising")}
        if edge == "rising":
            t_step, v_step = cap.time, v
    return step, t_step, v_step


def _sine(
    bench: Bench, bu: BufferConfig, gain: float, issues: list[str]
) -> tuple[dict, np.ndarray, np.ndarray]:
    """check_sine_dc +/- check_sine_amplitude on JP4 pin 1 (W1's amplitude scaled by the DC gain).

    Returns (the sine's numbers, t, v). A Bench limit refusal stops the run; a capture failure is an issue.
    """
    a = bench.cfg.ad2
    w_dc, _, cal = _calibrate(bench, bu.check_sine_dc, limit=bu.check_peak_max)
    if cal:
        issues.append(f"sine DC level: {cal}")
        return {}, np.zeros(0), np.zeros(0)
    w_amp = bu.check_sine_amplitude / gain
    bench.vin_sine_from_wavegen(dc=w_dc, amplitude=w_amp, frequency=bu.check_sine_frequency)
    try:
        cap = bench.scope.capture(
            sample_rate=min(AD2_MAX_SAMPLE_RATE, 8192 * bu.check_sine_frequency / 20),  # 20 cycles
            n_samples=8192,
            setups=bench.vin_fine_setups(),
            trigger_channel=a.ref_channel,
            trigger_level=bu.check_sine_dc,
            edge="rising",
            pre_trigger=0.0,
            stimulus=lambda: None,
            timeout=2.0,
            free_running=True,
        )
    except InstrumentError as e:
        issues.append(f"the sine capture failed: {e}")
        return {}, np.zeros(0), np.zeros(0)
    finally:
        bench.vin_from_wavegen(0.0)
    v = cap.channel(a.ref_channel)
    amp = float(np.percentile(v, 99.5) - np.percentile(v, 0.5)) / 2
    sine = {
        "frequency_hz": bu.check_sine_frequency,
        "dc_v": float(np.mean(v)),
        "w1_amplitude_v": w_amp,
        "out_amplitude_v": amp,
        "gain": amp / w_amp,
        "peak_v": float(v.max()),
    }
    return sine, cap.time, v


def plot(res: BufferCheckResult, dut: str):
    """DC transfer (W1 -> output) at the levels, the rising step, and the sine, with the peak limit."""
    fig, (ax_dc, ax_st, ax_si) = plotting.figure(1, 3, size=(13.0, 4.4))
    ws = [r["w1_v"] for r in res.rows]
    vs = [r["out_v"] for r in res.rows]
    ax_dc.plot(ws, vs, "o-", color=plotting.SERIES[0])
    ax_dc.set_xlabel("W1 (V)")
    ax_dc.set_ylabel("JP4 pin 1 (V)")
    ax_dc.set_title(f"DC: gain {res.gain:.3f}", fontsize=10, pad=6)
    for ax, t, v, name in (
        (ax_st, res.t_step, res.v_step, "Rising step"),
        (ax_si, res.t_sine, res.v_sine, "Sine"),
    ):
        if len(v):
            ax.plot(t * 1e6, v, color=plotting.SERIES[1], lw=1.0)
        ax.axhline(
            res.peak_max, color=plotting.CRITICAL, lw=1.0, ls=(0, (4, 3)), label=f"limit {res.peak_max} V"
        )
        ax.set_xlabel("Time (us)")
        ax.set_title(name, fontsize=10, pad=6)
        ax.legend(loc="lower right")
    if res.issues:
        plotting.issue_box(ax_dc, res.issues)
    fig.suptitle(
        ("NOT READY: " if res.issues else "") + f"U2 buffer check: {dut}",
        x=0.01,
        ha="left",
        fontsize=11,
        fontweight="bold",
        color=plotting.CRITICAL if res.issues else plotting.INK,
    )
    return fig
