"""psrr: power-supply rejection vs frequency, with a sine on VIN through the BUF634.

Injection is the line-transient path: AD2 W1 -> VIN_FG / V_SIG SMA -> BUF634 -> VIN (JP4 to the
buffer), PSU on the buffer rail. The same interlock runs first (the PSU must not be on VIN), and the
DC level is calibrated at the board like line-tran's steps. W1 then plays dc +/- amplitude; its peak
is checked against dut.vin_max both before (the bench refuses) and after (measured) every frequency.

At each frequency a `PsrrMeter` returns the VIN and VOUT phasors. The default, `Ad2SineFit`, takes
free-running AD2 captures of both channels and least-squares fits a sine at the injected frequency
(a lock-in): VOUT/VIN is averaged coherently over `averages` captures, so noise averages down and
the phase is kept. What the fit can't explain sets a floor; a VOUT amplitude under it means the
PSRR is at least the value plotted (hollow markers, "≥"). The AD2's 14-bit / ~0.5 mV-noise inputs
reach roughly 70-80 dB here. A spectrum analyzer on VOUT can take over that end: write another
PsrrMeter that reads VOUT's amplitude from it (VIN still from AD2 scope 2).

PSRR (dB) = 20 log10(|VIN| / |VOUT|), positive = rejection.

run() steps:
1. Confirm the wiring, set the load code, interlock (line_tran._interlock), buffer rail to
   psu_voltage, calibrate W1's DC level so VIN = psrr.vin at the board (line_tran._calibrate).
2. VOUT DC check (setup issue if not near nominal).
3. For each frequency: W1 plays dc +/- amplitude, wait settle_cycles periods (at least 50 ms),
   measure. If VIN's amplitude is off by more than level_tolerance (buffer / board roll-off at
   high frequency), W1's amplitude is scaled to compensate (at most 2 x the target, never past
   the wavegen / vin_max limit) and the point is measured again. After every measurement the
   measured VIN peak is checked against dut.vin_max + PEAK_MARGIN (hard stop).
4. W1 off, load bank off.

Validity check per point: VIN must carry at least MIN_INJECTION of the target amplitude,
else the sine isn't reaching VIN (setup issue). Notes (run still valid): VOUT following VIN
(< 3 dB) at the lowest valid frequency, which suggests dropout or a probe on VIN; and any
floor-limited points.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Protocol

import numpy as np

from ldo_bench import plotting
from ldo_bench.analysis import sine_fit
from ldo_bench.bench import Bench
from ldo_bench.config import AD2_WAVEGEN_MAX_V, PsrrConfig
from ldo_bench.instruments.base import ChannelSetup, InstrumentError
from ldo_bench.procedures.line_tran import _calibrate, _interlock

log = logging.getLogger(__name__)

NAME = "psrr"
FLOOR_SIGMA = 3.0  # a VOUT phasor smaller than this many noise sigmas counts as "not measured"
MIN_INJECTION = 0.3  # VIN amplitude below this fraction of the target: the sine isn't reaching VIN
PEAK_MARGIN = 0.01  # V over dut.vin_max that stops the run
SUMMARY_FREQS = (100.0, 1e3, 10e3, 100e3, 1e6)  # Hz, reported in summary.json when measured nearby


@dataclass
class Tone:
    """One frequency as a meter saw it. Phasors share a phase reference (VIN's), amplitudes are peak."""

    vin: complex
    vout: complex
    vin_floor: float  # V peak: smallest amplitude the meter resolves on each channel
    vout_floor: float
    vin_dc: float  # V, VIN's DC level during the measurement
    sample_rate: float = float("nan")  # Hz, of the captures (if the meter has one)
    cycles: float = float("nan")  # sine periods per capture


class PsrrMeter(Protocol):
    """Anything that can measure the VIN and VOUT tones at one frequency (W1 is already playing)."""

    name: str  # shown in the report and plot title

    def measure(self, frequency: float) -> Tone: ...


class Ad2SineFit:
    """VIN (scope ref channel) and VOUT (scope vout channel) from the same AD2 captures.

    Each capture is fitted separately; the transfer VOUT/VIN is averaged as a complex number over
    the captures (coherent averaging: noise in VOUT averages down by sqrt(averages), the phase is
    kept). The floors are FLOOR_SIGMA x the fit's amplitude uncertainty, also / sqrt(averages).
    """

    name = "AD2 sine fit"

    def __init__(self, bench: Bench, ps: PsrrConfig) -> None:
        self.bench, self.ps = bench, ps
        a = bench.cfg.ad2
        # VIN's highest point, allowing for levelling up to 2 x the target amplitude
        peak = ps.vin + 2 * ps.amplitude
        # the 5 V (+/-2.5 V) range has 10x the resolution of the 50 V one: use it when VIN fits
        vin_range = 5.0 if peak < 2.4 else 50.0
        vout_range = 5.0 if bench.cfg.dut.vout_nominal < 2.4 else 50.0
        self.setups = {
            a.vout_channel: ChannelSetup(vout_range, 0.0),
            a.ref_channel: ChannelSetup(vin_range, 0.0),
        }

    def measure(self, frequency: float) -> Tone:
        """Take ps.averages free-running captures and fit the sine at `frequency` on both channels."""
        b, ps, a = self.bench, self.ps, self.bench.cfg.ad2
        # ps.cycles periods per capture, unless that needs more than max_sample_rate (then more cycles)
        rate = min(ps.max_sample_rate, ps.n_samples * frequency / ps.cycles)
        h, vin_amp, sig_in, sig_out, dc = [], [], [], [], []
        cap = None
        for _ in range(ps.averages):
            cap = b.scope.capture(
                sample_rate=rate,
                n_samples=ps.n_samples,
                setups=self.setups,
                trigger_channel=a.ref_channel,
                trigger_level=ps.vin,
                edge="rising",
                pre_trigger=0.0,
                stimulus=lambda: None,
                timeout=ps.timeout,
                force=True,  # free-running: the ratio needs no trigger
            )
            fin = sine_fit(cap.channel(a.ref_channel), cap.sample_rate, frequency)
            fout = sine_fit(cap.channel(a.vout_channel), cap.sample_rate, frequency)
            h.append(fout.phasor / fin.phasor if fin.amplitude > 0 else complex("nan"))
            vin_amp.append(fin.amplitude)
            sig_in.append(fin.amplitude_noise)
            sig_out.append(fout.amplitude_noise)
            dc.append(fin.dc)
        k = len(h)
        vin = float(np.mean(vin_amp))
        n = len(cap.ch1)
        # VIN is the phase reference (real); VOUT = averaged transfer x VIN amplitude
        return Tone(
            vin=complex(vin, 0.0),
            vout=complex(np.mean(h)) * vin,
            vin_floor=FLOOR_SIGMA * float(np.mean(sig_in)) / math.sqrt(k),
            vout_floor=FLOOR_SIGMA * float(np.mean(sig_out)) / math.sqrt(k),
            vin_dc=float(np.mean(dc)),
            sample_rate=cap.sample_rate,
            cycles=frequency * n / cap.sample_rate,
        )


@dataclass
class PsrrPoint:
    """One frequency's result. Amplitudes in V peak, PSRR in dB."""

    frequency: float  # Hz
    tone: Tone
    w1_amplitude: float  # what W1 played (after levelling)
    issues: list[str] = field(default_factory=list)  # setup problems at this frequency

    @property
    def vin_amp(self) -> float:
        """VIN amplitude (V peak)."""
        return abs(self.tone.vin)

    @property
    def vout_amp(self) -> float:
        """VOUT amplitude at the injected frequency (V peak)."""
        return abs(self.tone.vout)

    @property
    def limited(self) -> bool:
        """VOUT is under the meter's floor: the PSRR is at least `floor_db`."""
        return not self.vout_amp > self.tone.vout_floor  # `not >`: a NaN amplitude counts as limited

    @property
    def floor_db(self) -> float:
        """Most rejection this point could show (VIN amplitude over the VOUT floor)."""
        if self.vin_amp <= 0 or self.tone.vout_floor <= 0:
            return float("nan")
        return 20 * math.log10(self.vin_amp / self.tone.vout_floor)

    @property
    def psrr_db(self) -> float:
        """Measured rejection; for a floor-limited point, the bound it's at least."""
        if self.limited:
            return self.floor_db
        if self.vin_amp <= 0:
            return float("nan")
        return 20 * math.log10(self.vin_amp / self.vout_amp)

    @property
    def phase_deg(self) -> float:
        """Phase of VOUT relative to VIN (nan when floor-limited or the meter has no phase)."""
        if self.limited or self.vin_amp <= 0 or not np.isfinite(self.tone.vout.real):
            return float("nan")
        return math.degrees(np.angle(self.tone.vout / self.tone.vin))


@dataclass
class PsrrResult:
    """The whole PSRR sweep. `issues` = setup issues plus every point's (each listed once)."""

    rail: float  # V, buffer rail (PSU setpoint)
    vin_dc: float  # measured at the board after calibration
    w1_dc: float  # V, W1's DC setpoint after calibration
    amplitude: float  # target VIN amplitude (V peak)
    load_code: int  # load bank code held during the sweep
    i_load: float  # A, that code's current at the nominal VOUT
    averages: int  # captures per frequency
    meter: str  # the PsrrMeter's name
    points: list[PsrrPoint]
    vout_nominal: float  # V
    setup_issues: list[str] = field(default_factory=list)  # calibration / VOUT problems
    notes: list[str] = field(default_factory=list)  # findings that don't invalidate the run

    @property
    def issues(self) -> list[str]:
        out = list(self.setup_issues)
        for p in self.points:
            out += [i for i in p.issues if i not in out]
        return out

    @property
    def rows(self) -> list[dict]:
        return [
            {
                "frequency_hz": p.frequency,
                "psrr_db": p.psrr_db,
                "floor_limited": p.limited,
                "psrr_floor_db": p.floor_db,
                "phase_deg": p.phase_deg,
                "vin_amp_v": p.vin_amp,
                "vout_amp_v": p.vout_amp,
                "vout_floor_v": p.tone.vout_floor,
                "vin_dc_v": p.tone.vin_dc,
                "w1_amp_v": p.w1_amplitude,
                "sample_rate_hz": p.tone.sample_rate,
                "cycles": p.tone.cycles,
                "load_code": self.load_code,
                "valid": not p.issues,
                "issues": " | ".join(p.issues),
            }
            for p in self.points
        ]

    def at(self, f: float) -> PsrrPoint | None:
        """The measured point nearest `f` (Hz) on a log scale, if within 0.2 decades (a factor ~1.6)."""
        if not self.points:
            return None
        p = min(self.points, key=lambda q: abs(math.log10(q.frequency / f)))
        return p if abs(math.log10(p.frequency / f)) < 0.2 else None

    def summary(self) -> dict:
        measured = [p for p in self.points if not p.limited and np.isfinite(p.psrr_db)]
        worst = min(measured, key=lambda p: p.psrr_db) if measured else None
        at = {}
        for f in SUMMARY_FREQS:
            p = self.at(f)
            if p is not None:
                at[f"{f:g}"] = {"frequency": p.frequency, "psrr_db": p.psrr_db, "floor_limited": p.limited}
        return {
            "valid": not self.issues,
            "issues": self.issues,
            "notes": self.notes,
            "meter": self.meter,
            "buffer_rail_v": self.rail,
            "vin_dc_v": self.vin_dc,
            "w1_dc_v": self.w1_dc,
            "amplitude_v": self.amplitude,
            "load_code": self.load_code,
            "i_load_a": self.i_load,
            "averages": self.averages,
            "psrr_at": at,
            "worst": {"frequency": worst.frequency, "psrr_db": worst.psrr_db} if worst else None,
        }

    def report(self) -> str:
        lines = [
            f"PSRR at VIN {self.vin_dc:.3f} V ± {self.amplitude * 1e3:.0f} mV, load {self.i_load * 1e3:.1f} mA "
            f"(code {self.load_code}), {self.meter}, {self.averages} captures per point:"
        ]
        if self.issues:
            lines.append("  NOT A VALID MEASUREMENT (data saved and plotted as measured):")
            lines += [f"    - {i}" for i in self.issues]
        lines.append("      f (Hz)     PSRR (dB)   phase    VIN (mV pk)   VOUT (uV pk)")
        for p in self.points:
            db = f">= {p.psrr_db:5.1f}" if p.limited else f"   {p.psrr_db:5.1f}"
            ph = f"{p.phase_deg:6.0f}" if np.isfinite(p.phase_deg) else "     -"
            lines.append(
                f"  {p.frequency:10.4g}   {db:>9s}   {ph}   {p.vin_amp * 1e3:9.2f}    {p.vout_amp * 1e6:10.1f}"
            )
        lines += [f"  note: {n}" for n in self.notes]
        return "\n".join(lines)


def wiring(bench: Bench, ps: PsrrConfig) -> list[str]:
    """Wiring lines shown before the run (the load bank line only if a load code is set)."""
    a, lb = bench.cfg.ad2, bench.cfg.loadbank
    lines = [
        f"PSU OUT{bench.cfg.psu.channel} -> buffer rail VS_BUFF / VDD_BUF and GND. NOT on VIN: "
        "the rail goes above the LDO's VIN limit",
        f"AD2 W{a.wavegen_channel} -> buffer input SMA (VIN_FG / V_SIG); buffer output onto VIN (JP4)",
        f"Scope {a.vout_channel}+ -> VOUT at the output pins, {a.vout_channel}- -> GND (short ground lead)",
        f"Scope {a.ref_channel}+ -> VIN at the LDO input pins, {a.ref_channel}- -> GND",
    ]
    if ps.load_code:
        lines.append(f"DIO {lb.dio_pins} -> load bank bits B0..B{len(lb.dio_pins) - 1}, AD2 GND -> board GND")
    return lines


def _check_peak(bench: Bench, tone: Tone, f: float) -> None:
    """Hard stop: W1 off and InstrumentError if the measured VIN peak (DC + amplitude) is over
    dut.vin_max + PEAK_MARGIN. Backs up the setpoint check in bench.vin_sine_from_wavegen, which
    can't see the buffer's offset or gain.
    """
    peak = tone.vin_dc + abs(tone.vin)
    if peak > bench.cfg.dut.vin_max + PEAK_MARGIN:
        bench.wavegen_off()
        raise InstrumentError(
            f"at {f:g} Hz VIN peaks at {peak:.3f} V, above dut.vin_max ({bench.cfg.dut.vin_max} V): stopped"
        )


def run(bench: Bench, ps: PsrrConfig, meter: PsrrMeter | None = None) -> PsrrResult:
    """Measure PSRR at each of ps.frequencies() (see the module docstring for steps and checks).

    ps: the [psrr] section. meter: how the tones are measured (default Ad2SineFit). The interlock,
    dut.vin_max and the wavegen limit raise InstrumentError; other problems are recorded as issues.
    """
    cfg = bench.cfg
    a = cfg.ad2
    bench.confirm_wiring("PSRR", wiring(bench, ps), ref_probe=("vin", 0), psu_to="rail")
    bench.loadbank.set(ps.load_code)
    _interlock(bench, ps.psu_voltage)
    bench.buffer_rail(ps.psu_voltage)
    w_dc, v_dc, cal = _calibrate(bench, ps.vin)
    vout_dc, _ = bench.measure_dc(0.05)
    setup_issues = [i for i in (cal, bench.vout_issue(vout_dc.mean, source=f"scope {a.vout_channel}")) if i]
    for i in setup_issues:
        log.warning("psrr: %s (running anyway; the data will show it)", i)
    i_load = bench.loadbank.current(ps.load_code, cfg.dut.vout_nominal)
    meter = meter or Ad2SineFit(bench, ps)
    w_limit = min(cfg.dut.vin_max, AD2_WAVEGEN_MAX_V) - w_dc  # largest amplitude W1 may play
    # (the sine's peak, w_dc + amplitude, then stays within what vin_sine_from_wavegen allows)

    amp = min(ps.amplitude, w_limit)
    points = []
    freqs = ps.frequencies()
    for n, f in enumerate(freqs, 1):
        bench.vin_sine_from_wavegen(dc=w_dc, amplitude=amp, frequency=f)
        bench.sleep(max(0.05, ps.settle_cycles / f))  # let the LDO reach steady state at this tone
        tone = meter.measure(f)
        _check_peak(bench, tone, f)
        got = abs(tone.vin)
        # level W1 so VIN keeps the target amplitude (the buffer / board roll off at high f)
        if got > MIN_INJECTION * ps.amplitude and abs(got - ps.amplitude) > ps.level_tolerance * ps.amplitude:
            # scale by target / measured; capped at 2 x target so a bad reading can't push it far
            new = min(amp * ps.amplitude / got, 2 * ps.amplitude, w_limit)
            if abs(new - amp) > 1e-4:  # skip re-measuring for a change under 0.1 mV
                amp = new
                bench.vin_sine_from_wavegen(dc=w_dc, amplitude=amp, frequency=f)
                bench.sleep(max(0.05, ps.settle_cycles / f))
                tone = meter.measure(f)
                _check_peak(bench, tone, f)
                got = abs(tone.vin)
        issues = []
        if got < MIN_INJECTION * ps.amplitude:
            issues.append(
                f"VIN only carries {got * 1e3:.1f} mV of the {ps.amplitude * 1e3:.0f} mV sine: is the buffer "
                f"driving VIN (JP4 to the buffer, W{a.wavegen_channel} on the buffer input SMA) and scope "
                f"{a.ref_channel} on VIN?"
            )
        p = PsrrPoint(f, tone, amp, issues)
        points.append(p)
        db = f">= {p.psrr_db:.1f}" if p.limited else f"{p.psrr_db:.1f}"
        log.info("[%d/%d] %g Hz: PSRR %s dB (VIN %.1f mV pk)", n, len(freqs), f, db, got * 1e3)
    bench.wavegen_off()
    bench.loadbank.off()

    notes = []
    good = [p for p in points if not p.issues]
    if good and not good[0].limited and good[0].psrr_db < 3:
        notes.append(
            f"VOUT follows VIN at {good[0].frequency:g} Hz ({good[0].psrr_db:.1f} dB): the LDO may be in dropout "
            "at this VIN / load, or scope 1 is on VIN"
        )
    if any(p.limited for p in good):
        notes.append(
            "hollow points: VOUT's ripple is under what the AD2 resolves, so the PSRR is at least that value; "
            "a spectrum analyzer on VOUT will measure them"
        )
    return PsrrResult(
        ps.psu_voltage,
        v_dc,
        w_dc,
        ps.amplitude,
        ps.load_code,
        i_load,
        ps.averages,
        getattr(meter, "name", type(meter).__name__),
        points,
        cfg.dut.vout_nominal,
        list(dict.fromkeys(setup_issues)),
        notes,
    )


def plot(res: PsrrResult, dut: str):
    """Top: PSRR (dB) vs frequency, with the measurement limit dashed and floor-limited points as
    hollow triangles ("at least"). Bottom: VIN, VOUT and VOUT floor amplitudes (mV peak, log).
    """
    fig, (ax, ax_a) = plotting.figure(
        2, 1, size=(8.0, 6.4), sharex=True, gridspec_kw={"height_ratios": [1.6, 1]}
    )
    valid = not res.issues
    f = np.array([p.frequency for p in res.points])
    db = np.array([p.psrr_db for p in res.points])
    lim = np.array([p.limited for p in res.points], bool)
    floor_db = np.array([p.floor_db for p in res.points])
    c = plotting.SERIES[0]
    ax.plot(f, floor_db, color=plotting.MUTED, lw=1.0, ls=(0, (4, 3)), zorder=2, label="measurement limit")
    ax.plot(f[~lim], db[~lim], color=c, lw=1.6, zorder=3)
    ax.plot(f[~lim], db[~lim], "o", ms=5, color=c, zorder=4, label="measured")
    ax.plot(
        f[lim], db[lim], "^", ms=6, mfc=plotting.SURFACE, mec=c, zorder=4, label="≥ (VOUT under the floor)"
    )
    for p in res.points:
        if not p.limited and np.isfinite(p.psrr_db):
            ax.annotate(
                f"{p.psrr_db:.0f}",
                (p.frequency, p.psrr_db),
                xytext=(0, -11),
                textcoords="offset points",
                ha="center",
                fontsize=7,
                color=plotting.INK_2,
            )
    ax.set_xscale("log")
    ax.set_ylabel("PSRR (dB)")
    top = np.nanmax(np.concatenate([floor_db, db])) if len(f) and np.isfinite(floor_db).any() else None
    if top is not None and np.isfinite(top):
        ax.set_ylim(top=top + 10)  # room for the legend above the limit line
    ax.legend(loc="upper right", ncols=3)
    if not valid:
        plotting.issue_box(ax, res.issues)

    vin = np.array([p.vin_amp for p in res.points]) * 1e3
    vout = np.array([p.vout_amp for p in res.points]) * 1e3
    vfl = np.array([p.tone.vout_floor for p in res.points]) * 1e3
    ax_a.plot(f, vin, "o-", ms=4, lw=1.2, color=plotting.SERIES[1], label="VIN (scope 2)")
    ax_a.plot(f, vout, "o-", ms=4, lw=1.2, color=c, label="VOUT")
    ax_a.plot(f, vfl, color=plotting.MUTED, lw=1.0, ls=(0, (4, 3)), label="VOUT floor")
    ax_a.set_yscale("log")
    ax_a.set_ylabel("Amplitude (mV peak)")
    ax_a.set_xlabel("Frequency (Hz)")
    ax_a.legend(loc="best")
    title = (
        f"PSRR: {dut}  ·  VIN {res.vin_dc:.2f} V ± {res.amplitude * 1e3:.0f} mV  ·  "
        f"{res.i_load * 1e3:.1f} mA load  ·  {res.meter}"
    )
    if not valid:
        title = "SETUP PROBLEM, not a valid measurement\n" + title
    fig.suptitle(
        title,
        x=0.01,
        ha="left",
        fontsize=11,
        fontweight="bold",
        color=plotting.CRITICAL if not valid else plotting.INK,
    )
    return fig
