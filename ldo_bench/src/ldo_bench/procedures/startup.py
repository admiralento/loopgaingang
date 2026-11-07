"""enable-startup: step the enable line and record how VOUT and the BGR (or the supply current) respond.

JAFWAK enable path: AD2 DIO -> J13.12 EN_CTRL_33 -> FXLP34 -> EN_CTRL_18 -> J6 7-8 -> EN_AIN
-> enable block -> EN_OUT -> J8 1-2 -> EN_IN (and ~EN_OUT -> J8 9-10 -> ~EN_IN; ~EN_CTRL_33 on
J13.10 is driven inverted for boards jumpered to take it directly). The AD2's V+ supplies V_33.

The scope triggers on the AD2's own DIO edge, so both channels are free: ch1 = VOUT, ch2 = VREF
(TP3, the BGR output) or the voltage across R13 (input current) / R1 (output current). Each edge
(enable, disable) is captured `averages` times and averaged; t = 0 is the DIO edge, so delays
include the level shifter and the enable block.

run() steps:
1. Confirm the wiring; AD2 V+ on (V_33), enable low, load code set, PSU ramps VIN, wait off_time.
2. For each edge: `_capture_edge` puts the enable in its starting state, waits (off_time before
   an enable edge, on_time before a disable edge), arms a capture triggered by the DIO pin and
   flips the enable. Repeated `averages` times and averaged with align_average.
3. `analysis.step_metrics` on VOUT and on ch2 (currents = volts across the sense resistor /
   sense_ohm): before / after levels, 10 % and 90 % times, settling time, overshoot, peak.
4. Validity checks from the data: on the enable edge VOUT must start below 20 % of nominal (else
   the enable path isn't controlling the LDO) and end near nominal (bench.vout_issue), and VREF
   must end within 20 % of su.vref_nominal; on the disable edge VOUT must start near nominal.
5. Enable low, load bank off.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np

from ldo_bench import plotting
from ldo_bench.analysis import StepMetrics, step_metrics
from ldo_bench.bench import Bench
from ldo_bench.config import StartupConfig
from ldo_bench.instruments.base import Capture, ChannelSetup, InstrumentError
from ldo_bench.procedures.transient import align_average

log = logging.getLogger(__name__)

NAME = "enable-startup"

CH2_LABEL = {"vref": "VREF (V)", "iin": "Input current (mA)", "iout": "Output current (mA)"}
CH2_NAME = {"vref": "VREF", "iin": "I_in", "iout": "I_out"}


@dataclass
class EdgeStartup:
    """One enable or disable edge, averaged, with step metrics for both channels."""

    edge: str  # "rising" = enable, "falling" = disable
    t: np.ndarray  # s, 0 at the EN_CTRL_33 DIO edge
    vout: np.ndarray  # V
    ch2: np.ndarray  # V for vref, A for currents
    vout_m: StepMetrics
    ch2_m: StepMetrics
    issues: list[str] = field(default_factory=list)  # setup problems: non-empty = not a valid edge

    @property
    def title(self) -> str:
        return "Enable (EN_CTRL rising)" if self.edge == "rising" else "Disable (EN_CTRL falling)"


@dataclass
class StartupResult:
    """The whole enable-startup run: one EdgeStartup per edge in startup.edges."""

    vin: float  # V, PSU setpoint
    ch2: str  # what scope ch2 measured: "vref", "iin" or "iout"
    averages: int  # captures averaged per edge
    load_code: int  # load bank code held during the run
    i_load: float  # A, that code's current at the nominal VOUT
    edges: list[EdgeStartup]
    vout_nominal: float  # V
    setup_issues: list[str] = field(default_factory=list)

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
            row = {"edge": e.edge, "valid": not e.issues, "issues": " | ".join(e.issues)}
            row |= {f"vout_{k}": v for k, v in e.vout_m.as_dict().items()}
            row |= {f"{self.ch2}_{k}": v for k, v in e.ch2_m.as_dict().items()}
            out.append(row)
        return out

    def extra_csvs(self) -> dict[str, list[dict]]:
        unit = "v" if self.ch2 == "vref" else "a"
        return {
            f"waveform_{e.edge}.csv": [
                {"t_s": float(t), "vout_v": float(v), f"{self.ch2}_{unit}": float(c)}
                for t, v, c in zip(e.t, e.vout, e.ch2, strict=True)
            ]
            for e in self.edges
        }

    def summary(self) -> dict:
        return {
            "valid": not self.issues,
            "issues": self.issues,
            "vin": self.vin,
            "ch2": self.ch2,
            "averages": self.averages,
            "load_code": self.load_code,
            "i_load_a": self.i_load,
            "edges": {
                e.edge: {"vout": e.vout_m.as_dict(), self.ch2: e.ch2_m.as_dict(), "issues": e.issues}
                for e in self.edges
            },
        }

    def report(self) -> str:
        lines = [
            f"Enable startup at VIN {self.vin:.2f} V, load {self.i_load * 1e3:.1f} mA (code {self.load_code}), "
            f"{self.averages} averages; t = 0 at the EN_CTRL_33 edge:"
        ]

        def us(x):
            return "n/a" if not np.isfinite(x) else f"{x * 1e6:.1f} us"

        for e in self.edges:
            lines.append(f"  {e.title}")
            if e.issues:
                lines.append("    NOT A VALID MEASUREMENT (data saved and plotted as measured):")
                lines += [f"      - {i}" for i in e.issues]
                continue
            v = e.vout_m
            word = "rise" if e.edge == "rising" else "fall"
            lines.append(
                f"    VOUT   {v.before:.4f} -> {v.after:.4f} V;  10 % at {us(v.t10)}, 90 % at {us(v.t90)} "
                f"({word} {us(v.rise_time)}), settled ({us(v.t_settle)}), overshoot {v.overshoot * 100:.1f} %"
            )
            c = e.ch2_m
            if self.ch2 == "vref":
                lines.append(
                    f"    VREF   {c.before:.4f} -> {c.after:.4f} V;  10 % at {us(c.t10)}, 90 % at {us(c.t90)}, "
                    f"settled ({us(c.t_settle)}), overshoot {c.overshoot * 100:.1f} %"
                )
            else:
                lines.append(
                    f"    {CH2_NAME[self.ch2]:6s} {c.before * 1e3:.2f} -> {c.after * 1e3:.2f} mA;  peak "
                    f"{c.peak * 1e3:.1f} mA"
                )
        return "\n".join(lines)


def wiring(bench: Bench, su: StartupConfig) -> list[str]:
    """Wiring lines shown before the run; the ch2 line depends on su.ch2."""
    a, e, lb = bench.cfg.ad2, bench.cfg.enable, bench.cfg.loadbank
    ch2 = {
        "vref": f"Scope {a.ref_channel}+ -> TP3 (VREF, the BGR output), {a.ref_channel}- -> GND; JP2 closed",
        "iin": f"Scope {a.ref_channel}+ -> J13.1 (VIN), {a.ref_channel}- -> J13.2 (INB): across R13, input current",
        "iout": f"Scope {a.ref_channel}+ -> J13.4 (OUTA), {a.ref_channel}- -> J13.3 (VOUT): across R1, output current",
    }[su.ch2]
    lines = [
        "PSU -> VIN_DC (JP4 to DC); J1: each rail you want powered (BGR_VDD 1-3, VDD 5-7, EN_VDD 9-11, OTA_VDD 13-15)",
        "Enable path: J6 7-8 (EN_CTRL_18 -> EN_AIN), J8 1-2 (EN_OUT -> EN_IN), J8 9-10 (~EN_OUT -> ~EN_IN)",
        f"AD2 DIO {e.dio_pin} -> J13.12 (EN_CTRL_33)"
        + (f", DIO {e.dio_pin_n} -> J13.10 (~EN_CTRL_33)" if e.dio_pin_n is not None else ""),
        ("AD2 V+ -> J13.14 (V_33)" if e.v33 is not None else "3.3 V supply -> J13.14 (V_33)")
        + ", AD2 GND -> board GND",
        f"Scope {a.vout_channel}+ -> J13.3 (VOUT), {a.vout_channel}- -> GND",
        ch2,
    ]
    if su.load_code:
        lines.append(f"DIO {lb.dio_pins} -> load bank bits B0..B{len(lb.dio_pins) - 1}")
    return lines


def _capture_edge(bench: Bench, su: StartupConfig, setups, edge: str) -> tuple[Capture, list[str]]:
    """`su.averages` captures of one enable edge, averaged, plus any trigger issues.

    The AD2 triggers on its own DIO output (dio_trigger), so the scope channels are free for VOUT
    and ch2. If a capture never triggers, a forced capture is taken and the average of the ones
    that did trigger (or the forced one) is returned with an issue.
    """
    on = edge == "rising"
    pin = bench.cfg.enable.dio_pin
    caps, issues = [], []
    for k in range(su.averages):
        bench.set_enable(not on)  # start state
        # time in the start state: fully off (caps discharged) before enabling, settled on before disabling
        bench.sleep(su.off_time if on else su.on_time)
        kw = {
            "sample_rate": su.n_samples / su.window,
            "n_samples": su.n_samples,
            "setups": setups,
            "trigger_channel": 1,  # the DIO trigger below replaces the analog trigger
            "trigger_level": 0.0,
            "edge": edge,
            "pre_trigger": su.pre_trigger,
            "stimulus": lambda: bench.set_enable(on),
            "timeout": su.timeout,
            "dio_trigger": (pin, edge),
        }
        try:
            caps.append(bench.scope.capture(**kw))
        except InstrumentError as e:
            log.warning(
                "%s capture %d: no trigger on DIO %d (%s); recording untriggered", edge, k + 1, pin, e
            )
            issues.append(f"the scope never triggered on DIO {pin} ({edge}): plotted an untriggered capture")
            bench.set_enable(not on)
            bench.sleep(su.off_time if on else su.on_time)
            forced = bench.scope.capture(**kw, force=True)
            return (align_average(caps) if caps else forced), issues
    return align_average(caps), issues


def run(bench: Bench, su: StartupConfig) -> StartupResult:
    """Capture each edge in su.edges (see the module docstring for the steps and checks).

    su: the [startup] section. Setup problems are recorded per edge, never raised.
    """
    a = bench.cfg.ad2
    nom = bench.cfg.dut.vout_nominal
    bench.confirm_wiring(
        "enable startup",
        wiring(bench, su),
        ref_probe=(su.ch2, 0),
        en_pin=bench.cfg.enable.dio_pin,
    )
    setups = {
        a.vout_channel: ChannelSetup(a.vout.range, a.vout.offset),
        a.ref_channel: ChannelSetup(su.ch2_range, 0.0),  # range set per ch2 signal (VREF or a sense voltage)
    }
    bench.enable_logic(True)
    bench.set_enable(False)
    bench.loadbank.set(su.load_code)
    bench.vin_from_psu(su.vin)
    bench.sleep(su.off_time)
    i_load = bench.loadbank.current(su.load_code, nom)

    edges = []
    for edge in su.edges:
        cap, issues = _capture_edge(bench, su, setups, edge)
        t = cap.time
        vout = cap.channel(a.vout_channel)
        raw2 = cap.channel(a.ref_channel)
        ch2 = raw2 if su.ch2 == "vref" else raw2 / su.sense_ohm  # currents: Ohm's law on R13 / R1
        vm = step_metrics(t, vout, su.settle_fraction)
        cm = step_metrics(t, ch2, su.settle_fraction)
        # setup checks from the data (the run still saves what it measured)
        if edge == "rising":
            # a disabled LDO should sit near 0 V; 20 % of nominal leaves room for leakage and noise
            if vm.before > 0.2 * nom:
                issues.append(
                    f"VOUT was already {vm.before:.3f} V before the enable edge: the enable path isn't "
                    "controlling the LDO (J6 7-8, J8 1-2 / 9-10?)"
                )
            up = bench.vout_issue(vm.after, source=f"scope {a.vout_channel}")
            if up:
                issues.append(
                    f"after enabling, {up} Check the enable path: J6 7-8, J8 1-2 / 9-10, V_33 on J13.14, "
                    f"DIO {bench.cfg.enable.dio_pin} on J13.12"
                )
            if (
                su.ch2 == "vref"
                and su.vref_nominal
                and abs(cm.after - su.vref_nominal) > 0.2 * su.vref_nominal
            ):
                issues.append(
                    f"VREF settles at {cm.after:.3f} V, expected ~{su.vref_nominal} V: is scope {a.ref_channel} on TP3?"
                )
        else:
            down = bench.vout_issue(vm.before, source=f"scope {a.vout_channel}")
            if down:
                issues.append(f"before disabling, {down}")
        edges.append(EdgeStartup(edge, t, vout, ch2, vm, cm, list(dict.fromkeys(issues))))
        if issues:
            log.warning("%s edge: %d setup issue(s), see the plot", edge, len(issues))
    bench.set_enable(False)
    bench.loadbank.off()
    return StartupResult(su.vin, su.ch2, su.averages, su.load_code, i_load, edges, nom)


def plot(res: StartupResult, dut: str):
    """One column per edge: VOUT on top, ch2 below, against time from the EN_CTRL_33 edge.

    Valid edges get the final levels and the 10 % / 90 % markers (VREF and VOUT only); invalid
    edges show the expected VOUT and the issues in a red box.
    """
    n = len(res.edges)
    fig, axes = plotting.figure(2, n, size=(5.4 * n, 5.8), squeeze=False, sharex="col")
    span = res.edges[0].t[-1] - res.edges[0].t[0] if res.edges else 1e-3
    scale, unit = (1e3, "ms") if span > 2e-3 else (1e6, "µs")
    ch2_scale = 1.0 if res.ch2 == "vref" else 1e3
    any_invalid = False
    for col, e in enumerate(res.edges):
        ax_v, ax_c = axes[0][col], axes[1][col]
        x = e.t * scale
        valid = not e.issues
        any_invalid |= not valid
        for ax in (ax_v, ax_c):
            ax.axvline(0, color=plotting.AXIS, lw=0.8, zorder=1)
        ax_v.plot(x, e.vout, color=plotting.SERIES[0], lw=1.4, zorder=3)
        ax_c.plot(x, e.ch2 * ch2_scale, color=plotting.SERIES[1], lw=1.2, zorder=3)
        if valid:
            for ax, m, s in ((ax_v, e.vout_m, 1.0), (ax_c, e.ch2_m, ch2_scale)):
                ax.axhline(m.after * s, color=plotting.INK_2, lw=0.8, ls=(0, (4, 3)), zorder=2)
                if res.ch2 != "vref" and ax is ax_c:
                    continue  # currents: show the level, not 10/90 % markers
                for tt, lab in ((m.t10, "10 %"), (m.t90, "90 %")):
                    if np.isfinite(tt):
                        ax.axvline(tt * scale, color=plotting.INK_2, lw=0.8, ls=(0, (2, 2)), zorder=2)
                        ax.annotate(
                            f"{lab} {tt * 1e6:.1f} µs",
                            (tt * scale, 0.55 if lab == "10 %" else 0.45),
                            xycoords=("data", "axes fraction"),
                            xytext=(3, 0),
                            textcoords="offset points",
                            fontsize=7.5,
                            color=plotting.INK_2,
                        )
        else:
            ax_v.axhline(
                res.vout_nominal,
                color=plotting.CRITICAL,
                lw=1.2,
                ls=(0, (5, 3)),
                zorder=2,
                label=f"Expected VOUT {res.vout_nominal:.3f} V",
            )
            ax_v.legend(loc="center right")
            plotting.issue_box(ax_v, e.issues)
        ax_v.set_title(e.title, fontsize=10, pad=6)
        ax_v.set_ylabel("VOUT (V)")
        ax_c.set_ylabel(CH2_LABEL[res.ch2])
        ax_c.set_xlabel(f"Time from EN_CTRL_33 edge ({unit})")
        ax_v.tick_params(labelbottom=False)
    title = f"Enable startup: {dut}  ·  VIN {res.vin:.2f} V  ·  {res.i_load * 1e3:.1f} mA load  ·  {res.averages} averages"
    if any_invalid:
        title = "SETUP PROBLEM, not a valid measurement\n" + title
    fig.suptitle(
        title,
        x=0.01,
        ha="left",
        fontsize=11,
        fontweight="bold",
        color=plotting.CRITICAL if any_invalid else plotting.INK,
    )
    return fig
