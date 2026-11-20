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
1. Confirm the wiring, set the load code, interlock (line_tran.interlock), buffer rail to
   psu_voltage, calibrate W1's DC level so VIN = psrr.vin at the board (line_tran.calibrate).
2. VOUT DC check (setup issue if not near nominal).
3. For each frequency: W1 plays dc +/- amplitude, wait settle_cycles periods (at least 50 ms),
   measure. If VIN's amplitude is off by more than level_tolerance (buffer / board roll-off at
   high frequency), W1's amplitude is scaled to compensate (at most 2 x the target, never past
   the wavegen / vin_max limit) and the point is measured again. After every measurement the
   measured VIN peak is checked against dut.vin_max less the AD2's error margin (hard stop).
4. W1 off (with [buffer]: the split supply down first, W1 off last), load bank off.

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
from ldo_bench.analysis import multi_sine_fit, sine_fit
from ldo_bench.bench import Bench
from ldo_bench.config import AD2_WAVEGEN_MAX_V, PsrrConfig
from ldo_bench.instruments.base import ChannelSetup, InstrumentError
from ldo_bench.procedures import INVALID
from ldo_bench.procedures.line_tran import (
    buffer_down,
    buffer_up,
    buffer_wiring,
    calibrate,
    input_idle,
    interlock,
    vin_limit,
)

log = logging.getLogger(__name__)

NAME = "psrr"
FLOOR_SIGMA = 4.0  # a VOUT phasor smaller than this many noise sigmas counts as "not measured"
# (4 sigma: pure noise passes as "measured" with p = exp(-8) ~ 0.03 % per point; 3 sigma was 1.1 %)
OFF_BINS = 3  # off-frequency noise fits this many frequency steps (1 / capture length) either side
MIN_INJECTION = 0.3  # VIN amplitude below this fraction of the target: the sine isn't reaching VIN
SA_MIN_VIN = (
    2e-3  # V peak: on the 8591E's points a smaller tone is still fine (PSRR is a ratio), down to this
)
LEVEL_STEPS = 6  # levelling passes per frequency
LEVEL_MAX_STEP = 1.5  # W1's amplitude grows at most this much per pass (and per new frequency, see run())
LEVEL_MIN_VIN = 1e-3  # V peak: below this VIN reading the gain is too uncertain to level from
SUMMARY_FREQS = (100.0, 1e3, 10e3, 100e3, 1e6)  # Hz, reported in summary.json when measured nearby
# --- meter = sa (HP 8591E on VOUT through U4 -> R14 47 ohm -> J9 -> DC block -> coax) -------------------
SA_RBW_RANGE = (300.0, 3e3)  # Hz: RBW = f / 10, within this (300 Hz is the bench unit's narrowest)
SA_SPAN_RBWS = 20  # span = this many RBWs around the tone
SA_FLOOR_RBWS = 6  # the noise floor is read this many RBWs either side of the tone
SA_FLOOR_DB = 6.0  # a tone counts as measured only this far above that noise reading
SA_REF_LEVEL_DBM = -20.0  # 31.6 mV peak into 50 ohm: VOUT's ripple through U4 stays well under it
# U4 (unity) driving the analyzer's 50 ohm through R14 (47 ohm): what the path should measure, for a note only
SA_PATH_EXPECTED_DB = 20 * math.log10(50 / 97)
SA_PATH_NOTE_DB = 3.0  # a measured path gain this far from the expected one gets a note


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
    phase_known: bool = True  # False when VOUT's amplitude came from a meter with no phase (the 8591E)
    vout_meter: str = "AD2"  # what measured VOUT


class PsrrMeter(Protocol):
    """Anything that can measure the VIN and VOUT tones at one frequency (W1 is already playing)."""

    name: str  # shown in the report and plot title

    def measure(self, frequency: float) -> Tone: ...


class Ad2SineFit:
    """VIN (scope ref channel) and VOUT (scope vout channel) from the same AD2 captures.

    Each capture is fitted separately; the transfer VOUT/VIN is averaged as a complex number over
    the captures (coherent averaging: noise in VOUT averages down by sqrt(averages), the phase is
    kept).

    The floor is FLOOR_SIGMA x the largest of three estimates of the noise on one quadrature of
    the averaged phasor, so that a tone near the test frequency (mains hum, 1/f noise) can't pass
    for PSRR:
    - white: the fit's residual, sigma * sqrt(2 / n) (assumes the residual is white);
    - spread: how much VOUT/VIN scatters from capture to capture (free-running captures see an
      unlocked tone at a different phase each time);
    - off-frequency: tones fitted OFF_BINS frequency steps either side, jointly with the test tone.
    Each is divided by sqrt(averages). Pickup locked to VIN (crosstalk, ground coupling) shows in
    none of them: it is the one floor this can't see (a spectrum analyzer or a shorted-input
    calibration run would).
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

    def measure(self, frequency: float, averages: int | None = None) -> Tone:
        """Take `averages` (default ps.averages) free-running captures and fit the sine at `frequency`
        on both channels."""
        b, ps, a = self.bench, self.ps, self.bench.cfg.ad2
        averages = averages or ps.averages
        # ps.cycles periods per capture, unless that needs more than max_sample_rate (then more cycles)
        rate = min(ps.max_sample_rate, ps.n_samples * frequency / ps.cycles)
        h, vin_amp, sig_in, sig_out, off, dc = [], [], [], [], [], []
        cap = None
        for _ in range(averages):
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
            vin_x, vout_x = cap.channel(a.ref_channel), cap.channel(a.vout_channel)
            fin = sine_fit(vin_x, cap.sample_rate, frequency)
            fout = sine_fit(vout_x, cap.sample_rate, frequency)
            h.append(fout.phasor / fin.phasor if fin.amplitude > 0 else complex("nan"))
            vin_amp.append(fin.amplitude)
            sig_in.append(fin.amplitude_noise)
            sig_out.append(fout.amplitude_noise)
            dc.append(fin.dc)
            step = OFF_BINS * cap.sample_rate / len(vout_x)  # Hz
            near = [f for f in (frequency - step, frequency + step) if f > 0]
            fitted = multi_sine_fit(vout_x, cap.sample_rate, [frequency, *near])
            off += [abs(p) ** 2 / 2 for p in fitted[1:]]  # per-quadrature variance of each off tone
        k = len(h)
        vin = float(np.mean(vin_amp))
        n = len(cap.ch1)
        hs = np.array(h)
        white = float(np.mean(sig_out))
        spread = float(np.sqrt(np.sum(np.abs(hs - hs.mean()) ** 2) / (2 * (k - 1)))) * vin if k > 1 else 0.0
        near_noise = float(np.sqrt(np.mean(off))) if off else 0.0
        # VIN is the phase reference (real); VOUT = averaged transfer x VIN amplitude
        return Tone(
            vin=complex(vin, 0.0),
            vout=complex(hs.mean()) * vin,
            vin_floor=FLOOR_SIGMA * float(np.mean(sig_in)) / math.sqrt(k),
            vout_floor=FLOOR_SIGMA * max(white, spread, near_noise) / math.sqrt(k),
            vin_dc=float(np.mean(dc)),
            sample_rate=cap.sample_rate,
            cycles=frequency * n / cap.sample_rate,
        )


@dataclass
class SaReading:
    """What the 8591E read at one frequency, before the path gain is known (V peak at its input)."""

    vpeak: float
    floor_vpeak: float
    rbw: float  # Hz
    peak_frequency: float  # Hz, where the marker found the tone
    dbm: float  # the tone as the analyzer read it (dBm into 50 ohm)

    @property
    def margin_db(self) -> float:
        """How far the tone is above the analyzer's floor (dB)."""
        return (
            20 * math.log10(self.vpeak / self.floor_vpeak)
            if self.vpeak > 0 and self.floor_vpeak > 0
            else math.nan
        )


def _margin_db(amp: float, floor: float) -> float:
    """amp over floor (dB); NaN if either isn't positive."""
    return 20 * math.log10(amp / floor) if amp > 0 and floor > 0 else math.nan


class SaPsrrMeter:
    """VIN from the AD2 (sine fit, as Ad2SineFit); VOUT from the HP 8591E from psrr.sa_from up.

    The analyzer sees VOUT through U4, R14 (47 ohm), J9, the DC block and the coax, so its reading is
    VOUT x the path gain. That gain is measured in the run, never assumed: from sa_from to sa_cal_to
    the AD2 also measures VOUT, and where both readings are sa_cal_margin_db above their floors their
    ratio is a path-gain point. finish() takes the median, checks the points agree within
    sa_path_tolerance_db, and converts every analyzer reading to VOUT. Below sa_from (the analyzer
    starts at 9 kHz) VOUT is the AD2's. The analyzer gives no phase.
    """

    def __init__(self, bench: Bench, ps: PsrrConfig) -> None:
        if bench.sa is None:
            raise InstrumentError("psrr --meter sa needs the HP 8591E open ([sa], found on GPIB)")
        self.bench, self.ps, self.sa = bench, ps, bench.sa
        self.ad2 = Ad2SineFit(bench, ps)
        self.name = "AD2 (VIN) + HP 8591E (VOUT)"
        self.readings: dict[float, SaReading] = {}  # by frequency; a re-levelled point overwrites its first
        self.path_gain: float | None = None  # V/V, once finish() has measured it
        self.path_points: list[tuple[float, float]] = []  # (Hz, V/V) the gain was measured from
        self.sa.preset()

    def measure(self, frequency: float) -> Tone:
        """The AD2's tone (VIN, and VOUT as it sees it); from sa_from up also the analyzer's reading."""
        overlap = self.ps.sa_from <= frequency <= self.ps.sa_cal_to
        # where the path gain is measured the AD2's VOUT must clear its floor: more captures there
        tone = self.ad2.measure(frequency, self.ps.sa_cal_averages if overlap else None)
        if frequency >= self.ps.sa_from:
            self.readings[frequency] = self._read(frequency)
        return tone

    def _read(self, f: float) -> SaReading:
        lo, hi = SA_RBW_RANGE
        want = min(max(f / 10, lo), hi)
        rbw = self.sa.tune(center=f, span=SA_SPAN_RBWS * want, rbw=want, ref_level_dbm=SA_REF_LEVEL_DBM)
        self.sa.sweep()
        pk = self.sa.peak()
        # the tone: the peak if it's at f (within 2 RBWs), else whatever is at f (then it's under the noise)
        tone = pk if abs(pk.frequency - f) <= 2 * rbw else self.sa.marker_at(f)
        noise = max(
            self.sa.marker_at(f + SA_FLOOR_RBWS * rbw).vpeak, self.sa.marker_at(f - SA_FLOOR_RBWS * rbw).vpeak
        )
        return SaReading(tone.vpeak, noise * 10 ** (SA_FLOOR_DB / 20), rbw, tone.frequency, tone.dbm)

    def finish(self, points: list[PsrrPoint]) -> tuple[list[str], list[str]]:
        """Measure the path gain from the overlap, convert the analyzer's points to VOUT. (issues, notes)."""
        ps, margin = self.ps, 10 ** (self.ps.sa_cal_margin_db / 20)
        for p in points:  # the analyzer's own readings are kept on every point it read, whatever follows
            p.sa = self.readings.get(p.frequency)
        for p in points:
            if p.frequency in self.readings:
                r = self.readings[p.frequency]
                usable = (
                    not p.issues
                    and p.frequency <= ps.sa_cal_to
                    and p.tone.vout_floor > 0
                    and abs(p.tone.vout) >= margin * p.tone.vout_floor
                    and r.vpeak >= margin * r.floor_vpeak
                )
                if usable:
                    self.path_points.append((p.frequency, r.vpeak / abs(p.tone.vout)))
        sa_points = [p for p in points if p.frequency in self.readings]
        if not self.path_points:
            seen = "; ".join(
                f"{p.frequency:g} Hz: AD2 {_margin_db(abs(p.tone.vout), p.tone.vout_floor):.1f} dB, "
                f"8591E {p.sa.margin_db:.1f} dB"
                for p in sa_points
                if p.frequency <= ps.sa_cal_to
            )
            issue = (
                f"the 8591E's path gain couldn't be measured: no point from {ps.sa_from:g} to {ps.sa_cal_to:g} Hz "
                f"had both the AD2's and the analyzer's VOUT {ps.sa_cal_margin_db:g} dB above their floors "
                f"({seen}). Its points keep the AD2's VOUT (check U4's rails, J10, the J9 coax and the DC block)"
            )
            for p in sa_points:
                p.issues.append(issue)
            return [issue], []
        gains_db = [20 * math.log10(g) for _, g in self.path_points]
        self.path_gain = 10 ** (float(np.median(gains_db)) / 20)
        issues, notes = [], []
        spread = max(gains_db) - min(gains_db)
        if spread > ps.sa_path_tolerance_db:
            issues.append(
                f"the 8591E's path gain varies {spread:.1f} dB across {len(gains_db)} points "
                f"({', '.join(f'{f:g} Hz: {g:+.1f} dB' for (f, _), g in zip(self.path_points, gains_db, strict=True))}): "
                "the path isn't flat (a DC block rolling off at low frequency?), so one gain can't convert the sweep"
            )
            for p in sa_points:
                p.issues.append(issues[0])
        g_db = 20 * math.log10(self.path_gain)
        notes.append(
            f"8591E path gain {g_db:+.2f} dB (median of {len(gains_db)} points, {min(gains_db):+.2f} to "
            f"{max(gains_db):+.2f} dB) from VOUT on both meters at {ps.sa_from:g}-{ps.sa_cal_to:g} Hz; "
            f"assumed the same up to {max(p.frequency for p in sa_points):g} Hz"
        )
        if abs(g_db - SA_PATH_EXPECTED_DB) > SA_PATH_NOTE_DB:
            notes.append(
                f"the path gain is {g_db - SA_PATH_EXPECTED_DB:+.1f} dB from the {SA_PATH_EXPECTED_DB:+.1f} dB that U4 into "
                "R14 + 50 ohm should give: check the J9 coax, the DC block and U4"
            )
        for p in sa_points:
            r, t = self.readings[p.frequency], p.tone
            p.ad2_vout = abs(t.vout)
            p.tone = Tone(
                vin=t.vin,
                vout=complex(r.vpeak / self.path_gain, 0.0),
                vin_floor=t.vin_floor,
                vout_floor=r.floor_vpeak / self.path_gain,
                vin_dc=t.vin_dc,
                sample_rate=t.sample_rate,
                cycles=t.cycles,
                phase_known=False,
                vout_meter="8591E",
            )
        return issues, notes


@dataclass
class PsrrPoint:
    """One frequency's result. Amplitudes in V peak, PSRR in dB."""

    frequency: float  # Hz
    tone: Tone
    w1_amplitude: float  # what W1 played (after levelling)
    issues: list[str] = field(default_factory=list)  # setup problems at this frequency
    ad2_vout: float | None = None  # V peak: the AD2's own VOUT reading where the 8591E measured (cross-check)
    sa: SaReading | None = None  # the 8591E's raw reading at this frequency (meter = sa, from sa_from up)

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
        if (
            self.limited
            or self.vin_amp <= 0
            or not self.tone.phase_known
            or not np.isfinite(self.tone.vout.real)
        ):
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
    path_gain_db: float | None = None  # meter = sa: the 8591E path's gain, measured from the overlap (dB)

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
                "vout_meter": p.tone.vout_meter,
                "ad2_vout_amp_v": p.ad2_vout if p.ad2_vout is not None else float("nan"),
                # the 8591E's raw reading at its input (before the path gain), saved whatever the path gain did
                "sa_dbm": p.sa.dbm if p.sa else float("nan"),
                "sa_vpeak_v": p.sa.vpeak if p.sa else float("nan"),
                "sa_floor_vpeak_v": p.sa.floor_vpeak if p.sa else float("nan"),
                "sa_margin_db": p.sa.margin_db if p.sa else float("nan"),
                "sa_rbw_hz": p.sa.rbw if p.sa else float("nan"),
                "sa_peak_frequency_hz": p.sa.peak_frequency if p.sa else float("nan"),
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
            "sa_path_gain_db": self.path_gain_db,
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
            lines.append(f"  {INVALID}")
        lines.append("      f (Hz)     PSRR (dB)   phase    VIN (mV pk)   VOUT (uV pk)   VOUT from")
        for p in self.points:
            db = f">= {p.psrr_db:5.1f}" if p.limited else f"   {p.psrr_db:5.1f}"
            ph = f"{p.phase_deg:6.0f}" if np.isfinite(p.phase_deg) else "     -"
            lines.append(
                f"  {p.frequency:10.4g}   {db:>9s}   {ph}   {p.vin_amp * 1e3:9.2f}    {p.vout_amp * 1e6:10.1f}"
                f"   {p.tone.vout_meter}"
                + (f" (AD2 {p.ad2_vout * 1e6:.1f})" if p.ad2_vout is not None else "")
            )
        lines += [f"  note: {n}" for n in self.notes]
        return "\n".join(lines)


def wiring(bench: Bench, ps: PsrrConfig) -> list[str]:
    """Wiring lines shown before the run (with meter = sa, the 8591E's path from VOUT too)."""
    lines = buffer_wiring(bench, ps.load_code, "short ground lead")
    if ps.meter == "sa":
        lines += [
            "U4 (VOUT buffer) on the split supply: V+ on VS_BUFF, V- (pin 4) on -5 V (jumper wires), JP3 open "
            "(BW: low-quiescent mode, keeps the -5 V rail under buffer.current_limit)",
            'Fit J10 "LOAD" (VOUT -> load cell and U4\'s input)',
            "J9 (VOUT_BUFF) -> DC block -> coax -> 8591E INPUT 50 Ω (check the DC block is rated down to 10 kHz)",
        ]
    return lines


def _check_peak(bench: Bench, tone: Tone, f: float) -> None:
    """Hard stop: W1 off and InstrumentError if the measured VIN peak (DC + amplitude) is above
    dut.vin_max less the AD2's error margin (ad2.vin_margin), i.e. if the true peak could be over
    the limit. Backs up the setpoint check in bench.vin_sine_from_wavegen, which can't see the
    buffer's offset or gain.
    """
    peak = tone.vin_dc + abs(tone.vin)
    if peak > vin_limit(bench):
        input_idle(bench)
        raise InstrumentError(
            f"at {f:g} Hz VIN peaks at {peak:.3f} V, above dut.vin_max ({bench.cfg.dut.vin_max} V) "
            "less the AD2's error margin: stopped"
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
    interlock(bench, ps.psu_voltage, ps.proof_levels)
    buffer_up(bench, ps.psu_voltage)
    w_dc, v_dc, cal = calibrate(bench, ps.vin)
    if bench.vref_external:  # after the interlock and calibration, with VIN at its DC level
        bench.vin_from_wavegen(w_dc)
        bench.reference_on()
    vout_dc, _ = bench.measure_dc(0.05)
    setup_issues = [i for i in (cal, bench.vout_issue(vout_dc.mean, source=f"scope {a.vout_channel}")) if i]
    for i in setup_issues:
        log.warning("psrr: %s (running anyway; the data will show it)", i)
    i_load = bench.loadbank.current(ps.load_code, cfg.dut.vout_nominal)
    meter = meter or (SaPsrrMeter(bench, ps) if ps.meter == "sa" else Ad2SineFit(bench, ps))
    # Largest amplitude W1 may play: the W1 peak within the bench's wavegen limit, and the VIN peak
    # (the measured DC level + amplitude, the buffer being unity gain) within vin_max less the AD2's
    # error margin. The second matters when the buffer's offset puts VIN above W1.
    w_limit = min(min(cfg.dut.vin_max, AD2_WAVEGEN_MAX_V) - w_dc, vin_limit(bench) - v_dc)

    amp = min(ps.amplitude, w_limit)  # the first tone as if the buffer passed it at full size
    w1_cap = min(cfg.dut.vin_max, AD2_WAVEGEN_MAX_V) - w_dc  # W1's own peak within its limit
    sa_from = ps.sa_from if isinstance(meter, SaPsrrMeter) else math.inf
    gain = 1.0  # VIN amplitude / W1 amplitude at the last point (the buffer and board roll off)
    points = []
    freqs = ps.frequencies()
    for n, f in enumerate(freqs, 1):
        # carry the amplitude over, but so that even a gain LEVEL_MAX_STEP x the last one keeps VIN in bounds
        amp = min(amp, w1_cap, max(vin_limit(bench) - v_dc, 0.0) / (LEVEL_MAX_STEP * gain))
        bench.vin_sine_from_wavegen(dc=w_dc, amplitude=amp, frequency=f)
        bench.sleep(max(0.05, ps.settle_cycles / f))  # let the LDO reach steady state at this tone
        tone = meter.measure(f)
        _check_peak(bench, tone, f)
        # level W1 so VIN carries the target amplitude, one bounded pass at a time
        for _ in range(LEVEL_STEPS):
            got = abs(tone.vin)
            if got < LEVEL_MIN_VIN or abs(got - ps.amplitude) <= ps.level_tolerance * ps.amplitude:
                break
            room = max(vin_limit(bench) - tone.vin_dc, 0.0)  # VIN peak headroom, from the measured DC
            # the next VIN amplitude, predicted from this one (linear: VIN scales with W1), stays in room
            new = min(amp * ps.amplitude / got, amp * LEVEL_MAX_STEP, amp * room / got, w1_cap)
            if abs(new - amp) <= 1e-4:  # at a limit, or a change under 0.1 mV: stop here
                break
            amp = new
            bench.vin_sine_from_wavegen(dc=w_dc, amplitude=amp, frequency=f)
            bench.sleep(max(0.05, ps.settle_cycles / f))
            tone = meter.measure(f)
            _check_peak(bench, tone, f)
        got = abs(tone.vin)
        if got > 0 and amp > 0:
            gain = got / amp
        issues = []
        if f >= sa_from:  # the analyzer resolves a small tone: a ratio needs no 50 mV, only a real one
            if got < SA_MIN_VIN:
                issues.append(
                    f"VIN only carries {got * 1e3:.2f} mV at {f:g} Hz (W1 {amp * 1e3:.0f} mV, at its limit), under "
                    f"{SA_MIN_VIN * 1e3:g} mV: the buffer can't drive VIN's capacitance this fast"
                )
        elif got < MIN_INJECTION * ps.amplitude:
            issues.append(
                f"VIN only carries {got * 1e3:.1f} mV of the {ps.amplitude * 1e3:.0f} mV sine: is the buffer "
                f"driving VIN (JP4 to the buffer, W{a.wavegen_channel} on the buffer input SMA) and scope "
                f"{a.ref_channel} on VIN?"
            )
        p = PsrrPoint(f, tone, amp, issues)
        points.append(p)
        db = f">= {p.psrr_db:.1f}" if p.limited else f"{p.psrr_db:.1f}"
        log.info("[%d/%d] %g Hz: PSRR %s dB (VIN %.1f mV pk)", n, len(freqs), f, db, got * 1e3)
    buffer_down(bench)
    bench.loadbank.off()

    notes = []
    path_gain_db = None
    if isinstance(meter, SaPsrrMeter):  # the path gain from the overlap; the analyzer's points to VOUT
        sa_issues, sa_notes = meter.finish(points)
        setup_issues += sa_issues
        notes += sa_notes
        if meter.path_gain is not None:
            path_gain_db = 20 * math.log10(meter.path_gain)
    good = [p for p in points if not p.issues]
    if good and not good[0].limited and good[0].psrr_db < 3:
        notes.append(
            f"VOUT follows VIN at {good[0].frequency:g} Hz ({good[0].psrr_db:.1f} dB): the LDO may be in dropout "
            f"at this VIN / load, or scope {bench.cfg.ad2.vout_channel} is on VIN"
        )
    if any(p.limited for p in good) and not isinstance(meter, SaPsrrMeter):
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
        path_gain_db,
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
    ax_a.plot(f, vin, "o-", ms=4, lw=1.2, color=plotting.SERIES[1], label="VIN (AD2)")
    ax_a.plot(f, vout, "o-", ms=4, lw=1.2, color=c, label="VOUT")
    ax_a.plot(f, vfl, color=plotting.MUTED, lw=1.0, ls=(0, (4, 3)), label="VOUT floor")
    cross = [(p.frequency, p.ad2_vout * 1e3) for p in res.points if p.ad2_vout is not None]
    if cross:  # meter = sa: the AD2's own VOUT reading next to the analyzer's
        ax_a.plot(
            *zip(*cross, strict=True), "x", ms=5, color=plotting.SERIES[2], label="VOUT (AD2, cross-check)"
        )
    ax_a.set_yscale("log")
    ax_a.set_ylabel("Amplitude (mV peak)")
    ax_a.set_xlabel("Frequency (Hz)")
    ax_a.legend(loc="best")
    title = (
        f"PSRR: {dut}  ·  VIN {res.vin_dc:.2f} V ± {res.amplitude * 1e3:.0f} mV  ·  "
        f"{res.i_load * 1e3:.1f} mA load  ·  {res.meter}"
    )
    plotting.suptitle(fig, title, invalid=not valid)
    return fig
