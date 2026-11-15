"""enable-startup: step the enable line and record VOUT, EN_IN and the input current as the LDO turns on and off.

JAFWAK enable path: AD2 DIO -> J13.12 EN_CTRL_33 -> FXLP34 -> EN_CTRL_18 -> J6 7-8 -> EN_AIN
-> enable block -> EN_OUT -> J8 1-2 -> EN_IN (and ~EN_OUT -> J8 9-10 -> ~EN_IN; ~EN_CTRL_33 on
J13.10 is driven inverted for boards jumpered to take it directly). The AD2's V+ supplies V_33.

What is measured:
- AD2 scope 1 ([ad2] vout_channel): VOUT at OUTA. Scope 2 (ref_channel): EN_IN, the LDO's enable after
  the enable block. The AD2 triggers on its own DIO edge (EN_CTRL_33); its t = 0 is that edge.
- 54621A: ch INA on J13 pin 1, ch INB on J13 pin 2, both on xscope_shunt_range centred on VIN, triggered
  on EN_AIN (1.8 V logic) through its Ext Trig input at half of xscope_trigger_high; its t = 0 is that
  crossing, a level-shifter delay after the AD2's. Its channels are ground-referenced (its probe
  grounds and the AD2's are common), so the voltage across R13 is INA - INB. The two channels' offset
  and gain differ by far more than that voltage, so the difference is zeroed at its level while the
  LDO is disabled (before the enable edge, after the disable edge): the shutdown current is far under
  the resolution there. I_in = (INA - INB - zero) / sense_ohm.
No scope goes on VREF: a scope's input loads the BGR and pulls VREF, and VOUT with it, down
(2026-11-14: both read ~0.98 V instead of 1.2 V).

run() steps:
1. Confirm the wiring; AD2 V+ on (V_33), enable low, load code set, PSU ramps VIN, wait off_time.
2. For each edge: arm the 54621A, then `averages` times put the enable in its starting state, wait
   (off_time before an enable edge, on_time before a disable edge), arm an AD2 capture triggered by the
   DIO pin and flip the enable. Average the AD2 captures; collect the 54621A's average.
3. `analysis.step_metrics` on VOUT and on I_in. A current step under RESOLVE_COUNTS of the 54621A's
   8-bit counts is reported as a bound, without timings; the inrush peak is still given when it is
   above that.
4. Validity checks from the data: on the enable edge VOUT must start below 20 % of nominal (else the
   enable path isn't controlling the LDO), settle within the window and end near nominal; on the
   disable edge it must start near nominal and end low. INA or INB at the edge of the 54621A's screen
   (clipped: VIN out of the zoomed window) is a setup problem, and so is no 54621A data at all.
5. Enable low, load bank off.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace

import numpy as np

from ldo_bench import plotting
from ldo_bench import wiring as w
from ldo_bench.analysis import StepMetrics, step_metrics
from ldo_bench.bench import Bench
from ldo_bench.config import StartupConfig
from ldo_bench.instruments.base import Capture, ChannelSetup, InstrumentError
from ldo_bench.procedures import INVALID
from ldo_bench.procedures.transient import CLIP_MARGIN, align_average

log = logging.getLogger(__name__)

NAME = "enable-startup"

XSCOPE_BITS = 8  # the 54621A's ADC
RESOLVE_COUNTS = 4  # a current step must span at least this many scope counts to be measured
NO_METRICS = StepMetrics(*(float("nan"),) * 7)  # columns for a signal that wasn't measured


@dataclass
class ShuntCurrent:
    """The input current through R13 from the 54621A's INA - INB, on its own time axis."""

    t: np.ndarray  # s, 0 at the 54621A's trigger (EN_AIN through half its logic level)
    ina: np.ndarray  # V
    inb: np.ndarray  # V
    a: np.ndarray  # A, (ina - inb - zero_v) / sense_ohm
    zero_v: float  # V, INA - INB with the LDO disabled (the channels' offset difference)
    m: StepMetrics  # timings NaN when not resolved
    resolved: bool  # False = the step is under what the 54621A resolves (its size is a bound)
    floor: float  # A, the smallest step the 54621A resolves on this range and resistor


@dataclass
class EdgeStartup:
    """One enable or disable edge, averaged: VOUT and EN_IN from the AD2, I_in from the 54621A."""

    edge: str  # "rising" = enable, "falling" = disable
    t: np.ndarray  # s, the AD2's time axis, 0 at the EN_CTRL_33 DIO edge
    vout: np.ndarray  # V
    en_in: np.ndarray  # V
    vout_m: StepMetrics
    iin: ShuntCurrent | None  # None when the 54621A gave no data
    issues: list[str] = field(default_factory=list)  # setup problems: non-empty = not a valid edge

    @property
    def title(self) -> str:
        return "Enable (EN_CTRL rising)" if self.edge == "rising" else "Disable (EN_CTRL falling)"


@dataclass
class StartupResult:
    """The whole enable-startup run: one EdgeStartup per edge in startup.edges."""

    vin: float  # V, PSU setpoint
    averages: int  # captures averaged per edge
    load_code: int  # load bank code held during the run
    i_load: float  # A, that code's current at the nominal VOUT
    edges: list[EdgeStartup]
    vout_nominal: float  # V
    xscope_model: str = "54621A"

    @property
    def issues(self) -> list[str]:
        """Every edge's setup problems, each prefixed with its edge."""
        return [f"{e.edge}: {i}" for e in self.edges for i in e.issues]

    @property
    def rows(self) -> list[dict]:
        out = []
        for e in self.edges:
            row = {"edge": e.edge, "valid": not e.issues, "issues": " | ".join(e.issues)}
            row |= {f"vout_{k}": v for k, v in e.vout_m.as_dict().items()}
            c = e.iin
            row |= {f"iin_{k}": v for k, v in (c.m if c else NO_METRICS).as_dict().items()}
            row |= {
                "iin_resolved": c.resolved if c else None,
                "iin_floor_a": c.floor if c else float("nan"),
                "iin_zero_v": c.zero_v if c else float("nan"),
            }
            out.append(row)
        return out

    def extra_csvs(self) -> dict[str, list[dict]]:
        """waveform_<edge>.csv (AD2: VOUT, EN_IN) and waveform_<edge>_iin.csv (54621A: INA, INB, I_in)."""
        out = {}
        for e in self.edges:
            out[f"waveform_{e.edge}.csv"] = [
                {"t_s": float(t), "vout_v": float(v), "en_in_v": float(n)}
                for t, v, n in zip(e.t, e.vout, e.en_in, strict=True)
            ]
            if e.iin is not None:
                c = e.iin
                out[f"waveform_{e.edge}_iin.csv"] = [
                    {"t_s": float(t), "ina_v": float(a), "inb_v": float(b), "iin_a": float(i)}
                    for t, a, b, i in zip(c.t, c.ina, c.inb, c.a, strict=True)
                ]
        return out

    def summary(self) -> dict:
        return {
            "valid": not self.issues,
            "issues": self.issues,
            "vin": self.vin,
            "averages": self.averages,
            "load_code": self.load_code,
            "i_load_a": self.i_load,
            "edges": {
                e.edge: {
                    "vout": e.vout_m.as_dict(),
                    "iin": (
                        e.iin.m.as_dict()
                        | {"resolved": e.iin.resolved, "floor_a": e.iin.floor, "zero_v": e.iin.zero_v}
                        if e.iin
                        else None
                    ),
                    "issues": e.issues,
                }
                for e in self.edges
            },
        }

    def report(self) -> str:
        lines = [
            f"Enable startup at VIN {self.vin:.2f} V, load {self.i_load * 1e3:.1f} mA (code {self.load_code}), "
            f"{self.averages} averages. VOUT: t = 0 at the EN_CTRL_33 edge (AD2); I_in: t = 0 at EN_AIN "
            f"(the {self.xscope_model}'s trigger), zeroed at the disabled level:"
        ]

        def us(x):
            return "n/a" if not np.isfinite(x) else f"{x * 1e6:.1f} us"

        for e in self.edges:
            lines.append(f"  {e.title}")
            if e.issues:
                lines.append(f"    {INVALID}")
                lines += [f"      - {i}" for i in e.issues]
            v = e.vout_m
            word = "rise" if e.edge == "rising" else "fall"
            lines.append(
                f"    VOUT   {v.before:.4f} -> {v.after:.4f} V;  10 % at {us(v.t10)}, 90 % at {us(v.t90)} "
                f"({word} {us(v.rise_time)}), settled ({us(v.t_settle)}), overshoot {v.overshoot * 100:.1f} %"
            )
            c = e.iin
            if c is None:
                lines.append("    I_in   not measured")
            elif not c.resolved:
                # the level change is quantisation, but an inrush spike well above the floor is real
                peak = f";  peak {c.m.peak * 1e3:.1f} mA" if abs(c.m.peak) >= c.floor else ""
                lines.append(
                    f"    I_in   step under what the {self.xscope_model} resolves across R13 "
                    f"(< {c.floor * 1e3:.1f} mA; use `iq` for DC current){peak}"
                )
            else:
                lines.append(
                    f"    I_in   {c.m.before * 1e3:.2f} -> {c.m.after * 1e3:.2f} mA (+/- "
                    f"{c.floor / RESOLVE_COUNTS * 1e3:.1f} mA per count);  peak {c.m.peak * 1e3:.1f} mA"
                )
        return "\n".join(lines)


def wiring(bench: Bench, su: StartupConfig) -> list[str]:
    """Wiring lines shown before the run, one connection per line (see the module docstring)."""
    cfg = bench.cfg
    b = cfg.board
    lines = [
        *w.psu_on_vin(cfg),
        *w.j1_rails(),
        *w.enable_dio(cfg),
        w.scope_vout(cfg),
        w.scope(cfg, "ref", b.enable_ldo_in, note="EN_IN, the LDO's enable"),
        f"54621A ch{su.xscope_ina_channel}+ -> J13 pin 1 (INA), 54621A ch{su.xscope_ina_channel} ground -> "
        f"{b.probe_gnd}",
        f"54621A ch{su.xscope_inb_channel}+ -> J13 pin 2 (INB), 54621A ch{su.xscope_inb_channel} ground -> "
        f"{b.probe_gnd}",
        f"54621A Ext Trig -> EN_AIN (TP6) ({su.xscope_trigger_high:g} V logic, the trigger)",
        "54621A ground clips on GND only, never on INB: its grounds and the AD2's are all common",
    ]
    if su.load_code:
        lines += w.load_bank(cfg)
    return [*lines, w.ad2_gnd(cfg)]


def _capture_edge(
    bench: Bench, su: StartupConfig, setups, edge: str
) -> tuple[Capture, list[str], dict | None, str | None]:
    """`su.averages` AD2 captures of one enable edge, averaged, plus the 54621A's average of the same edges.

    Returns (AD2 capture, AD2 trigger issues, 54621A traces by channel or None, why there are none).
    If an AD2 capture never triggers, a forced capture is taken and the average of the ones that did
    trigger (or the forced one) is returned with an issue; the 54621A is still collected.
    """
    on = edge == "rising"
    pin = bench.cfg.enable.dio_pin
    xs, x_issue, x_traces = bench.xscope, None, None
    if xs is None:
        x_issue = bench.xscope_issue or "the 54621A isn't connected (no [xscope], or it is disabled)"
    elif xs.busy:
        xs, x_issue = None, f"skipped: the {xs.model} is still busy from an earlier edge (press Run/Stop)"
    if xs is not None:
        hi = su.xscope_trigger_high  # EN_AIN: 1.8 V logic
        try:
            xs.arm(
                window=su.window,
                pre_trigger=su.pre_trigger,
                vout_channel=su.xscope_ina_channel,  # the driver's two data channels: INA and INB
                vout_range=su.xscope_shunt_range,
                vout_offset=su.vin,
                ref_channel=su.xscope_inb_channel,
                ref_range=su.xscope_shunt_range,  # the same scale, so INA - INB means something
                ref_offset=su.vin,
                trigger_level=hi / 2,
                edge=edge,
                averages=su.averages,
                points=bench.cfg.xscope.points,
                trigger="external",
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


def _clip_issue(v: np.ndarray, su: StartupConfig, name: str, ch: int) -> str | None:
    """A setup problem if a shunt channel touches the top or bottom of the 54621A's zoomed screen."""
    lo, hi = su.vin - su.xscope_shunt_range / 2, su.vin + su.xscope_shunt_range / 2
    margin = CLIP_MARGIN * su.xscope_shunt_range
    n = int(np.sum((v <= lo + margin) | (v >= hi - margin)))
    if not n:
        return None
    return (
        f"54621A ch{ch} ({name}) is clipped ({n} of {len(v)} points at the edge of its {lo:.3f} to {hi:.3f} V "
        "screen), so I_in there isn't a measurement: is it on its pin, and is VIN at startup.vin? If VIN sags "
        "at startup (the supply's current limit?), widen startup.xscope_shunt_range"
    )


def _shunt(t: np.ndarray, ina: np.ndarray, inb: np.ndarray, su: StartupConfig, edge: str) -> ShuntCurrent:
    """INA - INB -> I_in, zeroed at the disabled level, with the 54621A's resolution bound."""
    diff = ina - inb
    tail = t >= t[0] + 0.9 * (t[-1] - t[0])  # last 10 %, as step_metrics' `after`
    disabled = t < 0 if edge == "rising" else tail
    zero = float(np.mean(diff[disabled])) if disabled.any() else float("nan")
    a = (diff - zero) / su.sense_ohm
    m = step_metrics(t, a, su.settle_fraction)
    # Four 8-bit counts across R13: a smaller step is quantisation, not a current; its timing is
    # meaningless and its size only a bound. (Averaging adds resolution only as far as noise dithers it.)
    floor = RESOLVE_COUNTS * su.xscope_shunt_range / 2**XSCOPE_BITS / su.sense_ohm
    resolved = abs(m.step) >= floor
    if not resolved:
        m = replace(m, t10=float("nan"), t90=float("nan"), t_settle=float("nan"), overshoot=float("nan"))
    return ShuntCurrent(t, ina, inb, a, zero, m, resolved, floor)


def _vout_issues(bench: Bench, su: StartupConfig, edge: str, vm: StepMetrics) -> list[str]:
    """Setup checks on VOUT around one edge (step 4 in the module docstring)."""
    nom, issues = bench.cfg.dut.vout_nominal, []
    source = f"scope {bench.cfg.ad2.vout_channel}"
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
    bench.loadbank.require([su.load_code])  # dut.i_max with any external load, before power-up
    bench.confirm_wiring(
        "enable startup",
        wiring(bench, su),
        ref_probe=("en_in", 0),
        en_pin=bench.cfg.enable.dio_pin,
    )
    setups = {
        a.vout_channel: ChannelSetup(a.vout.range, a.vout.offset),
        a.ref_channel: ChannelSetup(su.en_in_range, 0.0),
    }
    bench.enable_logic(True)
    bench.set_enable(False)
    bench.loadbank.set(su.load_code)
    bench.vin_from_psu(su.vin)
    bench.sleep(su.off_time)
    i_load = bench.loadbank.current(su.load_code, nom)
    model = getattr(bench.xscope, "model", None) or "54621A"

    edges = []
    for edge in su.edges:
        cap, issues, x_traces, x_issue = _capture_edge(bench, su, setups, edge)
        t = cap.time
        vout, en_in = cap.channel(a.vout_channel), cap.channel(a.ref_channel)
        vm = step_metrics(t, vout, su.settle_fraction)
        issues += _vout_issues(bench, su, edge, vm)
        iin = None
        if x_traces is None:
            issues.append(f"the input current wasn't measured ({x_issue})")
        else:
            ina, inb = x_traces[su.xscope_ina_channel], x_traces[su.xscope_inb_channel]
            for name, tr, ch in (("INA", ina, su.xscope_ina_channel), ("INB", inb, su.xscope_inb_channel)):
                if clipped := _clip_issue(tr.v, su, name, ch):
                    issues.append(clipped)
            iin = _shunt(ina.t, ina.v, inb.v, su, edge)
        edges.append(EdgeStartup(edge, t, vout, en_in, vm, iin, list(dict.fromkeys(issues))))
        if issues:
            log.warning("%s edge: %d setup issue(s), see the plot", edge, len(issues))
    bench.set_enable(False)
    bench.loadbank.off()
    return StartupResult(su.vin, su.averages, su.load_code, i_load, edges, nom, model)


def plot(res: StartupResult, dut: str):
    """One column per edge: VOUT, EN_IN (AD2) and I_in (54621A), against time from the edge.

    Valid edges get the final levels and VOUT's 10 % / 90 % markers; invalid edges show the expected
    VOUT and the issues in a red box.
    """
    n = len(res.edges)
    fig, axes = plotting.figure(3, n, size=(5.4 * n, 8.4), squeeze=False, sharex="col")
    span = res.edges[0].t[-1] - res.edges[0].t[0] if res.edges else 1e-3
    scale, unit = (1e3, "ms") if span > 2e-3 else (1e6, "µs")
    any_invalid = False
    for col, e in enumerate(res.edges):
        ax_v, ax_e, ax_i = (axes[r][col] for r in range(3))
        valid = not e.issues
        any_invalid |= not valid
        for ax in (ax_v, ax_e, ax_i):
            ax.axvline(0, color=plotting.AXIS, lw=0.8, zorder=1)
        x = e.t * scale
        ax_v.plot(x, e.vout, color=plotting.SERIES[0], lw=1.4, zorder=3)
        ax_e.plot(x, e.en_in, color=plotting.SERIES[3], lw=1.2, zorder=3)
        c = e.iin
        if c is None:
            ax_i.text(
                0.5, 0.5, "I_in not measured", transform=ax_i.transAxes, ha="center", color=plotting.INK_2
            )
        else:
            ax_i.plot(c.t * scale, c.a * 1e3, color=plotting.SERIES[1], lw=1.2, zorder=3)
            if valid and c.resolved:
                ax_i.axhline(c.m.after * 1e3, color=plotting.INK_2, lw=0.8, ls=(0, (4, 3)), zorder=2)
            elif not c.resolved:
                plotting.subtitle(
                    ax_i, f"level change under the {res.xscope_model}'s resolution (< {c.floor * 1e3:.0f} mA)"
                )
        if valid:
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
        ax_e.set_ylabel("EN_IN (V)")
        ax_i.set_ylabel(f"I_in (mA), {res.xscope_model}")
        ax_i.set_xlabel(f"Time from the enable edge ({unit}); I_in from EN_AIN")
        for ax in (ax_v, ax_e):
            ax.tick_params(labelbottom=False)
    title = f"Enable startup: {dut}  ·  VIN {res.vin:.2f} V  ·  {res.i_load * 1e3:.1f} mA load  ·  {res.averages} averages"
    plotting.suptitle(fig, title, invalid=any_invalid)
    return fig
