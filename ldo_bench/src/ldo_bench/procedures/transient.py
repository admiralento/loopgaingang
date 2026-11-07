"""Shared machinery for the transient tests: repeated triggered captures, aligned and averaged."""

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
    """Average captures sample-by-sample with their trigger points lined up."""
    if not captures:
        raise ValueError("no captures")
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
    """Setup problems visible in a transient capture. Empty list = it shows the real stimulus and VOUT."""
    ch, vch = bench.cfg.ad2.ref_channel, bench.cfg.ad2.vout_channel
    issues = []
    if not cap.edge_found:
        issues.append(f"the scope triggered but scope {ch}'s data shows no clean edge: is it on {ref_desc}?")
    if metrics.step_index < 0:
        issues.append(f"no step found on scope {ch}: is it on {ref_desc}?")
    lo, hi = np.percentile(ref, 2), np.percentile(ref, 98)
    swing = hi - lo
    if abs(swing - expected_swing) > SWING_TOLERANCE * expected_swing:
        issues.append(
            f"scope {ch} swings {swing:.3f} V but {ref_desc} should swing {expected_swing:.3f} V: "
            "probably on the wrong signal"
        )
    before = cap.channel(vch)[: max(10, cap.trigger_index - 10)].mean()
    issue = bench.vout_issue(float(before), source=f"scope {vch}")
    if issue:
        issues.append(issue)
    return issues


@dataclass
class XScopeSetup:
    """How the cross-check scope's channels are set for one test (the rest comes from [xscope])."""

    ref_range: float
    ref_offset: float


@dataclass
class XCheck:
    """One edge as seen by the cross-check scope (54621A), analysed like the AD2 capture."""

    model: str
    averages: int
    t: np.ndarray  # s, 0 at the step it found (else at its trigger)
    vout: np.ndarray
    vout_smoothed: np.ndarray
    ref: np.ndarray
    metrics: SettlingResult | None
    issues: list[str] = field(default_factory=list)


@dataclass
class StepCapture:
    cap: Capture  # the AD2's averaged capture
    issues: list[str]
    xtraces: dict | None = None  # cross-check scope traces by channel, if it delivered
    xissue: str | None = None


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
) -> StepCapture:
    """`tc.averages` captures of the step start() -> step(), aligned and averaged, plus any issues.

    start() puts the DUT at the starting level (then we wait tc.rest_time), step() is called by
    the scope driver once the trigger is armed. If the scope never triggers, one untriggered
    capture is returned instead (edge_found=False) so the plot shows what is on the channels.

    With a cross-check scope (bench.xscope and `xscope` given) its averaged acquisition is armed
    first, sees the same steps, and is collected afterwards. Its problems never affect the AD2's.
    """
    xs, xs_issue, xs_traces = bench.xscope if xscope is not None else None, None, None
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
        start()
        bench.sleep(tc.rest_time)
        step()

    def collect_x() -> None:
        nonlocal xs_traces, xs_issue
        if xs is None:
            return
        try:
            xs_traces = xs.collect(again, 2 * tc.averages, bench.cfg.xscope.timeout)
        except InstrumentError as e:
            xs_issue = str(e)

    caps: list[Capture] = []
    for k in range(tc.averages):
        start()
        bench.sleep(tc.rest_time)
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
            forced = bench.scope.capture(**kw, force=True)
            collect_x()
            return StepCapture(align_average(caps) if caps else forced, [issue], xs_traces, xs_issue)
    log.info("%s: averaged %d captures at %.3g MS/s", label, len(caps), caps[0].sample_rate / 1e6)
    collect_x()
    return StepCapture(align_average(caps), [], xs_traces, xs_issue)


def analyse_xcheck(
    bench: Bench,
    sc: StepCapture,
    an: AnalysisConfig,
    edge: Edge,
    *,
    expected_swing: float,
    ref_desc: str,
) -> XCheck | None:
    """Run the same settling analysis on the cross-check scope's traces, with their own checks."""
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
    fs = 1.0 / float(tv.t[1] - tv.t[0])
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
    pre = tv.v[tv.t < 0]
    vi = bench.vout_issue(
        float(pre.mean()) if len(pre) else float(tv.v.mean()), source=f"{model} ch{xc.vout_channel}"
    )
    if vi:
        issues.append(vi)
    t = tv.t - (tv.t[m.step_index] if m.step_index >= 0 else 0.0)
    return XCheck(model, tv.averages, t, tv.v, smoothed, tr.v, m, issues)


def xcheck_row(x: XCheck | None) -> dict:
    """Cross-check fields for data.csv / summary.json (empty if no cross-check scope)."""
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
    return [
        {"t_s": float(t), "vout_v": float(v), "vout_smoothed_v": float(f), "ref_v": float(r)}
        for t, v, f, r in zip(x.t, x.vout, x.vout_smoothed, x.ref, strict=True)
    ]


def xcheck_report(e) -> list[str]:
    """Cross-check scope lines for one edge (never affects the AD2 result's validity)."""
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
    """Report lines for one edge. Invalid edges list their setup issues and no metrics."""
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
    ref_label: str = "Scope 2 (V)",
    vout_nominal: float | None = None,
    full: bool = False,
    window: float | None = None,
):
    """One column per edge: the scope 2 (trigger) trace on a small panel on top, sharing the time
    axis with VOUT below, both in absolute volts.

    Valid edges also get the analysis (smoothed trace, settling band, peak, settling time).
    Edges with setup issues show only the measured data, the expected VOUT as a dashed line and
    the issues in a red box, so a wrong setup is obvious and nothing looks like a result.
    Separate panels rather than two y-axes on one plot (V and mV scales are easy to misread).
    Each edge needs .title, .metrics, .t, .vout, .vout_smoothed, .ref, .trigger_level, .issues.
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
    fig, axes = plotting.figure(
        2, n, size=(5.4 * n, 5.8), squeeze=False, sharex="col", height_ratios=[1, 3.2]
    )
    any_invalid = False
    for col, e in enumerate(edges):
        ax_ref, ax = axes[0][col], axes[1][col]
        m, issues = e.metrics, list(getattr(e, "issues", []))
        valid = not issues
        any_invalid |= not valid
        # long full-capture windows read better in ms; settling times stay labelled in µs
        scale, unit = (1e3, "ms") if full and (e.t[-1] - e.t[0]) > 2e-3 else (1e6, "µs")
        us = e.t * scale
        if not full and t_end_us is not None and (valid or window is not None):  # zoom on the step
            view = (us >= -0.15 * t_end_us) & (us <= t_end_us)
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
        if xok:
            xlabel = f"{x.model} ({x.averages} avg)" + (": see report" if x.issues else "")
            ax.plot(
                x.t[xv] * xs_scale,
                x.vout[xv],
                color=plotting.SERIES[2],
                lw=1.2,
                alpha=0.9,
                zorder=2,
                label=xlabel,
            )
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
            i_pk = int(np.argmin(np.abs(e.vout_smoothed - m.peak)))
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
            ax.legend(loc="lower right" if m.deviation > 0 else "upper right")
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
            ax.legend(loc="center right")
            plotting.issue_box(ax, issues)
        pad = 0.15 * (hi - lo or 1e-3)
        ax.set_ylim(lo - pad, hi + pad)
        ax.yaxis.set_major_formatter(FormatStrFormatter("%.4f"))
        ax.set_xlim(us[view][0], us[view][-1])  # exactly the chosen span, the same for every edge
        ax.set_xlabel(
            f"Time from step ({unit})" if e.metrics.step_index >= 0 else f"Time from scope trigger ({unit})"
        )
        ax.set_ylabel(f"VOUT, scope {getattr(e, 'vout_channel', 1)} (V)")
    if any_invalid:  # two lines so it fits; invalid edges draw no band, so drop its legend note
        suptitle = "SETUP PROBLEM, not a valid measurement\n" + suptitle.replace(
            "  ·  shaded = settling band", ""
        )
    fig.suptitle(
        suptitle,
        x=0.01,
        ha="left",
        fontsize=11,
        fontweight="bold",
        color=plotting.CRITICAL if any_invalid else plotting.INK,
    )
    return fig
