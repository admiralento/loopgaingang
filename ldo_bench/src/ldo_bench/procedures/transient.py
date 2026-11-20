"""Shared machinery for the transient tests: repeated triggered captures, aligned and averaged.

Used by load-tran and line-tran. A transient test repeats the same step (load bank code change or
VIN step) `averages` times. Each repetition is one triggered AD2 capture of two channels: VOUT and
the "ref" channel, which carries the stimulus and is also the trigger source. Because the LDO's
response is the same on every repetition, averaging the captures with their trigger points lined
up (`align_average`) removes random noise without smearing the response.

Flow for one edge (the test module calls these in order):

1. `averaged_step` arms the optional 54621A cross-check scope, then takes the AD2 captures and
   averages them. If the AD2 never triggers it records one forced (untriggered) capture instead of
   failing, so the plot shows what was actually on the channels.
2. The test runs `analysis.settling_analysis` on the averaged VOUT / ref traces.
3. `capture_issues` checks the data for setup problems (no edge on ref, ref swinging the wrong
   amount, VOUT not near nominal before the step). Any issue makes the edge invalid: its data is
   still saved and plotted, but no metrics are reported.
4. `analyse_xcheck` runs the same analysis on the cross-check scope's traces. Its problems are
   reported separately and never make the AD2 result invalid.
5. `edge_report` / `plot_edges` format the result (one report block / plot column per edge).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field

import numpy as np

from ldo_bench import plotting
from ldo_bench.analysis import SettlingResult, settling_analysis
from ldo_bench.bench import Bench
from ldo_bench.config import AnalysisConfig, TransientCaptureConfig
from ldo_bench.instruments.base import Capture, ChannelSetup, Edge, InstrumentError

log = logging.getLogger(__name__)


def align_average(captures: list[Capture]) -> Capture:
    """Average captures sample-by-sample with their trigger points lined up.

    Each capture's trigger_index can differ by a few samples (the driver finds the edge in the
    data), so every capture is cut to the same number of samples before and after its own trigger:
    the largest window that all of them cover. The result's trigger_index is that common pre-trigger
    length, and edge_found is True only if every capture found its edge. Raises ValueError if the
    list is empty.
    """
    if not captures:
        raise ValueError("no captures")
    # common window: the shortest pre-trigger and post-trigger stretch among the captures
    pre = min(c.trigger_index for c in captures)
    post = min(len(c.ch1) - c.trigger_index for c in captures)

    def cut(x: np.ndarray, c: Capture) -> np.ndarray:
        return x[c.trigger_index - pre : c.trigger_index + post]

    ch1 = np.mean([cut(c.ch1, c) for c in captures], axis=0)
    ch2 = np.mean([cut(c.ch2, c) for c in captures], axis=0)
    return Capture(captures[0].sample_rate, ch1, ch2, pre, edge_found=all(c.edge_found for c in captures))


SWING_TOLERANCE = 0.3  # the trigger channel's step should be within 30 % of what we commanded


def capture_issues(
    bench: Bench, metrics, cap: Capture, ref: np.ndarray, *, expected_swing: float, ref_desc: str
) -> list[str]:
    """Setup problems visible in a transient capture. Empty list = it shows the real stimulus and VOUT.

    metrics: the SettlingResult from settling_analysis on this capture. ref: the ref channel's
    samples (V). expected_swing: the step the ref channel should show (V), e.g. the DIO high level
    or vin_high - vin_low. ref_desc: what the ref channel should be on, used in the messages.

    Checks, in order: the driver found an edge in the data; the analysis found the step; the ref
    channel's swing is within SWING_TOLERANCE of expected_swing (a probe on the wrong signal);
    VOUT before the trigger is near dut.vout_nominal (bench.vout_issue).
    """
    ch, vch = bench.cfg.ad2.ref_channel, bench.cfg.ad2.vout_channel
    issues = []
    if not cap.edge_found:
        issues.append(f"the scope triggered but scope {ch}'s data shows no clean edge: is it on {ref_desc}?")
    if metrics.step_index < 0:
        issues.append(f"no step found on scope {ch}: is it on {ref_desc}?")
    # 2nd / 98th percentiles rather than min / max, so single-sample switching spikes don't count
    lo, hi = np.percentile(ref, 2), np.percentile(ref, 98)
    swing = hi - lo
    if abs(swing - expected_swing) > SWING_TOLERANCE * expected_swing:
        issues.append(
            f"scope {ch} swings {swing:.3f} V but {ref_desc} should swing {expected_swing:.3f} V: "
            "probably on the wrong signal"
        )
    # pre-trigger VOUT, stopping 10 samples short of the trigger to stay clear of the step itself
    before = cap.channel(vch)[: max(10, cap.trigger_index - 10)].mean()
    issue = bench.vout_issue(float(before), source=f"scope {vch}")
    if issue:
        issues.append(issue)
    return issues


@dataclass
class XScopeSetup:
    """How the cross-check scope's channels are set for one test (the rest comes from [xscope]).

    The ref channel's scale depends on the stimulus (a 3.3 V DIO edge or a small VIN step), so each
    test passes it; the VOUT channel's range and the channel numbers come from the config.
    """

    ref_range: float  # V, full screen (8 divisions) on the 54621A
    ref_offset: float  # V at the screen centre


@dataclass
class XCheck:
    """One edge as seen by the cross-check scope (54621A), analysed like the AD2 capture.

    When the scope gave no data (not opened, busy, failed to arm or never completed), the arrays
    are empty, metrics is None and issues says why.
    """

    model: str  # instrument model, e.g. "54621A"
    averages: int  # acquisitions the scope averaged (0 = no data)
    t: np.ndarray  # s, 0 at the step it found (else at its trigger)
    vout: np.ndarray  # V, averaged VOUT from the scope
    vout_smoothed: np.ndarray  # V, VOUT after settling_analysis' glitch removal and moving average
    ref: np.ndarray  # V, the scope's ref (trigger) channel
    metrics: SettlingResult | None  # None when there was no data
    issues: list[str] = field(default_factory=list)  # cross-check problems only (never the AD2's)


@dataclass
class StepCapture:
    """What `averaged_step` returns for one edge."""

    cap: Capture  # the AD2's averaged capture
    issues: list[str]  # AD2 trigger problems (empty if every capture triggered)
    xtraces: dict | None = None  # cross-check scope traces by channel, if it delivered
    xissue: str | None = None  # why the cross-check scope has no traces, if it doesn't


def averaged_step(
    bench: Bench,
    tc: TransientCaptureConfig,
    *,
    setups: dict[int, ChannelSetup],
    trigger_channel: int,
    trigger_level: float,
    trigger_edge: Edge,
    start: Callable[[], None],
    step: Callable[[], None],
    label: str,
    xscope: XScopeSetup | None = None,
    hysteresis: float | None = None,
) -> StepCapture:
    """`tc.averages` captures of the step start() -> step(), aligned and averaged, plus any issues.

    start() puts the DUT at the starting level (then we wait tc.rest_time), step() is called by
    the scope driver once the trigger is armed. If the scope never triggers, one untriggered
    capture is returned instead (edge_found=False) so the plot shows what is on the channels.

    With a cross-check scope (bench.xscope and `xscope` given) its averaged acquisition is armed
    first, sees the same steps, and is collected afterwards. Its problems never affect the AD2's.

    setups: AD2 channel range / offset by channel. trigger_channel / trigger_level (V) /
    trigger_edge: the AD2 trigger, on the ref channel. label: name used in log messages.
    hysteresis (V): the AD2 trigger's, passed to Oscilloscope.capture (None = its default, 1 % of
    the trigger channel's range).

    If the AD2 times out on capture k, the run is not stopped: the issue is recorded, a forced
    capture is taken, and the function returns early with the average of the captures that did
    trigger (or the forced capture if none did). The cross-check scope is still collected.
    """
    xs, xs_issue, xs_traces = bench.xscope if xscope is not None else None, None, None
    # A 54621A left mid-:DIGitize by an earlier edge queues every command, so don't touch it.
    if xs is not None and xs.busy:
        xs, xs_issue = None, f"skipped: the {xs.model} is still busy from an earlier edge (press Run/Stop)"
    if xs is not None:
        xc = bench.cfg.xscope
        try:
            xs.arm(
                window=tc.n_samples / tc.sample_rate,
                pre_trigger=tc.pre_trigger,
                vout_channel=xc.vout_channel,
                vout_range=xc.vout_range,
                vout_offset=bench.cfg.dut.vout_nominal,
                ref_channel=xc.ref_channel,
                ref_range=xscope.ref_range,
                ref_offset=xscope.ref_offset,
                trigger_level=trigger_level,
                edge=trigger_edge,
                averages=tc.averages,
                points=xc.points,
            )
            bench.sleep(0.3)  # let :DIGitize start before the first step
        except InstrumentError as e:
            xs, xs_issue = None, f"could not arm: {e}"

    def again() -> None:
        """One more step for the cross-check scope, if it missed triggers during the AD2 captures."""
        start()
        bench.sleep(tc.rest_time)
        step()

    def collect_x() -> None:
        """Wait for the cross-check scope's average and read it; failures only set xs_issue."""
        nonlocal xs_traces, xs_issue
        if xs is None:
            return
        try:
            # up to 2 x averages extra steps if it missed some of the AD2's
            xs_traces = xs.collect(again, 2 * tc.averages, bench.cfg.xscope.timeout)
        except InstrumentError as e:
            xs_issue = str(e)

    caps: list[Capture] = []
    for k in range(tc.averages):
        start()
        bench.sleep(tc.rest_time)  # let VOUT settle at the starting level before the next step
        kw = {
            "sample_rate": tc.sample_rate,
            "n_samples": tc.n_samples,
            "setups": setups,
            "trigger_channel": trigger_channel,
            "trigger_level": trigger_level,
            "edge": trigger_edge,
            "pre_trigger": tc.pre_trigger,
            "stimulus": step,
            "timeout": tc.timeout,
            "hysteresis": hysteresis,
        }
        try:
            caps.append(bench.scope.capture(**kw))
        except InstrumentError as e:
            log.warning(
                "%s: capture %d got no trigger (%s); recording an untriggered capture", label, k + 1, e
            )
            issue = (
                f"the scope never triggered ({trigger_edge} through {trigger_level:.3f} V on scope "
                f"{trigger_channel}, capture {k + 1}): plotted an untriggered capture of the channels"
            )
            start()
            bench.sleep(tc.rest_time)
            # force=True: acquire without waiting for the trigger (the step is still applied)
            forced = bench.scope.capture(**kw, force=True)
            collect_x()
            return StepCapture(align_average(caps) if caps else forced, [issue], xs_traces, xs_issue)
    log.info("%s: averaged %d captures at %.3g MS/s", label, len(caps), caps[0].sample_rate / 1e6)
    collect_x()
    return StepCapture(align_average(caps), [], xs_traces, xs_issue)


def _pre_step_offset(e, x: XCheck) -> float:
    """Cross-check scope's pre-step VOUT minus the AD2's (V), from each one's settling analysis.

    NaN when either has no pre-step level (no step found, or no analysis), so the caller can
    overlay the traces on one axis instead.
    """
    xm = x.metrics
    if xm is None or xm.step_index < 0 or e.metrics.step_index < 0:
        return float("nan")
    return float(xm.nominal - e.metrics.nominal)


CLIP_MARGIN = 0.01  # a sample within 1 % of full scale of a screen edge counts as clipped


def _clip_issue(v: np.ndarray, centre: float, full_range: float, channel: str) -> str | None:
    """A cross-check problem if VOUT touches the top or bottom of the scope's screen.

    The 54621A screen spans centre +/- full_range / 2 (V; arm() sets ch offset and range). A clipped
    sample isn't a measurement of VOUT, so neither a flat trace on the edge (VOUT off screen, e.g.
    ~0 V: bench, 2026-11-10) nor a peak that runs off it may be analysed as if it were.
    """
    lo, hi = centre - full_range / 2, centre + full_range / 2
    margin = CLIP_MARGIN * full_range
    n_lo, n_hi = int(np.sum(v <= lo + margin)), int(np.sum(v >= hi - margin))
    if not (n_lo or n_hi):
        return None
    where = " and ".join(
        f"the {side} ({n} of {len(v)} points at {edge:.3f} V)"
        for side, n, edge in (("bottom", n_lo, lo), ("top", n_hi, hi))
        if n
    )
    return (
        f"its {channel} VOUT is clipped at {where} of its {lo:.3f} to {hi:.3f} V screen: is it on VOUT, "
        "with VOUT near nominal, or is the response bigger than xscope.vout_range?"
    )


def analyse_xcheck(
    bench: Bench,
    sc: StepCapture,
    an: AnalysisConfig,
    edge: Edge,
    *,
    expected_swing: float,
    ref_desc: str,
) -> XCheck | None:
    """Run the same settling analysis on the cross-check scope's traces, with their own checks.

    Returns None when no cross-check scope was wanted. When it was wanted but has no data, returns
    an XCheck with empty arrays and the reason in issues. Otherwise the checks are the same as
    capture_issues (step found, ref swing within SWING_TOLERANCE, VOUT near nominal before t = 0),
    plus VOUT not clipped at the screen's edges, and the time axis is shifted so 0 is the step the analysis found.
    """
    if bench.xscope is None:
        if bench.xscope_issue:  # wanted but couldn't be opened: say so on every edge
            empty = np.array([])
            return XCheck("54621A", 0, empty, empty, empty, empty, None, [bench.xscope_issue])
        return None
    model = getattr(bench.xscope, "model", "xscope")
    if sc.xtraces is None:
        empty = np.array([])
        return XCheck(model, 0, empty, empty, empty, empty, None, [sc.xissue or "no data"])
    xc = bench.cfg.xscope
    tv, tr = sc.xtraces[xc.vout_channel], sc.xtraces[xc.ref_channel]
    fs = 1.0 / float(tv.t[1] - tv.t[0])  # the scope returns a uniform time axis
    m, smoothed = settling_analysis(
        tv.v,
        tr.v,
        fs,
        edge,
        settle_fraction=an.settle_fraction,
        smooth_time=an.smooth_time,
        peak_window=an.peak_window,
        blank_time=an.blank_time,
        reference=an.settle_reference,
    )
    issues = []
    if m.step_index < 0:
        issues.append(f"no step found on its ch{xc.ref_channel}: is it on {ref_desc}?")
    swing = float(np.percentile(tr.v, 98) - np.percentile(tr.v, 2))
    if abs(swing - expected_swing) > SWING_TOLERANCE * expected_swing:
        issues.append(
            f"its ch{xc.ref_channel} swings {swing:.3f} V, {ref_desc} should swing {expected_swing:.3f} V"
        )
    clipped = _clip_issue(tv.v, bench.cfg.dut.vout_nominal, xc.vout_range, f"ch{xc.vout_channel}")
    if clipped:
        issues.append(clipped)
    pre = tv.v[tv.t < 0]
    vi = bench.vout_issue(
        float(pre.mean()) if len(pre) else float(tv.v.mean()), source=f"{model} ch{xc.vout_channel}"
    )
    if vi:
        issues.append(vi)
    t = tv.t - (tv.t[m.step_index] if m.step_index >= 0 else 0.0)
    return XCheck(model, tv.averages, t, tv.v, smoothed, tr.v, m, issues)


def xcheck_row(x: XCheck | None) -> dict:
    """Cross-check fields for data.csv / summary.json (empty if no cross-check scope).

    Metrics are NaN unless the cross-check is clean, so a bad cross-check never looks like a number.
    """
    if x is None:
        return {}
    m = x.metrics
    ok = not x.issues and m is not None
    return {
        "xcheck_model": x.model,
        "xcheck_valid": ok,
        "xcheck_issues": " | ".join(x.issues),
        "xcheck_averages": x.averages,
        "xcheck_deviation_v": m.deviation if ok else float("nan"),
        "xcheck_settling_time_s": m.settling_time if ok else float("nan"),
        "xcheck_noise_v": m.noise if ok else float("nan"),
    }


def xcheck_csv(x: XCheck) -> list[dict]:
    """The cross-check scope's waveform as CSV rows (t in s, voltages in V)."""
    return [
        {"t_s": float(t), "vout_v": float(v), "vout_smoothed_v": float(f), "ref_v": float(r)}
        for t, v, f, r in zip(x.t, x.vout, x.vout_smoothed, x.ref, strict=True)
    ]


def xcheck_report(e) -> list[str]:
    """Cross-check scope lines for one edge (never affects the AD2 result's validity).

    `e` is a test's EdgeResult. The difference from the AD2's peak is shown only when both are valid.
    """
    x = getattr(e, "xcheck", None)
    if x is None:
        return []
    if x.issues or x.metrics is None:
        return [f"    {x.model:9s} no cross-check:"] + [f"      - {i}" for i in x.issues]
    m = x.metrics
    ts = "did not settle" if np.isnan(m.settling_time) else f"{m.settling_time * 1e6:.2f} us"
    line = f"    {x.model:9s} peak {m.deviation * 1e3:+.2f} mV, settling {ts}  ({x.averages} averages)"
    ad2 = e.metrics
    if not getattr(e, "issues", []) and np.isfinite(ad2.deviation):
        d = (m.deviation - ad2.deviation) * 1e3
        line += f";  differs from the AD2 by {d:+.2f} mV"
    return [line]


def edge_report(e, extra: list[str] | None = None) -> list[str]:
    """Report lines for one edge. Invalid edges list their setup issues and no metrics.

    `e` is a test's EdgeResult (needs .title, .metrics, .issues, optionally .xcheck). `extra` lines
    (e.g. line-tran's mV per V) go between the peak and the settling time. Also warns when, with
    settle_reference "pre", VOUT ends outside the band around the pre-step level (a DC shift
    bigger than the band, e.g. load regulation on a large step), since it can then never settle.
    """
    lines = [f"  {e.title}"]
    issues = list(getattr(e, "issues", []))
    if issues:
        lines.append("    NOT A VALID MEASUREMENT (data saved and plotted as measured):")
        lines += [f"      - {i}" for i in issues]
        return lines + xcheck_report(e)
    m = e.metrics
    ts = "did not settle in the capture" if np.isnan(m.settling_time) else f"{m.settling_time * 1e6:.2f} us"
    lines += [
        f"    nominal   {m.nominal:.5f} V",
        f"    peak      {m.deviation * 1e3:+.2f} mV  (band ±{m.band * 1e3:.2f} mV)",
        *(extra or []),
        f"    settling  {ts}  (to the {'pre-step' if m.reference == 'pre' else 'final'} level)",
        f"    noise     {m.noise * 1e3:.3f} mV rms"
        + ("" if m.trustworthy else "  ! above band/3: settling time limited by noise"),
    ]
    shift = m.final - m.nominal
    if m.reference == "pre" and np.isfinite(shift) and abs(shift) > m.band:
        lines.append(
            f"    ! VOUT ends {shift * 1e3:+.2f} mV from the pre-step level, outside the "
            f"±{m.band * 1e3:.2f} mV band, so it can never settle back to it. "
            'Set analysis.settle_reference = "post" to measure settling to the final level.'
        )
    return lines + xcheck_report(e)


def plot_edges(
    edges,
    suptitle: str,
    ref_label: str = "Trigger channel (V)",
    vout_nominal: float | None = None,
    full: bool = False,
    window: float | None = None,
    per_edge: bool = False,
):
    """One column per edge: the trigger channel's trace on a small panel on top, sharing the time
    axis with VOUT below, both in absolute volts.

    Valid edges also get the analysis (smoothed trace, settling band, peak, settling time).
    Edges with setup issues show only the measured data, the expected VOUT as a dashed line and
    the issues in a red box, so a wrong setup is obvious and nothing looks like a result.
    Separate panels rather than two y-axes on one plot (V and mV scales are easy to misread). The one
    exception is the cross-check scope's VOUT: it gets a right-hand axis at the same V/div as the
    AD2's, offset by the two scopes' difference in pre-step level (their DC offsets differ by mV).
    Each edge needs .title, .metrics, .t, .vout, .vout_smoothed, .ref, .trigger_level, .issues.

    ref_label: y label of the top panel. vout_nominal: drawn as the expected VOUT on invalid edges.
    full: show the whole capture instead of zooming on the step (plot_full.png). window: time after
    the step to show (s); None picks it from the settling times. per_edge: size each valid edge's
    span to its own settling time instead of the slowest edge's (plot_zoom.png; ignores `window`). The cross-check scope's traces,
    if any, are overlaid in another colour. Returns the matplotlib Figure.
    """
    from matplotlib.ticker import FormatStrFormatter

    n = len(edges)
    # One time span for every edge, so the panels (and a scope at a fixed time/div) compare
    # directly. `window` (s after the step) sets it; else the largest automatic span:
    # 2.5 x the slowest valid edge's settling time + 10 us.
    if window is not None:
        t_end_us = window * 1e6
    else:
        spans = [
            e.metrics.settling_time * 1e6 * 2.5 + 10
            for e in edges
            if not getattr(e, "issues", []) and np.isfinite(e.metrics.settling_time)
        ]
        t_end_us = max(spans) if spans else None

    def edge_span_us(e) -> float | None:
        """plot_zoom.png: this edge's own span (2.5 x its settling time + 10 us), else the shared one."""
        if per_edge and not getattr(e, "issues", []) and np.isfinite(e.metrics.settling_time):
            return e.metrics.settling_time * 1e6 * 2.5 + 10
        return t_end_us

    fig, axes = plotting.figure(
        2, n, size=(5.4 * n, 5.8), squeeze=False, sharex="col", height_ratios=[1, 3.2]
    )
    any_invalid = False
    legend: dict = {}  # label -> handle, over every edge; drawn below the panels, not over the data
    for col, e in enumerate(edges):
        ax_ref, ax = axes[0][col], axes[1][col]
        m, issues = e.metrics, list(getattr(e, "issues", []))
        valid = not issues
        any_invalid |= not valid
        # long full-capture windows read better in ms; settling times stay labelled in µs
        scale, unit = (1e3, "ms") if full and (e.t[-1] - e.t[0]) > 2e-3 else (1e6, "µs")
        us = e.t * scale
        span_us = edge_span_us(e)
        if not full and span_us is not None and (valid or window is not None):  # zoom on the step
            view = (us >= -0.15 * span_us) & (us <= span_us)
        else:
            view = np.ones_like(us, dtype=bool)  # whole capture: nothing hidden

        # Top: scope 2, the trigger / stimulus channel (measured, absolute).
        ax_ref.axvline(0, color=plotting.AXIS, lw=0.8, zorder=1)
        ax_ref.plot(us[view], e.ref[view], color=plotting.SERIES[1], lw=1.4, zorder=3)
        if getattr(e, "trigger_level", None) is not None:
            ax_ref.axhline(e.trigger_level, color=plotting.INK_2, lw=0.9, ls=(0, (3, 2)), zorder=2)
            ax_ref.annotate(
                f"trigger {e.trigger_level:.2f} V",
                (1.0, e.trigger_level),
                xycoords=("axes fraction", "data"),
                xytext=(-4, 3),
                textcoords="offset points",
                ha="right",
                fontsize=7.5,
                color=plotting.INK_2,
            )
        x = getattr(e, "xcheck", None)
        xok = x is not None and len(x.t) > 0
        if xok:
            xs_scale = 1e3 if unit == "ms" else 1e6
            xv = (x.t * xs_scale >= us[view][0]) & (x.t * xs_scale <= us[view][-1])
            ax_ref.plot(x.t[xv] * xs_scale, x.ref[xv], color=plotting.SERIES[2], lw=1.0, alpha=0.9, zorder=2)
        ax_ref.set_ylabel(ref_label, fontsize=8)
        ax_ref.set_title(e.title, fontsize=10, pad=6)
        ax_ref.tick_params(labelbottom=False)

        # Bottom: VOUT, measured, absolute volts.
        ax.axvline(0, color=plotting.AXIS, lw=0.8, zorder=1)
        v, vs = e.vout[view], e.vout_smoothed[view]
        # The two scopes' DC levels differ by several mV (each is offset at 20 mV/div), so the
        # cross-check goes on its own right-hand axis at the same V/div, shifted by the difference
        # in pre-step levels. Both traces keep their measured values; only the axes differ.
        x_offset = _pre_step_offset(e, x) if xok else float("nan")
        if valid:
            ax.axhspan(m.lower, m.upper, color=plotting.GRID, alpha=0.8, lw=0, zorder=0)
            ax.plot(us[view], v, color=plotting.SERIES[0], lw=0.8, alpha=0.35, zorder=2, label="Averaged")
            ax.plot(us[view], vs, color=plotting.SERIES[0], lw=1.8, zorder=3, label="Smoothed")
            if np.isfinite(m.settling_time) and m.settling_time * scale <= us[view][-1]:
                ax.axvline(m.settling_time * scale, color=plotting.INK_2, lw=1, ls=(0, (3, 2)), zorder=2)
                ax.annotate(
                    f"settled {m.settling_time * 1e6:.1f} µs",
                    (m.settling_time * scale, 1.0),
                    xycoords=("data", "axes fraction"),
                    xytext=(4, -12),
                    textcoords="offset points",
                    fontsize=8,
                    color=plotting.INK_2,
                )
            elif np.isfinite(m.settling_time):  # beyond the chosen span: say so instead of widening it
                ax.annotate(
                    f"settles at {m.settling_time * 1e6:.1f} µs →",
                    (1.0, 1.0),
                    xycoords="axes fraction",
                    xytext=(-4, -12),
                    textcoords="offset points",
                    ha="right",
                    fontsize=8,
                    color=plotting.INK_2,
                )
            # the sample closest to the peak value (the analysis returns the value, not its index)
            i0 = max(m.step_index, 0)  # the peak is after the step: don't match an equal value before it
            i_pk = i0 + int(np.argmin(np.abs(e.vout_smoothed[i0:] - m.peak)))
            ax.plot(
                us[i_pk],
                e.vout_smoothed[i_pk],
                "o",
                ms=6,
                color=plotting.SERIES[0],
                mec=plotting.SURFACE,
                mew=1.5,
                zorder=4,
            )
            ax.annotate(
                f"{m.deviation * 1e3:+.1f} mV",
                (us[i_pk], e.vout_smoothed[i_pk]),
                xytext=(8, 0),
                textcoords="offset points",
                fontsize=8,
                color=plotting.INK,
                va="center",
            )
            # Scale to the response and band; the 1-sample switching glitch would squash it.
            lo, hi = min(vs.min(), m.lower), max(vs.max(), m.upper)
        else:
            ax.plot(us[view], v, color=plotting.SERIES[0], lw=1.0, zorder=3, label="Measured (averaged)")
            lo, hi = float(np.percentile(v, 0.5)), float(np.percentile(v, 99.5))
            if vout_nominal is not None:
                ax.axhline(
                    vout_nominal,
                    color=plotting.CRITICAL,
                    lw=1.2,
                    ls=(0, (5, 3)),
                    zorder=2,
                    label=f"Expected VOUT {vout_nominal:.3f} V",
                )
                lo, hi = min(lo, vout_nominal), max(hi, vout_nominal)
            plotting.issue_box(ax, issues)
        pad = 0.15 * (hi - lo or 1e-3)
        ax.set_ylim(lo - pad, hi + pad)
        ax.yaxis.set_major_formatter(FormatStrFormatter("%.4f"))
        handles, labels = ax.get_legend_handles_labels()
        if xok:
            xlabel = f"{x.model} ({x.averages} avg)" + (": see report" if x.issues else "")
            if np.isfinite(x_offset):
                ax_x = ax.twinx()
                ax_x.set_ylim(lo - pad + x_offset, hi + pad + x_offset)  # same span as the AD2's
                ax_x.yaxis.set_major_formatter(FormatStrFormatter("%.4f"))
                ax_x.set_ylabel(f"VOUT, {x.model} (V)", color=plotting.SERIES[2])
                ax_x.tick_params(axis="y", colors=plotting.SERIES[2])
                ax.set_zorder(ax_x.get_zorder() + 1)  # AD2 traces and markers drawn on top
                ax.patch.set_visible(False)
                xlabel += ", right axis"
            else:  # no pre-step level to align on: overlay on the AD2's axis
                ax_x = ax
            (xl,) = ax_x.plot(
                x.t[xv] * xs_scale, x.vout[xv], color=plotting.SERIES[2], lw=1.2, alpha=0.9, zorder=2
            )
            handles, labels = [xl, *handles], [xlabel, *labels]
        for h, lbl in zip(handles, labels, strict=True):  # one figure legend: each label once
            legend.setdefault(lbl, h)
        ax.set_xlim(us[view][0], us[view][-1])  # exactly the chosen span, the same for every edge
        ax.set_xlabel(
            f"Time from step ({unit})" if e.metrics.step_index >= 0 else f"Time from scope trigger ({unit})"
        )
        ax.set_ylabel(f"VOUT, scope {getattr(e, 'vout_channel', 1)} (V)")
    if legend:
        fig.legend(
            list(legend.values()), list(legend), loc="outside lower center", ncols=len(legend), fontsize=8
        )
    if any_invalid:  # invalid edges draw no band, so drop its legend note
        suptitle = suptitle.replace("  ·  shaded = settling band", "")
    plotting.suptitle(fig, suptitle, invalid=any_invalid)
    return fig
