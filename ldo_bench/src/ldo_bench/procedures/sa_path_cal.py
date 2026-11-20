"""sa-path-cal: the gain of the HP 8591E's path from U4's input to the analyzer, vs frequency, for psrr.

psrr --meter sa reads VOUT through U4 -> R14 (47 ohm) -> J9 -> DC block -> coax -> 8591E. Its gain
can't be measured inside psrr (VOUT is only tens of uV where both meters overlap), so this run
measures it with a known 100 mV tone and saves a curve that psrr applies.

Wiring: J10 removed (U4's input, J10 pin 1, no longer joined to VOUT, pin 2); AD2 W2 -> J10 pin 1;
AD2 scope 1 -> J10 pin 1; scope 2 -> OUTA (VOUT, to prove J10 is out); J9 -> DC block -> coax ->
8591E INPUT; U2 and U4 on their split rails; nothing on VIN_DC. The LDO stays unpowered.

run() steps:
1. W1 to 0 V (U2's input), the split rails up (Bench.buffer_rails_on). VIN stays near 0 V (U2 follows
   W1), so the chip is off. Scope 2 is on VOUT here, not VIN, so the rails' ramp check reads VOUT:
   if the PSU were on VIN, the LDO would start regulating and VOUT leave the window by VIN ~1.3 V.
   VOUT must then read near 0 V (VOUT_OFF_MAX), else stop.
2. Bench.prove_j10_open(): W2 at 0.3 then 0.6 V DC; J10 pin 1 must follow, VOUT must not. Only then
   may W2 go above 0.6 V.
3. At each calibration frequency (sa_from to f_stop of [psrr], CAL_PPD per decade): W2 plays
   1.2 V + 100 mV, the AD2 fits the tone on J10 pin 1 (CAL_AVERAGES captures), the 8591E reads it on
   J9 with exactly psrr's settings (read_tone). Gain = analyzer / AD2 (V peak / V peak).
4. Linearity: at LINEARITY_POINTS frequencies, the same with 10 mV and 1 mV tones. psrr's tones are
   60-80 dB below this one: the gain must hold within LINEARITY_TOL_DB, else a setup issue.
5. W2 to 0 V, the rails down (W2 off last).

A point where either reading is less than MIN_MARGIN_DB above its floor is a setup issue.
"""

from __future__ import annotations

import csv
import logging
import math
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import numpy as np

from ldo_bench import plotting
from ldo_bench.analysis import sine_fit
from ldo_bench.bench import Bench
from ldo_bench.config import PsrrConfig
from ldo_bench.instruments.base import ChannelSetup, InstrumentError
from ldo_bench.instruments.hp859x import HP859x
from ldo_bench.procedures.line_tran import buffer_down, buffer_up

log = logging.getLogger(__name__)

NAME = "sa-path-cal"
CAL_DC = 1.2  # V on U4's input: VOUT's working point
CAL_AMPLITUDE = 0.1  # V peak
LINEARITY_AMPLITUDES = (0.01, 0.001)  # V peak: psrr's tones are far smaller than CAL_AMPLITUDE
LINEARITY_POINTS = 3  # frequencies (spread over the range) the linearity is checked at
LINEARITY_TOL_DB = 0.5
CAL_PPD = 6  # calibration frequencies per decade
CAL_AVERAGES = 16  # AD2 captures per point (the tone is 100 mV: plenty)
CAL_CYCLES = 20  # sine cycles per AD2 capture
MIN_MARGIN_DB = 20.0  # both readings this far above their floors
VOUT_OFF_MAX = 0.15  # V: VOUT with the LDO unpowered (above it: the chip is powered, or a probe is wrong)
SETTLE = 0.05  # s after each W2 change

# --- reading one tone on the 8591E, as psrr does --------------------------------------------------
SA_RBW_RANGE = (300.0, 3e3)  # Hz: RBW = f / SA_RBW_DIVISOR within this (300 Hz: the bench unit's narrowest)
SA_RBW_DIVISOR = 30  # f / 30: a narrow RBW keeps the analyzer's LO feedthrough near 0 Hz off the tone
SA_SPAN_RBWS = 20  # span = this many RBWs around the tone
SA_FLOOR_RBWS = 6  # the floor is read this many RBWs ABOVE the tone (below it sits the LO feedthrough)
SA_FLOOR_DB = 6.0  # the floor reported: that reading + 6 dB
SA_REF_LEVEL_DBM = -20.0  # 31.6 mV peak into 50 ohm


@dataclass
class SaReading:
    """One tone as the 8591E read it, at its input (V peak), before any path gain."""

    vpeak: float
    floor_vpeak: float
    rbw: float  # Hz
    peak_frequency: float  # Hz, where the marker found the tone
    dbm: float  # the tone (dBm into 50 ohm)

    @property
    def margin_db(self) -> float:
        """How far the tone is above the analyzer's floor (dB)."""
        return margin_db(self.vpeak, self.floor_vpeak)


def margin_db(amp: float, floor: float) -> float:
    """amp over floor (dB); NaN if either isn't positive."""
    return 20 * math.log10(amp / floor) if amp > 0 and floor > 0 else math.nan


def read_tone(sa: HP859x, f: float) -> SaReading:
    """The tone at `f` (Hz): RBW f/30 (300 Hz - 3 kHz), span 20 RBWs, reference -20 dBm; marker to the
    peak if it is within 2 RBWs of f, else at f; floor 6 RBWs above f, + 6 dB."""
    lo, hi = SA_RBW_RANGE
    want = min(max(f / SA_RBW_DIVISOR, lo), hi)
    rbw = sa.tune(center=f, span=SA_SPAN_RBWS * want, rbw=want, ref_level_dbm=SA_REF_LEVEL_DBM)
    sa.sweep()
    pk = sa.peak()
    tone = pk if abs(pk.frequency - f) <= 2 * rbw else sa.marker_at(f)
    noise = sa.marker_at(f + SA_FLOOR_RBWS * rbw).vpeak
    return SaReading(tone.vpeak, noise * 10 ** (SA_FLOOR_DB / 20), rbw, tone.frequency, tone.dbm)


# --- the saved curve, as psrr uses it -------------------------------------------------------------
@dataclass
class PathGainCurve:
    """Path gain (V/V) vs frequency from an sa-path-cal run; log-frequency interpolation in dB."""

    frequencies: np.ndarray  # Hz, ascending
    gain_db: np.ndarray
    source: str  # the run folder it came from
    measured: datetime | None = None

    def gain(self, f: float) -> float:
        """V/V at `f` (Hz). InstrumentError outside the calibrated range (no extrapolation)."""
        lo, hi = float(self.frequencies[0]), float(self.frequencies[-1])
        if not lo * (1 - 1e-6) <= f <= hi * (1 + 1e-6):
            raise InstrumentError(
                f"{f:g} Hz is outside the 8591E path calibration ({lo:g} to {hi:g} Hz, {self.source}): "
                "run sa-path-cal over the psrr range"
            )
        db = float(np.interp(math.log10(f), np.log10(self.frequencies), self.gain_db))
        return 10 ** (db / 20)

    @property
    def age_hours(self) -> float:
        return (datetime.now() - self.measured).total_seconds() / 3600 if self.measured else math.nan

    @classmethod
    def load(cls, folder: Path) -> PathGainCurve:
        """From a saved sa-path-cal run folder. Refuses an invalid or simulated run."""
        folder = Path(folder)
        name = folder.name
        if name.endswith("_INVALID") or "_SIM" in name:
            raise InstrumentError(f"{folder} is an invalid or simulated sa-path-cal run: not a calibration")
        path = folder / "data.csv"
        if not path.exists():
            raise InstrumentError(f"no data.csv in {folder}: not an sa-path-cal run folder")
        with path.open(encoding="utf-8") as fh:
            rows = [r for r in csv.DictReader(fh) if r["kind"] == "cal"]
        if len(rows) < 2:
            raise InstrumentError(f"{path} has fewer than two calibration points")
        f = np.array([float(r["frequency_hz"]) for r in rows])
        g = np.array([float(r["gain_db"]) for r in rows])
        if not np.all(np.isfinite(g)):
            raise InstrumentError(f"{path} has a calibration point with no gain")
        order = np.argsort(f)
        try:
            measured = datetime.strptime(name[:15], "%Y%m%d-%H%M%S")
        except ValueError:
            measured = None
        return cls(f[order], g[order], str(folder), measured)

    @classmethod
    def flat(cls, gain_db: float, f_lo: float, f_hi: float, source: str) -> PathGainCurve:
        """A constant gain over f_lo..f_hi (Hz): for the simulator only (`psrr --sim --meter sa`)."""
        return cls(np.array([f_lo, f_hi]), np.array([gain_db, gain_db]), source, datetime.now())

    @classmethod
    def newest(cls, results: Path) -> PathGainCurve:
        """The newest valid (not _INVALID, not _SIM) sa-path-cal run under `results`."""
        root = Path(results) / NAME
        runs = sorted(
            (
                p
                for p in root.glob("*")
                if p.is_dir() and not p.name.endswith("_INVALID") and "_SIM" not in p.name
            ),
            key=lambda p: p.name,
        )
        if not runs:
            raise InstrumentError(
                f"no valid sa-path-cal run in {root}: run `ldo-bench sa-path-cal` first (or give --sa-cal)"
            )
        return cls.load(runs[-1])


# --- the calibration run --------------------------------------------------------------------------
@dataclass
class CalPoint:
    """One calibration frequency at one tone amplitude."""

    frequency: float  # Hz
    amplitude: float  # V peak W2 played
    u4_in: float  # V peak the AD2 measured on J10 pin 1
    u4_floor: float  # V peak: the AD2 fit's noise (4 sigma) on that
    sa: SaReading
    kind: str = "cal"  # "cal" (the curve) or "linearity"
    issues: list[str] = field(default_factory=list)

    @property
    def gain(self) -> float:
        return self.sa.vpeak / self.u4_in if self.u4_in > 0 else math.nan

    @property
    def gain_db(self) -> float:
        return 20 * math.log10(self.gain) if self.gain > 0 else math.nan


@dataclass
class SaPathCalResult:
    points: list[CalPoint]
    proof: dict
    analyzer: str
    setup_issues: list[str] = field(default_factory=list)

    @property
    def curve(self) -> list[CalPoint]:
        return [p for p in self.points if p.kind == "cal"]

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
                "kind": p.kind,
                "frequency_hz": p.frequency,
                "w2_amplitude_v": p.amplitude,
                "u4_in_v": p.u4_in,
                "u4_floor_v": p.u4_floor,
                "u4_margin_db": margin_db(p.u4_in, p.u4_floor),
                "sa_dbm": p.sa.dbm,
                "sa_vpeak_v": p.sa.vpeak,
                "sa_floor_vpeak_v": p.sa.floor_vpeak,
                "sa_margin_db": p.sa.margin_db,
                "sa_rbw_hz": p.sa.rbw,
                "sa_peak_frequency_hz": p.sa.peak_frequency,
                "gain_db": p.gain_db,
                "valid": not p.issues,
                "issues": " | ".join(p.issues),
            }
            for p in self.points
        ]

    def summary(self) -> dict:
        g = [p.gain_db for p in self.curve if np.isfinite(p.gain_db)]
        return {
            "valid": not self.issues,
            "issues": self.issues,
            "analyzer": self.analyzer,
            "j10_proof": self.proof,
            "gain_db_min": min(g) if g else None,
            "gain_db_max": max(g) if g else None,
            "frequency_hz": [p.frequency for p in self.curve],
            "gain_db": [p.gain_db for p in self.curve],
        }

    def report(self) -> str:
        lines = [f"8591E path gain (U4 -> R14 -> J9 -> DC block -> coax), {self.analyzer}:"]
        lines.append("      f (Hz)   tone (mV)   U4 in (mV)   8591E (mV)   gain (dB)")
        for p in self.points:
            tag = "" if p.kind == "cal" else "  (linearity)"
            lines.append(
                f"  {p.frequency:10.4g}   {p.amplitude * 1e3:9.1f}   {p.u4_in * 1e3:10.3f}   "
                f"{p.sa.vpeak * 1e3:10.4f}   {p.gain_db:9.2f}{tag}"
            )
        return "\n".join(lines)


def calibration_frequencies(ps: PsrrConfig) -> list[float]:
    """CAL_PPD per decade from psrr.sa_from to psrr.f_stop, both ends included."""
    decades = math.log10(ps.f_stop / ps.sa_from)
    n = max(2, round(decades * CAL_PPD) + 1)
    return [float(f) for f in np.geomspace(ps.sa_from, ps.f_stop, n)]


def wiring(bench: Bench) -> list[str]:
    """Wiring lines shown before the run."""
    from ldo_bench import wiring as w

    cfg, a = bench.cfg, bench.cfg.ad2
    return [
        *w.buffer_supply(cfg),
        f"AD2 W{a.wavegen_channel} -> VIN_FG (J3): holds U2's input at 0 V (VIN stays off)",
        'REMOVE J10 "LOAD": U4\'s input (J10 pin 1) must not be joined to VOUT (pin 2)',
        f"AD2 W{bench.u4_channel} -> J10 pin 1 (U4's input), AD2 GND -> {cfg.board.ad2_gnd}",
        f"Scope {a.vout_channel}+ -> J10 pin 1 (U4's input), Scope {a.vout_channel}- -> {cfg.board.probe_gnd}",
        f"Scope {a.ref_channel}+ -> {cfg.board.vout_probe} (VOUT: proves J10 is out), Scope {a.ref_channel}- -> "
        f"{cfg.board.probe_gnd}",
        "U4 on the split supply (V- on -5 V by jumper wires), JP3 open",
        "J9 (VOUT_BUFF) -> DC block -> coax -> 8591E INPUT 50 Ω (the same cable and block as for psrr)",
        "Nothing on VIN_DC (J7); the LDO stays unpowered",
    ]


def _fit_u4(bench: Bench, f: float, setups: dict[int, ChannelSetup]) -> tuple[float, float]:
    """(amplitude, 4-sigma floor), V peak, of the tone on J10 pin 1 from CAL_AVERAGES AD2 captures."""
    a = bench.cfg.ad2
    rate = min(100e6, 8192 * f / CAL_CYCLES)
    amps, sig = [], []
    for _ in range(CAL_AVERAGES):
        cap = bench.scope.capture(
            sample_rate=rate,
            n_samples=8192,
            setups=setups,
            trigger_channel=a.vout_channel,
            trigger_level=CAL_DC,
            edge="rising",
            pre_trigger=0.0,
            stimulus=lambda: None,
            timeout=5.0,
            force=True,
        )
        fit = sine_fit(cap.channel(a.vout_channel), cap.sample_rate, f)
        amps.append(fit.amplitude)
        sig.append(fit.amplitude_noise)
    return float(np.mean(amps)), 4.0 * float(np.mean(sig)) / math.sqrt(CAL_AVERAGES)


def _point(bench: Bench, f: float, amp: float, setups, kind: str) -> CalPoint:
    bench.u4_input_from_wavegen(CAL_DC, amp, f)
    bench.sleep(max(SETTLE, 20 / f))
    u4, u4_floor = _fit_u4(bench, f, setups)
    sa = read_tone(bench.sa, f)
    p = CalPoint(f, amp, u4, u4_floor, sa, kind)
    if margin_db(u4, u4_floor) < MIN_MARGIN_DB:
        p.issues.append(
            f"{f:g} Hz: the AD2 sees only {u4 * 1e3:.3f} mV on J10 pin 1 for a {amp * 1e3:g} mV tone"
        )
    if sa.margin_db < MIN_MARGIN_DB:
        p.issues.append(
            f"{f:g} Hz: the 8591E reads {sa.dbm:+.1f} dBm, only {sa.margin_db:.1f} dB above its floor: "
            "check U4's rails, the J9 coax and the DC block"
        )
    log.info("%s %g Hz, %g mV: gain %.2f dB", kind, f, amp * 1e3, p.gain_db)
    return p


def run(bench: Bench, ps: PsrrConfig) -> SaPathCalResult:
    """The steps in the module docstring."""
    if bench.sa is None:
        raise InstrumentError("sa-path-cal needs the HP 8591E ([sa])")
    if bench.cfg.buffer is None:
        raise InstrumentError("sa-path-cal needs U2 and U4 on the split supply ([buffer])")
    a = bench.cfg.ad2
    bench.confirm_wiring(
        "8591E path calibration",
        wiring(bench),
        ref_probe=("vin", 0),
        psu_to="rail",
        u4_rig=True,
        chip_powered=False,
    )
    bench.sa.preset()
    buffer_up(bench, ps.psu_voltage)
    vout = bench.scope.measure_dc(0.05, {a.ref_channel: ChannelSetup(bench.FINE_RANGE, 0.0)})[
        a.ref_channel
    ].mean
    if abs(vout) > VOUT_OFF_MAX:
        raise InstrumentError(
            f"VOUT reads {vout:.3f} V with the LDO meant to be unpowered (VIN held near 0 V): is anything on "
            f"VIN_DC, the PSU on VIN, or scope {a.ref_channel} not on VOUT? Stopping"
        )
    proof = bench.prove_j10_open()
    setups = {
        a.vout_channel: ChannelSetup(bench.FINE_RANGE, 0.0),
        a.ref_channel: ChannelSetup(bench.FINE_RANGE, 0.0),
    }
    freqs = calibration_frequencies(ps)
    points = [_point(bench, f, CAL_AMPLITUDE, setups, "cal") for f in freqs]
    issues: list[str] = []
    for f in [freqs[round(k * (len(freqs) - 1) / (LINEARITY_POINTS - 1))] for k in range(LINEARITY_POINTS)]:
        ref = next(p for p in points if p.frequency == f)
        for amp in LINEARITY_AMPLITUDES:
            p = _point(bench, f, amp, setups, "linearity")
            points.append(p)
            if not p.issues and abs(p.gain_db - ref.gain_db) > LINEARITY_TOL_DB:
                issues.append(
                    f"at {f:g} Hz the gain is {p.gain_db:+.2f} dB with a {amp * 1e3:g} mV tone against "
                    f"{ref.gain_db:+.2f} dB at {CAL_AMPLITUDE * 1e3:g} mV (tolerance ±{LINEARITY_TOL_DB} dB): "
                    "the analyzer isn't linear down to psrr's levels with these settings"
                )
    bench.u4_input_from_wavegen(0.0)
    buffer_down(bench)
    return SaPathCalResult(points, proof, bench.sa.idn, issues)


def plot(res: SaPathCalResult, dut: str):
    """Path gain (dB) vs frequency; the linearity points as hollow markers."""
    fig, axes = plotting.figure(1, 1, size=(8.0, 4.4), squeeze=False)
    ax = axes[0][0]
    cal = res.curve
    ax.plot(
        [p.frequency for p in cal],
        [p.gain_db for p in cal],
        "o-",
        ms=4,
        color=plotting.SERIES[0],
        label=f"{CAL_AMPLITUDE * 1e3:g} mV tone",
    )
    for k, amp in enumerate(LINEARITY_AMPLITUDES, 1):
        lin = [p for p in res.points if p.kind == "linearity" and p.amplitude == amp]
        ax.plot(
            [p.frequency for p in lin],
            [p.gain_db for p in lin],
            "s",
            ms=6,
            mfc=plotting.SURFACE,
            mec=plotting.SERIES[k],
            label=f"{amp * 1e3:g} mV tone",
        )
    ax.set_xscale("log")
    ax.set_xlabel("Frequency (Hz)")
    ax.set_ylabel("Path gain (dB)")
    ax.legend(loc="best")
    if res.issues:
        plotting.issue_box(ax, res.issues)
    plotting.suptitle(fig, f"8591E path gain (U4 → J9 → DC block → coax): {dut}", invalid=bool(res.issues))
    return fig
