"""enable-sine: drive the enable block's analog input with a sine and watch its output switch.

JAFWAK: AD2 W1 -> EN_FG SMA (J2) -> J6 5-6 -> R23 (1k) -> EN_AIN. The enable block runs from
EN_VDD (J1 9-11 to INB = VIN through R13). EN_OUT is on J8.2, EN_AIN on TP6. A slow sine
between 0 V and EN_VDD sweeps the input through both thresholds every cycle, so the output's
crossings give VT+ and VT- (and their difference, the hysteresis), its levels VOH / VOL, its edge
times and whether it chatters. Several separate captures give the spread of the thresholds.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np

from ldo_bench import plotting
from ldo_bench.analysis import SchmittResult, schmitt_analysis
from ldo_bench.bench import Bench
from ldo_bench.config import EnSineConfig
from ldo_bench.instruments.base import Capture, ChannelSetup, InstrumentError

log = logging.getLogger(__name__)

NAME = "enable-sine"
SWING_TOLERANCE = 0.3


@dataclass
class EnSineResult:
    vin: float
    frequency: float
    low: float
    high: float
    captures: list[SchmittResult]
    t: np.ndarray  # first capture, for plotting / CSV
    ain: np.ndarray
    aout: np.ndarray
    issues: list[str] = field(default_factory=list)  # setup problems: the run isn't a valid measurement
    notes: list[str] = field(default_factory=list)  # real findings worth a look (chatter, weak VOH)

    def _stat(self, key: str) -> tuple[float, float]:
        v = np.array([getattr(c, key) for c in self.captures], float)
        v = v[np.isfinite(v)]
        return (float(v.mean()), float(v.std())) if len(v) else (float("nan"), float("nan"))

    @property
    def rows(self) -> list[dict]:
        return [{"capture": k + 1, **c.as_dict()} for k, c in enumerate(self.captures)]

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
        return {
            "valid": not self.issues,
            "issues": self.issues,
            "notes": self.notes,
            "vin": self.vin,
            "frequency": self.frequency,
            "sine_low": self.low,
            "sine_high": self.high,
            "captures": len(self.captures),
            **stats,
        }

    def report(self) -> str:
        head = (
            f"Enable block, sine {self.low:.2f}-{self.high:.2f} V at {self.frequency:g} Hz into EN_AIN, "
            f"VIN {self.vin:.2f} V, {len(self.captures)} captures:"
        )
        if self.issues:
            return "\n".join(
                [head, "  NOT A VALID MEASUREMENT (data saved and plotted as measured):"]
                + [f"    - {i}" for i in self.issues]
            )

        def mv(key):
            m, s = self._stat(key)
            return f"{m * 1e3:8.1f} mV  (spread {s * 1e3:.1f} mV)"

        h = np.array([c.hysteresis for c in self.captures])
        lines = [
            head,
            f"  VT+ (rising)  {mv('vt_rise')}",
            f"  VT- (falling) {mv('vt_fall')}",
            f"  hysteresis    {np.nanmean(h) * 1e3:8.1f} mV  (spread {np.nanstd(h) * 1e3:.1f} mV)",
            f"  VOH / VOL     {self._stat('voh')[0]:.3f} V / {self._stat('vol')[0]:.3f} V",
            f"  rise / fall   {self._stat('t_rise')[0] * 1e9:.0f} ns / {self._stat('t_fall')[0] * 1e9:.0f} ns "
            "(10-90 %, limited by the sample interval)",
            f"  duty          {self._stat('duty')[0] * 100:.1f} %   "
            f"edges per input cycle {self._stat('edges_per_cycle')[0]:.2f}",
        ]
        lines += [f"  note: {n}" for n in self.notes]
        return "\n".join(lines)


def wiring(bench: Bench, es: EnSineConfig) -> list[str]:
    a = bench.cfg.ad2
    return [
        "PSU -> VIN_DC (JP4 to DC); J1 9-11: EN_VDD from INB (other J1 rails as you like)",
        f"AD2 W{es.wavegen_channel} -> EN_FG SMA (J2); J6 5-6: EN_FG -> EN_AIN",
        f"Scope {a.ref_channel}+ -> TP6 (EN_AIN) (trigger), Scope {a.vout_channel}+ -> J8.2 (EN_OUT); both - -> GND",
        "AD2 GND -> board GND",
    ]


def run(bench: Bench, es: EnSineConfig) -> EnSineResult:
    a = bench.cfg.ad2
    lo, hi = es.low, es.sine_high
    bench.confirm_wiring(
        "enable block (sine)", wiring(bench, es), ref_probe=("en_ain", 0), ch1_probe="en_out"
    )
    setups = {a.vout_channel: ChannelSetup(5.0, 0.0), a.ref_channel: ChannelSetup(5.0, 0.0)}
    bench.vin_from_psu(es.vin)
    bench.awg.set_sine(es.wavegen_channel, frequency=es.frequency, low=lo, high=hi)
    bench.sleep(0.05)
    rate = es.n_samples / (es.cycles / es.frequency)
    level = (lo + hi) / 2
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
            "stimulus": lambda: None,
            "timeout": es.timeout,
        }
        try:
            caps.append(bench.scope.capture(**kw))
        except InstrumentError as e:
            log.warning("capture %d: no trigger on EN_AIN (%s); recording an untriggered capture", k + 1, e)
            issues.append(
                f"the scope never triggered on scope {a.ref_channel} (EN_AIN) rising through {level:.2f} V: "
                f"is it on TP6, and is W{es.wavegen_channel} on the EN_FG SMA with J6 at 5-6?"
            )
            caps.append(bench.scope.capture(**kw, force=True))
            break
    bench.awg.off(es.wavegen_channel)

    results = []
    for cap in caps:
        ain, aout = cap.channel(a.ref_channel), cap.channel(a.vout_channel)
        results.append(schmitt_analysis(ain, aout, cap.sample_rate, es.frequency))
    first = caps[0]
    ain, aout = first.channel(a.ref_channel), first.channel(a.vout_channel)
    r0 = results[0]

    # setup checks: what the data says about the wiring (the run still saves what it measured)
    swing_in = float(np.percentile(ain, 98) - np.percentile(ain, 2))
    if abs(swing_in - (hi - lo)) > SWING_TOLERANCE * (hi - lo):
        issues.append(
            f"scope {a.ref_channel} swings {swing_in:.2f} V but the sine is {hi - lo:.2f} V: is it on TP6 (EN_AIN), "
            "and is J6 at 5-6 (EN_FG -> EN_AIN)?"
        )
    if (r0.voh - r0.vol) < 0.3 * es.vin or r0.n_rise == 0:
        issues.append(
            f"EN_OUT doesn't switch (scope {a.vout_channel} swings {r0.voh - r0.vol:.2f} V, {r0.n_rise} rising "
            f"edges): is scope {a.vout_channel} on J8.2, and is EN_VDD powered (J1 9-11)?"
        )
    notes = []
    if not issues:
        epc = np.nanmean([r.edges_per_cycle for r in results])
        if epc > 1.2:
            notes.append(f"EN_OUT toggles {epc:.1f} times per input cycle: it chatters around the threshold")
        if r0.voh < 0.8 * es.vin:
            notes.append(f"VOH {r0.voh:.2f} V is below 80 % of EN_VDD ({es.vin:.2f} V)")
        if np.isfinite(r0.hysteresis) and r0.hysteresis <= 0:
            notes.append("no hysteresis: VT+ is not above VT-")
    t = first.time
    return EnSineResult(
        es.vin, es.frequency, lo, hi, results, t, ain, aout, list(dict.fromkeys(issues)), notes
    )


def plot(res: EnSineResult, dut: str):
    import matplotlib.pyplot as plt

    fig = plt.figure(figsize=(11.0, 5.6), layout="constrained")
    gs = fig.add_gridspec(2, 2, width_ratios=[1.6, 1])
    ax_in = fig.add_subplot(gs[0, 0])
    ax_out = fig.add_subplot(gs[1, 0], sharex=ax_in)
    ax_tr = fig.add_subplot(gs[:, 1])
    ms = res.t * 1e3
    valid = not res.issues
    r = res.captures[0]
    ax_in.plot(ms, res.ain, color=plotting.SERIES[1], lw=1.2)
    ax_out.plot(ms, res.aout, color=plotting.SERIES[0], lw=1.2)
    ax_tr.plot(res.ain, res.aout, color=plotting.SERIES[0], lw=0.8, alpha=0.7)
    if valid:
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
    ax_out.set_ylabel("EN_OUT (V)")
    ax_out.set_xlabel("Time (ms)")
    ax_in.tick_params(labelbottom=False)
    ax_tr.set_xlabel("EN_AIN (V)")
    ax_tr.set_ylabel("EN_OUT (V)")
    title = f"Enable block: {dut}  ·  {res.low:.2f}-{res.high:.2f} V sine at {res.frequency:g} Hz  ·  VIN {res.vin:.2f} V"
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
