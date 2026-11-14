"""enable-startup: step the enable line and record how VOUT and the supply currents respond.

JAFWAK enable path: AD2 DIO -> J13.12 EN_CTRL_33 -> FXLP34 -> EN_CTRL_18 -> J6 7-8 -> EN_AIN
-> enable block -> EN_OUT -> J8 1-2 -> EN_IN (and ~EN_OUT -> J8 9-10 -> ~EN_IN; ~EN_CTRL_33 on
J13.10 is driven inverted for boards jumpered to take it directly). The AD2's V+ supplies V_33.

What is measured (startup.channels):
- 'currents' (default): AD2 scope 1 across R13 (input current), scope 2 across R1 (output current),
  both differential; VOUT on the [xscope] 54621A at OUTA, its other channel on EN_AIN (TP6, 1.8 V
  logic after the level shifter), which it triggers on at half of startup.xscope_trigger_high. The 54621A's averaged acquisition is armed before the
  AD2's captures and sees the same edges (as in load-tran); both time axes have t = 0 at the edge.
- 'iin' / 'iout': the AD2 alone: scope 1 on VOUT, scope 2 across R13 or R1.
No mode puts a scope on VREF: a scope's input loads the BGR and pulls VREF, and VOUT with it, down
(2026-11-14: both read ~0.98 V instead of 1.2 V).

The AD2 triggers on its own DIO edge. Each edge (enable, disable) is captured `averages` times and
averaged; t = 0 is the DIO edge, so delays include the level shifter and the enable block.

run() steps:
1. Confirm the wiring; AD2 V+ on (V_33), enable low, load code set, PSU ramps VIN, wait off_time.
2. For each edge: arm the 54621A ('currents'), then `averages` times put the enable in its starting
   state, wait (off_time before an enable edge, on_time before a disable edge), arm an AD2 capture
   triggered by the DIO pin and flip the enable. Average the AD2 captures; collect the 54621A's.
3. `analysis.step_metrics` on VOUT and on each current (volts across the resistor / sense_ohm).
   A current step under RESOLVE_COUNTS AD2 counts is reported as a bound, without timings.
4. Validity checks from the data: on the enable edge VOUT must start below 20 % of nominal (else
   the enable path isn't controlling the LDO), settle within the window and end near nominal; on
   the disable edge it must start near nominal and end low. In 'currents' mode with no VOUT from the
   54621A (not connected, busy, failed) these checks can't run, so the edge is a setup problem; the
   currents are still saved.
5. Enable low, load bank off.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from typing import Literal

import numpy as np

from ldo_bench import plotting
from ldo_bench import wiring as w
from ldo_bench.analysis import StepMetrics, step_metrics
from ldo_bench.bench import Bench
from ldo_bench.config import StartupConfig
from ldo_bench.instruments.base import Capture, ChannelSetup, InstrumentError
from ldo_bench.procedures import INVALID
from ldo_bench.procedures.transient import align_average

log = logging.getLogger(__name__)

NAME = "enable-startup"

AD2_BITS = 14  # scope ADC resolution
RESOLVE_COUNTS = 4  # a current step must span at least this many AD2 counts to be measured
CURRENT_LABEL = {"iin": "Input current I_in (mA)", "iout": "Output current I_out (mA)"}
CURRENT_NAME = {"iin": "I_in", "iout": "I_out"}
SHUNT = {  # where each current is measured: (+, -, note)
    "iin": ("J13 pin 1 (INA)", "J13 pin 2 (INB)", "across R13: input current"),
    "iout": ("J13 pin 4 (OUTA)", "J13 pin 3 (OUTB)", "across R1: output current"),
}
NO_METRICS = StepMetrics(*(float("nan"),) * 7)  # VOUT's columns when it wasn't measured


@dataclass
class CurrentTrace:
    """One current (input through R13 or output through R1) around an edge, on the AD2's time axis."""

    name: Literal["iin", "iout"]
    a: np.ndarray  # A
    m: StepMetrics  # timings NaN when not resolved
    resolved: bool  # False = the step is under what the AD2 resolves (its size is a bound)
    floor: float  # A, the smallest step the AD2 resolves on this range and resistor


@dataclass
class EdgeStartup:
    """One enable or disable edge, averaged: VOUT (from the AD2 or the 54621A) and the currents."""

    edge: str  # "rising" = enable, "falling" = disable
    t: np.ndarray  # s, the AD2's time axis, 0 at the EN_CTRL_33 DIO edge
    currents: list[CurrentTrace]
    vout_t: np.ndarray  # s, VOUT's own time axis (the 54621A's in 'currents' mode); empty if not measured
    vout: np.ndarray  # V; empty if not measured
    vout_m: StepMetrics | None  # None if VOUT wasn't measured
    vout_source: str  # e.g. "54621A ch1" or "AD2 scope 1"
    issues: list[str] = field(default_factory=list)  # setup problems: non-empty = not a valid edge

    @property
    def title(self) -> str:
        return "Enable (EN_CTRL rising)" if self.edge == "rising" else "Disable (EN_CTRL falling)"


@dataclass
class StartupResult:
    """The whole enable-startup run: one EdgeStartup per edge in startup.edges."""

    vin: float  # V, PSU setpoint
    channels: str  # "currents", "iin" or "iout"
    averages: int  # captures averaged per edge
    load_code: int  # load bank code held during the run
    i_load: float  # A, that code's current at the nominal VOUT
    edges: list[EdgeStartup]
    vout_nominal: float  # V

    @property
    def issues(self) -> list[str]:
        """Every edge's setup problems, each prefixed with its edge."""
        return [f"{e.edge}: {i}" for e in self.edges for i in e.issues]

    @property
    def rows(self) -> list[dict]:
        out = []
        for e in self.edges:
            row = {"edge": e.edge, "valid": not e.issues, "issues": " | ".join(e.issues)}
            row |= {f"vout_{k}": v for k, v in (e.vout_m or NO_METRICS).as_dict().items()}
            for c in e.currents:
                row |= {f"{c.name}_{k}": v for k, v in c.m.as_dict().items()}
                row |= {f"{c.name}_resolved": c.resolved, f"{c.name}_floor_a": c.floor}
            out.append(row)
        return out

    def extra_csvs(self) -> dict[str, list[dict]]:
        """waveform_<edge>.csv on the AD2's time axis; VOUT from the 54621A in waveform_<edge>_vout.csv."""
        out = {}
        for e in self.edges:
            on_ad2 = e.vout_source.startswith("AD2")
            rows = []
            for k, t in enumerate(e.t):
                row = {"t_s": float(t)}
                if on_ad2:
                    row["vout_v"] = float(e.vout[k])
                row |= {f"{c.name}_a": float(c.a[k]) for c in e.currents}
                rows.append(row)
            out[f"waveform_{e.edge}.csv"] = rows
            if not on_ad2 and len(e.vout):
                out[f"waveform_{e.edge}_vout.csv"] = [
                    {"t_s": float(t), "vout_v": float(v)} for t, v in zip(e.vout_t, e.vout, strict=True)
                ]
        return out

    def summary(self) -> dict:
        return {
            "valid": not self.issues,
            "issues": self.issues,
            "vin": self.vin,
            "channels": self.channels,
            "averages": self.averages,
            "load_code": self.load_code,
            "i_load_a": self.i_load,
            "edges": {
                e.edge: {
                    "vout": e.vout_m.as_dict() if e.vout_m else None,
                    "vout_source": e.vout_source,
                    **{
                        c.name: c.m.as_dict() | {"resolved": c.resolved, "floor_a": c.floor}
                        for c in e.currents
                    },
                    "issues": e.issues,
                }
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
                lines.append(f"    {INVALID}")
                lines += [f"      - {i}" for i in e.issues]
            if e.vout_m is not None and not e.issues:
                v = e.vout_m
                word = "rise" if e.edge == "rising" else "fall"
                lines.append(
                    f"    VOUT   {v.before:.4f} -> {v.after:.4f} V ({e.vout_source});  10 % at {us(v.t10)}, "
                    f"90 % at {us(v.t90)} ({word} {us(v.rise_time)}), settled ({us(v.t_settle)}), "
                    f"overshoot {v.overshoot * 100:.1f} %"
                )
            for c in e.currents:
                name = CURRENT_NAME[c.name]
                if not c.resolved:
                    # the level change is quantisation, but an inrush spike well above the floor is real
                    peak = f";  peak {c.m.peak * 1e3:.1f} mA" if abs(c.m.peak) >= c.floor else ""
                    lines.append(
                        f"    {name:6s} step under what the AD2 resolves across the sense resistor "
                        f"(< {c.floor * 1e3:.1f} mA; use `iq` for DC current){peak}"
                    )
                else:
                    lines.append(
                        f"    {name:6s} {c.m.before * 1e3:.2f} -> {c.m.after * 1e3:.2f} mA (+/- "
                        f"{c.floor / RESOLVE_COUNTS * 1e3:.1f} mA per count);  peak {c.m.peak * 1e3:.1f} mA"
                    )
        return "\n".join(lines)


def current_names(su: StartupConfig) -> list[Literal["iin", "iout"]]:
    """The currents this mode measures, in AD2 channel order for 'currents' (scope 1, scope 2)."""
    return ["iin", "iout"] if su.channels == "currents" else [su.channels]


def wiring(bench: Bench, su: StartupConfig) -> list[str]:
    """Wiring lines shown before the run, one connection per line (see the module docstring)."""
    cfg = bench.cfg
    if su.channels == "currents":
        probes = [
            w.scope(cfg, "vout", *SHUNT["iin"]),  # AD2 scope 1 (the [ad2] vout_channel) across R13
            w.scope(cfg, "ref", *SHUNT["iout"]),
        ]
        if cfg.xscope is not None:
            probes += [
                w.xscope(cfg, "vout", cfg.board.vout_probe, note="VOUT"),
                w.xscope(cfg, "ref", "EN_AIN (TP6)", note=f"{su.xscope_trigger_high:g} V logic, the trigger"),
            ]
    else:
        probes = [w.scope_vout(cfg), w.scope(cfg, "ref", *SHUNT[su.channels])]
    lines = [*w.psu_on_vin(cfg), *w.j1_rails(), *w.enable_dio(cfg), *probes]
    if su.load_code:
        lines += w.load_bank(cfg)
    return [*lines, w.ad2_gnd(cfg)]


def _capture_edge(
    bench: Bench, su: StartupConfig, setups, edge: str, use_xscope: bool
) -> tuple[Capture, list[str], dict | None, str | None]:
    """`su.averages` AD2 captures of one enable edge, averaged, plus the 54621A's view of the same edges.

    Returns (AD2 capture, AD2 trigger issues, 54621A traces by channel or None, why there are none).
    If an AD2 capture never triggers, a forced capture is taken and the average of the ones that did
    trigger (or the forced one) is returned with an issue; the 54621A is still collected.
    """
    on = edge == "rising"
    pin = bench.cfg.enable.dio_pin
    xs, x_issue, x_traces = (bench.xscope if use_xscope else None), None, None
    if use_xscope and xs is None:
        x_issue = bench.xscope_issue or "the 54621A isn't connected (no [xscope], or it is disabled)"
    elif xs is not None and xs.busy:
        xs, x_issue = None, f"skipped: the {xs.model} is still busy from an earlier edge (press Run/Stop)"
    if xs is not None:
        xc, hi = bench.cfg.xscope, su.xscope_trigger_high  # EN_AIN: 1.8 V logic
        try:
            xs.arm(
                window=su.window,
                pre_trigger=su.pre_trigger,
                vout_channel=xc.vout_channel,
                vout_range=su.xscope_vout_range,
                vout_offset=bench.cfg.dut.vout_nominal / 2,
                ref_channel=xc.ref_channel,
                ref_range=2 * hi,
                ref_offset=hi / 2,
                trigger_level=hi / 2,
                edge=edge,
                averages=su.averages,
                points=xc.points,
            )
            bench.sleep(0.3)  # let :DIGitize start before the first edge
        except InstrumentError as e:
            xs, x_issue = None, f"the {xs.model} could not be armed: {e}"

    def start() -> None:
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
    caps, issues = [], []
    cap = None
    for k in range(su.averages):
        start()
        try:
            caps.append(bench.scope.capture(**kw))
        except InstrumentError as e:
            log.warning(
                "%s capture %d: no trigger on DIO %d (%s); recording untriggered", edge, k + 1, pin, e
            )
            issues.append(f"the scope never triggered on DIO {pin} ({edge}): plotted an untriggered capture")
            start()
            forced = bench.scope.capture(**kw, force=True)
            cap = align_average(caps) if caps else forced
            break
    if cap is None:
        cap = align_average(caps)

    if xs is not None:

        def again() -> None:
            start()
            bench.set_enable(on)

        try:  # up to 2 x averages extra edges if it missed some of the AD2's
            x_traces = xs.collect(again, 2 * su.averages, bench.cfg.xscope.timeout)
        except InstrumentError as e:
            x_issue = f"the {xs.model}: {e}"
    return cap, issues, x_traces, x_issue


def _current(name, t, raw: np.ndarray, su: StartupConfig) -> CurrentTrace:
    """Volts across the sense resistor -> a CurrentTrace (Ohm's law), with the AD2's resolution bound."""
    a = raw / su.sense_ohm
    m = step_metrics(t, a, su.settle_fraction)
    # Four AD2 counts across the resistor: a smaller step is quantisation, not a current; its timing is
    # meaningless and its size only a bound.
    floor = RESOLVE_COUNTS * su.sense_range / 2**AD2_BITS / su.sense_ohm
    resolved = abs(m.step) >= floor
    if not resolved:
        m = replace(m, t10=float("nan"), t90=float("nan"), t_settle=float("nan"), overshoot=float("nan"))
    return CurrentTrace(name, a, m, resolved, floor)


def _vout_issues(bench: Bench, su: StartupConfig, edge: str, vm: StepMetrics, source: str) -> list[str]:
    """Setup checks on VOUT around one edge (step 4 in the module docstring)."""
    nom, issues = bench.cfg.dut.vout_nominal, []
    if edge == "rising":
        # a disabled LDO should sit near 0 V; 20 % of nominal leaves room for leakage and noise
        if vm.before > 0.2 * nom:
            issues.append(
                f"VOUT was already {vm.before:.3f} V before the enable edge: the enable path isn't "
                "controlling the LDO (J6 7-8, J8 1-2 / 9-10?)"
            )
        if not np.isfinite(vm.t_settle):
            issues.append(
                f"VOUT hasn't settled by the end of the {su.window * 1e3:g} ms capture: the timings would be "
                "relative to a level it hadn't reached. Lengthen the window (startup.window / --window-ms)"
            )
        if up := bench.vout_issue(vm.after, source=source):
            issues.append(
                f"after enabling, {up} Check the enable path: J6 7-8, J8 1-2 / 9-10, V_33 on J13.14, "
                f"DIO {bench.cfg.enable.dio_pin} on J13.12"
            )
    else:
        if down := bench.vout_issue(vm.before, source=source):
            issues.append(f"before disabling, {down}")
        elif vm.after > 0.2 * nom:  # same 20 % as "already up" on the enable edge
            issues.append(
                f"VOUT is still {vm.after:.3f} V at the end of the capture after disabling: the enable path "
                "isn't turning the LDO off, or it discharges slower than the window (lengthen it)"
            )
    return issues


def run(bench: Bench, su: StartupConfig) -> StartupResult:
    """Capture each edge in su.edges (see the module docstring for the steps and checks).

    su: the [startup] section. Setup problems are recorded per edge, never raised.
    """
    a = bench.cfg.ad2
    nom = bench.cfg.dut.vout_nominal
    both = su.channels == "currents"
    bench.loadbank.require([su.load_code])  # dut.i_max with any external load, before power-up
    bench.confirm_wiring(
        "enable startup",
        wiring(bench, su),
        ref_probe=("iout" if both else su.channels, 0),
        ch1_probe="iin" if both else "vout",
        en_pin=bench.cfg.enable.dio_pin,
    )
    sense = ChannelSetup(su.sense_range, 0.0)
    setups = {
        a.vout_channel: sense if both else ChannelSetup(a.vout.range, a.vout.offset),
        a.ref_channel: sense,
    }
    bench.enable_logic(True)
    bench.set_enable(False)
    bench.loadbank.set(su.load_code)
    bench.vin_from_psu(su.vin)
    bench.sleep(su.off_time)
    i_load = bench.loadbank.current(su.load_code, nom)

    edges = []
    for edge in su.edges:
        cap, issues, x_traces, x_issue = _capture_edge(bench, su, setups, edge, use_xscope=both)
        t = cap.time
        if both:
            currents = [
                _current("iin", t, cap.channel(a.vout_channel), su),
                _current("iout", t, cap.channel(a.ref_channel), su),
            ]
        else:
            currents = [_current(su.channels, t, cap.channel(a.ref_channel), su)]

        if not both:
            vout_t, vout, source = t, cap.channel(a.vout_channel), f"AD2 scope {a.vout_channel}"
        elif x_traces is not None:
            xc, model = bench.cfg.xscope, getattr(bench.xscope, "model", "54621A")
            trace = x_traces[xc.vout_channel]
            # its own trigger: EN_AIN through half its logic level, just after the AD2's DIO edge
            vout_t, vout = trace.t, trace.v
            source = f"{model} ch{xc.vout_channel}, t = 0 at EN_AIN {su.xscope_trigger_high / 2:g} V"
        else:
            vout_t, vout, source = np.array([]), np.array([]), "not measured"
            issues.append(
                f"VOUT wasn't measured ({x_issue}), so the enable path and VOUT level couldn't be checked; "
                "the currents are saved as measured"
            )
        vm = step_metrics(vout_t, vout, su.settle_fraction) if len(vout) else None
        if vm is not None:
            issues += _vout_issues(bench, su, edge, vm, source)
        edges.append(EdgeStartup(edge, t, currents, vout_t, vout, vm, source, list(dict.fromkeys(issues))))
        if issues:
            log.warning("%s edge: %d setup issue(s), see the plot", edge, len(issues))
    bench.set_enable(False)
    bench.loadbank.off()
    return StartupResult(su.vin, su.channels, su.averages, su.load_code, i_load, edges, nom)


def plot(res: StartupResult, dut: str):
    """One column per edge: VOUT on top, then each current, against time from the EN_CTRL_33 edge.

    Valid edges get the final levels and VOUT's 10 % / 90 % markers; invalid edges show the expected
    VOUT and the issues in a red box.
    """
    n = len(res.edges)
    n_cur = len(res.edges[0].currents) if res.edges else 1
    rows = 1 + n_cur
    fig, axes = plotting.figure(rows, n, size=(5.4 * n, 2.9 * rows), squeeze=False, sharex="col")
    span = res.edges[0].t[-1] - res.edges[0].t[0] if res.edges else 1e-3
    scale, unit = (1e3, "ms") if span > 2e-3 else (1e6, "µs")
    any_invalid = False
    for col, e in enumerate(res.edges):
        ax_v, ax_cs = axes[0][col], [axes[1 + k][col] for k in range(n_cur)]
        valid = not e.issues
        any_invalid |= not valid
        for ax in (ax_v, *ax_cs):
            ax.axvline(0, color=plotting.AXIS, lw=0.8, zorder=1)
        if len(e.vout):
            ax_v.plot(e.vout_t * scale, e.vout, color=plotting.SERIES[0], lw=1.4, zorder=3)
        else:
            ax_v.text(
                0.5, 0.5, "VOUT not measured", transform=ax_v.transAxes, ha="center", color=plotting.INK_2
            )
        for k, (ax, c) in enumerate(zip(ax_cs, e.currents, strict=True)):
            ax.plot(e.t * scale, c.a * 1e3, color=plotting.SERIES[1 + k], lw=1.2, zorder=3)
            ax.set_ylabel(CURRENT_LABEL[c.name])
            if valid and c.resolved:
                ax.axhline(c.m.after * 1e3, color=plotting.INK_2, lw=0.8, ls=(0, (4, 3)), zorder=2)
            elif not c.resolved:
                plotting.subtitle(ax, f"under the AD2's resolution: step < {c.floor * 1e3:.1f} mA")
        if valid and e.vout_m is not None:
            m = e.vout_m
            ax_v.axhline(m.after, color=plotting.INK_2, lw=0.8, ls=(0, (4, 3)), zorder=2)
            for tt, lab in ((m.t10, "10 %"), (m.t90, "90 %")):
                if np.isfinite(tt):
                    ax_v.axvline(tt * scale, color=plotting.INK_2, lw=0.8, ls=(0, (2, 2)), zorder=2)
                    ax_v.annotate(
                        f"{lab} {tt * 1e6:.1f} µs",
                        (tt * scale, 0.55 if lab == "10 %" else 0.45),
                        xycoords=("data", "axes fraction"),
                        xytext=(3, 0),
                        textcoords="offset points",
                        fontsize=7.5,
                        color=plotting.INK_2,
                    )
        elif not valid:
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
        if len(e.vout):  # where VOUT came from and its time zero
            ax_v.text(
                0.99,
                0.04,
                e.vout_source,
                transform=ax_v.transAxes,
                ha="right",
                fontsize=7.5,
                color=plotting.INK_2,
            )
        ax_cs[-1].set_xlabel(f"Time from EN_CTRL_33 edge ({unit})")
        for ax in (ax_v, *ax_cs[:-1]):
            ax.tick_params(labelbottom=False)
    title = f"Enable startup: {dut}  ·  VIN {res.vin:.2f} V  ·  {res.i_load * 1e3:.1f} mA load  ·  {res.averages} averages"
    plotting.suptitle(fig, title, invalid=any_invalid)
    return fig
