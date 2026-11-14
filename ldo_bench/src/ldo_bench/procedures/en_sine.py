"""enable-sine: drive the enable block's analog input with a sine, triangle or square and watch its output switch.

JAFWAK: AD2 W1 -> EN_FG SMA (J2) -> J6 5-6 -> R23 (1k) -> EN_AIN. The enable block runs from
EN_VDD (J1 9-11 to INB = VIN through R13). EN_OUT is on J8.2, EN_AIN on TP6. A slow wave
between 0 V and EN_VDD sweeps the input through both thresholds every cycle, so the output's
crossings give VT+ and VT- (and their difference, the hysteresis), its levels VOH / VOL, its edge
times and whether it chatters. Several separate captures give the spread of the thresholds.

`shape` is a sine (default), a triangle or a square. The triangle ramps up and down linearly at
2 (high - low) frequency V/s, so both thresholds are crossed at the same, known slope; a 1 Hz
triangle (3.6 V/s for 0-1.8 V) is about 1500x slower than the 1 kHz sine at its midpoint, which
shows how much the comparator's delay moves the thresholds. The square jumps straight through both
thresholds, so it gives no thresholds; instead it gives the propagation delay from EN_AIN's
midpoint crossing to EN_OUT's (first bring-up plan, Test 1.8), for each output edge. A delay
within two sample intervals is reported as "under" that, like the edge times: use a high
frequency (e.g. 100 kHz) so the capture runs at the AD2's 100 MS/s.

Scope ref channel on TP6 (EN_AIN, also the trigger), scope vout channel on J8.2 (EN_OUT); both on
the 5 V range. With output "vout" (bring-up plan Test 5, the LDO tracking a slow triangle) scope 1
is on OUTA instead and J8 is on "LDO", so the thresholds are the EN_AIN levels where VOUT turns on
and off through the whole enable path. They include the LDO's startup and shutdown time (~6 /
~170 ms), so the wave must be slow (0.1 Hz: 0.36 V/s, ~60 mV of shift on the turn-off level). The thresholds are read from EN_AIN as measured at the pin, so R23 and the
wavegen's own error don't matter.

run() steps:
1. Confirm the wiring, PSU ramps VIN (EN_VDD comes from it), W1 starts the wave.
2. `captures` captures, each triggered on EN_AIN rising through the wave's midpoint and spanning
   `cycles` periods. Each waits up to `timeout` plus one window for its trigger and data. If one
   never triggers, a forced capture is recorded with an issue and the loop stops.
3. W1 off. `analysis.schmitt_analysis` on every capture (and `analysis.propagation_delays` for a square).
4. Validity checks on every capture: the EN_AIN swing within SWING_TOLERANCE of the wave's
   (scope on TP6, J6 at 5-6), and EN_OUT swinging at least 30 % of VIN with at least one rising
   edge (scope on EN_OUT, EN_VDD powered). These are issues (the run is invalid).
5. Only on a valid run, findings about the block itself go in `notes` (the run stays valid):
   more than 1.2 output edges per input cycle (chatter), VOH under 80 % of EN_VDD, no hysteresis.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np

from ldo_bench import plotting
from ldo_bench import wiring as w
from ldo_bench.analysis import PropagationDelays, SchmittResult, propagation_delays, schmitt_analysis
from ldo_bench.bench import Bench
from ldo_bench.config import AD2_MAX_SAMPLE_RATE, EnSineConfig
from ldo_bench.instruments.base import Capture, ChannelSetup, InstrumentError
from ldo_bench.procedures import INVALID

log = logging.getLogger(__name__)

NAME = "enable-sine"
SWING_TOLERANCE = 0.3  # EN_AIN's measured swing must be within 30 % of the wave's peak-to-peak


def _seconds(t: float) -> str:
    """A time (s) as text in ns, us or ms, three significant figures."""
    for scale, unit in ((1e-6, "ns"), (1e-3, "us"), (1.0, "ms")):
        if t < scale:
            return f"{t / scale * 1e3:.3g} {unit}"
    return f"{t:.3g} s"


@dataclass
class EnSineResult:
    """Thresholds from every capture, plus the first capture's waveforms.

    The summary and report give the mean and spread (standard deviation) over the captures.
    """

    vin: float  # V, PSU setpoint (EN_VDD is VIN through R13)
    frequency: float  # Hz, wave frequency
    low: float  # V, wave minimum / maximum as programmed on W1
    high: float
    captures: list[SchmittResult]  # one per capture
    t: np.ndarray  # first capture, for plotting / CSV
    ain: np.ndarray
    aout: np.ndarray
    shape: str = "sine"  # "sine", "triangle" or "square"
    sample_rate: float = float("nan")  # Hz, as the AD2 reported it (first capture)
    issues: list[str] = field(default_factory=list)  # setup problems: the run isn't a valid measurement
    notes: list[str] = field(default_factory=list)  # real findings worth a look (chatter, weak VOH)
    delays: list[PropagationDelays] = field(default_factory=list)  # square only: one per capture
    output: str = "en_out"  # what scope 1 watched: "en_out" (J8 pin 2) or "vout" (OUTA)

    @property
    def out_name(self) -> str:
        """The output's name in the report and plot."""
        return "VOUT" if self.output == "vout" else "EN_OUT"

    @property
    def square(self) -> bool:
        """A square input: delays are measured, thresholds are not."""
        return self.shape == "square"

    def _stat(self, key: str) -> tuple[float, float]:
        """(mean, std) of one SchmittResult field over the captures, ignoring NaNs."""
        v = np.array([getattr(c, key) for c in self.captures], float)
        v = v[np.isfinite(v)]
        return (float(v.mean()), float(v.std())) if len(v) else (float("nan"), float("nan"))

    def _edge(self, key: str) -> str:
        """Mean 10-90 % edge time as text, or a bound when it's within two sample intervals.

        An edge faster than the sampling is spread over the samples either side of it (the AD2
        averages between samples): on the bench (2026-11-12) EN_OUT measured 1.3-1.6 intervals at
        both 2 MS/s and 2 kS/s. So anything up to two intervals is reported as "under" that.
        """
        t, floor = self._stat(key)[0], 2 / self.sample_rate
        if not np.isfinite(t):
            return "n/a"
        return f"under {_seconds(floor)}" if t <= floor else _seconds(t)

    def _delay(self, key: str) -> str:
        """Mean propagation delay (s) as text, or a bound when within two sample intervals."""
        v = np.array([getattr(d, key) for d in self.delays], float)
        v = v[np.isfinite(v)]
        if not len(v):
            return "n/a (EN_OUT didn't follow)"
        floor = 2 / self.sample_rate
        t = float(v.mean())
        return (
            f"under {_seconds(floor)}" if t <= floor else f"{_seconds(t)} (spread {_seconds(float(v.std()))})"
        )

    @property
    def rows(self) -> list[dict]:
        rows = [{"capture": k + 1, **c.as_dict()} for k, c in enumerate(self.captures)]
        for row, d in zip(rows, self.delays, strict=False):
            row.update({f"tpd_{k}": v for k, v in d.as_dict().items()})
        return rows

    def extra_csvs(self) -> dict[str, list[dict]]:
        return {
            "waveform.csv": [
                {"t_s": float(t), "en_ain_v": float(a), "en_out_v": float(o)}
                for t, a, o in zip(self.t, self.ain, self.aout, strict=True)
            ]
        }

    def summary(self) -> dict:
        keys = (
            "vt_rise",
            "vt_fall",
            "hysteresis",
            "voh",
            "vol",
            "t_rise",
            "t_fall",
            "duty",
            "edges_per_cycle",
        )
        stats = {}
        for k in keys:
            if k == "hysteresis":
                v = np.array([c.hysteresis for c in self.captures])
                v = v[np.isfinite(v)]
                stats[k] = {
                    "mean": float(v.mean()) if len(v) else None,
                    "std": float(v.std()) if len(v) else None,
                }
            else:
                m, s = self._stat(k)
                stats[k] = {"mean": m, "std": s}
        if self.square:  # the input jumps through both thresholds: no threshold is measured
            for k in ("vt_rise", "vt_fall", "hysteresis"):
                stats[k] = None
            for k in ("lh", "hl"):
                v = np.array([getattr(d, k) for d in self.delays], float)
                v = v[np.isfinite(v)]
                stats[f"tpd_{k}"] = {
                    "mean": float(v.mean()) if len(v) else None,
                    "std": float(v.std()) if len(v) else None,
                }
            stats["inverting"] = bool(self.delays and self.delays[0].inverting)
        return {
            "valid": not self.issues,
            "issues": self.issues,
            "notes": self.notes,
            "vin": self.vin,
            "shape": self.shape,
            "output": self.output,
            "frequency": self.frequency,
            "sample_rate": self.sample_rate,
            "sine_low": self.low,
            "sine_high": self.high,
            "captures": len(self.captures),
            **stats,
        }

    def report(self) -> str:
        head = (
            f"Enable {'path to VOUT' if self.output == 'vout' else 'block'}, {self.shape} {self.low:.2f}-"
            f"{self.high:.2f} V at {self.frequency:g} Hz into EN_AIN, {self.out_name} on scope 1, VIN "
            f"{self.vin:.2f} V, {len(self.captures)} captures:"
        )
        if self.issues:
            return f"{head}\n  {INVALID}"

        def mv(key):
            m, s = self._stat(key)
            return f"{m * 1e3:8.1f} mV  (spread {s * 1e3:.1f} mV)"

        h = np.array([c.hysteresis for c in self.captures])
        if self.square:
            inv = " (output inverted)" if self.delays and self.delays[0].inverting else ""
            lines = [
                head,
                f"  propagation delay  output rising {self._delay('lh')} / falling {self._delay('hl')}{inv}",
                "                     (EN_AIN midpoint -> output midpoint; thresholds aren't measured with a square)",
            ]
        elif self.output == "vout":
            lines = [
                head,
                f"  EN_AIN level where VOUT turns on   {mv('vt_rise')}",
                f"  EN_AIN level where VOUT turns off  {mv('vt_fall')}",
                f"  difference                         {np.nanmean(h) * 1e3:8.1f} mV",
            ]
        else:
            lines = [
                head,
                f"  VT+ (rising)  {mv('vt_rise')}",
                f"  VT- (falling) {mv('vt_fall')}",
                f"  hysteresis    {np.nanmean(h) * 1e3:8.1f} mV  (spread {np.nanstd(h) * 1e3:.1f} mV)",
            ]
        lines += [
            f"  VOH / VOL     {self._stat('voh')[0]:.3f} V / {self._stat('vol')[0]:.3f} V",
            f"  rise / fall   {self._edge('t_rise')} / {self._edge('t_fall')} "
            f"(10-90 %, sample interval {_seconds(1 / self.sample_rate)})",
            f"  duty          {self._stat('duty')[0] * 100:.1f} %   "
            f"edges per input cycle {self._stat('edges_per_cycle')[0]:.2f}",
        ]
        if not self.square and self._edges_bounded:
            lines.append(
                "  (rise / fall are only bounds at this sample rate: run --shape square --freq 1e5 "
                "for the edge times at 100 MS/s)"
            )
        lines += [f"  note: {n}" for n in self.notes]
        return "\n".join(lines)

    @property
    def _edges_bounded(self) -> bool:
        """The rise or fall time is within two sample intervals, so it is reported as a bound."""
        return any(self._stat(k)[0] <= 2 / self.sample_rate for k in ("t_rise", "t_fall"))


def wiring(bench: Bench, es: EnSineConfig) -> list[str]:
    """Wiring lines shown before the run, one connection per line (JAFWAK)."""
    cfg = bench.cfg
    if es.output == "vout":
        return [
            *w.psu_on_vin(cfg),
            *w.j1_rails(),  # all four: VDD (J1 5-7) must be up, or the ESD diodes conduct
            'Fit J6 5-6 "FG" (EN_FG -> EN_AIN; nothing else on J6)',
            'Fit J8 1-2 "LDO" (EN_OUT -> EN_IN)',
            'Fit J8 9-10 "LDO" (~EN_OUT -> ~EN_IN)',
            f"AD2 W{es.wavegen_channel} -> EN_FG (J2)",
            w.scope(cfg, "ref", "EN_AIN (TP6)", note="the trigger"),
            w.scope_vout(cfg),
            w.ad2_gnd(cfg),
        ]
    return [
        *w.psu_on_vin(cfg),
        *w.j1_rails(),  # all four: VDD (J1 5-7) must be up, or the ESD diodes conduct
        'Fit J6 5-6 "FG" (EN_FG -> EN_AIN; nothing else on J6)',
        f"AD2 W{es.wavegen_channel} -> EN_FG (J2)",
        w.scope(cfg, "ref", "EN_AIN (TP6)", note="the trigger"),
        w.scope(cfg, "vout", "J8 pin 2 (EN_OUT)", note="J8 pin 10 for ~EN_OUT"),
        w.ad2_gnd(cfg),
    ]


def run(bench: Bench, es: EnSineConfig) -> EnSineResult:
    """Drive EN_AIN with the wave and analyse EN_OUT (see the module docstring for the steps).

    es: the [en_sine] section. Setup problems are recorded in `issues`, never raised.
    """
    a = bench.cfg.ad2
    lo, hi = es.low, es.sine_high
    on_vout = es.output == "vout"
    bench.confirm_wiring(
        f"enable {'path to VOUT' if on_vout else 'block'} ({es.shape})",
        wiring(bench, es),
        ref_probe=("en_ain", 0),
        ch1_probe="vout" if on_vout else "en_out",
        ldo_on_en_out=on_vout,
    )
    # 5 V range (+/-2.5 V) on both: covers the 0-EN_VDD wave and EN_OUT at the best resolution
    fine = ChannelSetup(bench.FINE_RANGE, 0.0)
    setups = {a.vout_channel: fine, a.ref_channel: fine}
    bench.vin_from_psu(es.vin)
    bench.en_ain_sine(es.wavegen_channel, frequency=es.frequency, low=lo, high=hi, shape=es.shape)
    bench.sleep(0.05)  # let the wavegen output start
    # n_samples spread over `cycles` periods, at most the AD2's 100 MS/s (then more cycles fit in the window)
    rate = min(es.n_samples / (es.cycles / es.frequency), AD2_MAX_SAMPLE_RATE)
    level = (lo + hi) / 2  # trigger at the wave's midpoint (V)
    window = es.n_samples / rate  # s, one capture (4 s for a 1 Hz triangle over 4 cycles)
    issues: list[str] = []
    caps: list[Capture] = []
    for k in range(es.captures):
        kw = {
            "sample_rate": rate,
            "n_samples": es.n_samples,
            "setups": setups,
            "trigger_channel": a.ref_channel,
            "trigger_level": level,
            "edge": "rising",
            "pre_trigger": 0.1,
            "stimulus": lambda: None,  # the wave runs continuously: nothing to do once armed
            "free_running": True,  # so it may trigger before the driver sees "armed" (1 kHz: < 1 ms)
            "timeout": es.timeout + window,  # the AD2 waits for the trigger, then fills the window
        }
        try:
            caps.append(bench.scope.capture(**kw))
        except InstrumentError as e:
            log.warning("capture %d: no trigger on EN_AIN (%s); recording an untriggered capture", k + 1, e)
            issues.append(
                f"the scope never triggered on scope {a.ref_channel} (EN_AIN) rising through {level:.2f} V: "
                f"is it on TP6, and is W{es.wavegen_channel} reaching EN_AIN (EN_FG SMA with J6 at 5-6)? "
                f"AD2: {e}"
            )
            caps.append(bench.scope.capture(**kw, force=True))
            break
    bench.wavegen_off(es.wavegen_channel)

    results, delays = [], []
    for cap in caps:
        ain, aout = cap.channel(a.ref_channel), cap.channel(a.vout_channel)
        results.append(schmitt_analysis(ain, aout, cap.sample_rate, es.frequency))
        if es.shape == "square":
            delays.append(propagation_delays(ain, aout, cap.sample_rate))
    first = caps[0]
    ain, aout = first.channel(a.ref_channel), first.channel(a.vout_channel)
    r0 = results[0]

    # setup checks on every capture: what the data says about the wiring (a probe can slip mid-run;
    # the run still saves what it measured)
    swing_bad, switch_bad = [], []
    for k, (cap, r) in enumerate(zip(caps, results, strict=True), 1):
        x = cap.channel(a.ref_channel)
        swing_in = float(np.percentile(x, 98) - np.percentile(x, 2))
        if abs(swing_in - (hi - lo)) > SWING_TOLERANCE * (hi - lo):
            swing_bad.append((k, swing_in))
        # EN_OUT swings rail to rail of EN_VDD (~VIN) when it works, VOUT 0 to nominal; under 30 % of
        # that it isn't switching
        full = bench.cfg.dut.vout_nominal if on_vout else es.vin
        if (r.voh - r.vol) < 0.3 * full or r.n_rise == 0:
            switch_bad.append((k, r))

    def which(bad) -> str:
        return f"capture {bad[0][0]}" if len(bad) == 1 else f"captures {', '.join(str(k) for k, _ in bad)}"

    if swing_bad:
        k, sw = swing_bad[0]
        issues.append(
            f"scope {a.ref_channel} swings {sw:.2f} V but the {es.shape} is {hi - lo:.2f} V ({which(swing_bad)}): is it "
            "on TP6 (EN_AIN), and is J6 at 5-6 (EN_FG -> EN_AIN)?"
        )
    if switch_bad:
        k, r = switch_bad[0]
        where = (
            'on OUTA (J13 pin 4), J8 on 1-2 / 9-10 "LDO", and EN_VDD and VDD powered (J1 9-11, 5-7)'
            if on_vout
            else "on J8.2, and is EN_VDD powered (J1 9-11)"
        )
        issues.append(
            f"{'VOUT' if on_vout else 'EN_OUT'} doesn't switch ({which(switch_bad)}: scope {a.vout_channel} swings "
            f"{r.voh - r.vol:.2f} V, {r.n_rise} rising edges): is scope {a.vout_channel} {where}?"
        )
    if (
        on_vout
        and not switch_bad
        and (up := bench.vout_issue(results[0].voh, source=f"scope {a.vout_channel}"))
    ):
        issues.append(f"with the LDO on, {up} (scope {a.vout_channel} belongs on OUTA, J13 pin 4)")
    notes = []
    if not issues:
        epc = np.nanmean([r.edges_per_cycle for r in results])
        if epc > 1.2:  # a clean comparator gives exactly 1; allow a little for partial cycles
            notes.append(f"EN_OUT toggles {epc:.1f} times per input cycle: it chatters around the threshold")
        if not on_vout and r0.voh < 0.8 * es.vin:
            notes.append(f"VOH {r0.voh:.2f} V is below 80 % of EN_VDD ({es.vin:.2f} V)")
        if on_vout:
            # V/s through the thresholds: the triangle's constant slope, the sine's at its midpoint
            slope = (2 if es.shape == "triangle" else np.pi) * (hi - lo) * es.frequency
            notes.append(
                f"these levels include the LDO's startup and shutdown time: at {slope:.2g} V/s, each 100 ms of "
                f"shutdown moves the turn-off level by {slope * 0.1 * 1e3:.0f} mV (slow the wave to shrink it)"
            )
        if es.shape != "square" and np.isfinite(r0.hysteresis) and r0.hysteresis <= 0:
            notes.append("no hysteresis: VT+ is not above VT-")
    t = first.time
    return EnSineResult(
        es.vin,
        es.frequency,
        lo,
        hi,
        results,
        t,
        ain,
        aout,
        shape=es.shape,
        sample_rate=first.sample_rate,
        issues=list(dict.fromkeys(issues)),
        notes=notes,
        delays=delays,
        output=es.output,
    )


EDGE_MIN_HALF_WIDTH = 12  # samples either side of an edge in its zoomed plot, at least


def edge_window(aout: np.ndarray, rising: bool, half_width: int) -> slice | None:
    """Samples around the first output edge (rising or falling through its midpoint) that has
    `half_width` samples on both sides in the capture, or None if there is no such edge."""
    mid = (np.percentile(aout, 98) + np.percentile(aout, 2)) / 2
    a, b = aout[:-1], aout[1:]
    edges = np.nonzero((a < mid) & (b >= mid) if rising else (a > mid) & (b <= mid))[0]
    for k in edges:
        if k - half_width >= 0 and k + half_width + 1 < len(aout):
            return slice(k - half_width, k + half_width + 2)
    return None


def _time_unit(span: float) -> tuple[float, str]:
    """(scale, unit) to show a time span (s) in ns, us, ms or s."""
    for scale, unit in ((1e-9, "ns"), (1e-6, "us"), (1e-3, "ms")):
        if span < scale * 1e3:
            return scale, unit
    return 1.0, "s"


def _edge_zoom(ax, res: EnSineResult, rising: bool) -> None:
    """One output edge close up: EN_AIN and EN_OUT against time, every sample marked, with the
    10 % / 90 % levels, so the edge's shape and the sample spacing can be seen."""
    r, fs = res.captures[0], res.sample_rate
    t_edge = r.t_rise if rising else r.t_fall
    half = EDGE_MIN_HALF_WIDTH
    if np.isfinite(t_edge):
        half = max(half, int(np.ceil(3 * t_edge * fs)))
    if res.delays and np.isfinite(d := res.delays[0].lh if rising else res.delays[0].hl):
        half = max(half, int(np.ceil(1.5 * d * fs)))  # keep the input edge in view too
    name = "rising" if rising else "falling"
    window = edge_window(res.aout, rising, half)
    if window is None:
        ax.set_title(f"Output {name} edge: none in the capture", fontsize=10, pad=6)
        return
    t = res.t[window] - res.t[window][len(res.t[window]) // 2]
    scale, unit = _time_unit(float(t[-1] - t[0]))
    ax.plot(t / scale, res.ain[window], color=plotting.SERIES[1], lw=1.0, marker=".", ms=3, label="EN_AIN")
    ax.plot(t / scale, res.aout[window], color=plotting.SERIES[0], lw=1.2, marker=".", ms=4, label="EN_OUT")
    swing = r.voh - r.vol
    for frac in (0.1, 0.9):
        ax.axhline(r.vol + frac * swing, color=plotting.INK_2, lw=0.8, ls=(0, (3, 2)))
    ax.set_title(
        f"Output {name} edge: {res._edge('t_rise' if rising else 't_fall')} (10-90 %)", fontsize=10, pad=6
    )
    ax.set_xlabel(f"Time from the edge ({unit}); dots are samples, {_seconds(1 / fs)} apart")
    ax.set_ylabel("V")
    ax.legend(fontsize=8, loc="center right" if rising else "center left")


def plot(res: EnSineResult, dut: str):
    """EN_AIN and EN_OUT against time (top left), EN_OUT against EN_AIN, the transfer (top right),
    and one rising and one falling output edge close up (bottom).

    Valid runs mark VT+ / VT- from the first capture; invalid runs show the issues in a red box.
    """
    import matplotlib.pyplot as plt

    fig = plt.figure(figsize=(11.0, 8.4), layout="constrained")
    gs = fig.add_gridspec(3, 2, width_ratios=[1.6, 1], height_ratios=[1, 1, 1.2])
    ax_in = fig.add_subplot(gs[0, 0])
    ax_out = fig.add_subplot(gs[1, 0], sharex=ax_in)
    ax_tr = fig.add_subplot(gs[:2, 1])
    zoom = gs[2, :].subgridspec(1, 2)
    for col, rising in enumerate((True, False)):
        _edge_zoom(fig.add_subplot(zoom[0, col]), res, rising)
    ms = res.t * 1e3
    valid = not res.issues
    r = res.captures[0]
    ax_in.plot(ms, res.ain, color=plotting.SERIES[1], lw=1.2)
    ax_out.plot(ms, res.aout, color=plotting.SERIES[0], lw=1.2)
    ax_tr.plot(res.ain, res.aout, color=plotting.SERIES[0], lw=0.8, alpha=0.7)
    if valid and res.square:
        ax_tr.set_title("Transfer (square: no thresholds measured)", fontsize=10, pad=6)
    elif valid:
        for v, name in ((r.vt_rise, "VT+"), (r.vt_fall, "VT-")):
            ax_in.axhline(v, color=plotting.INK_2, lw=0.9, ls=(0, (3, 2)))
            ax_in.annotate(
                f"{name} {v:.3f} V",
                (1.0, v),
                xycoords=("axes fraction", "data"),
                xytext=(-4, 3),
                textcoords="offset points",
                ha="right",
                fontsize=8,
                color=plotting.INK_2,
            )
            ax_tr.axvline(v, color=plotting.INK_2, lw=0.9, ls=(0, (3, 2)))
            ax_tr.annotate(
                f"{name}\n{v:.3f} V",
                (v, 0.5),
                xycoords=("data", "axes fraction"),
                xytext=(4, 0),
                textcoords="offset points",
                fontsize=8,
                color=plotting.INK_2,
            )
        ax_tr.set_title(f"Transfer: hysteresis {r.hysteresis * 1e3:.0f} mV", fontsize=10, pad=6)
    else:
        ax_tr.set_title("Transfer (as measured)", fontsize=10, pad=6)
        plotting.issue_box(ax_out, res.issues)
    ax_in.set_ylabel("EN_AIN (V)")
    ax_out.set_ylabel(f"{res.out_name} (V)")
    ax_out.set_xlabel("Time (ms)")
    ax_in.tick_params(labelbottom=False)
    ax_tr.set_xlabel("EN_AIN (V)")
    ax_tr.set_ylabel(f"{res.out_name} (V)")
    title = (
        f"Enable block: {dut}  ·  {res.low:.2f}-{res.high:.2f} V {res.shape} at {res.frequency:g} Hz"
        f"  ·  VIN {res.vin:.2f} V"
    )
    plotting.suptitle(fig, title, invalid=not valid)
    return fig
