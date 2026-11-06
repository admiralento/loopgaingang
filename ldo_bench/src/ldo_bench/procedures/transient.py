"""Shared machinery for the transient tests: repeated triggered captures, aligned and averaged."""

from __future__ import annotations

import logging
from collections.abc import Callable

import numpy as np

from ldo_bench import plotting
from ldo_bench.bench import Bench
from ldo_bench.config import TransientCaptureConfig
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
    return Capture(captures[0].sample_rate, ch1, ch2, pre)


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
) -> Capture:
    """`tc.averages` captures of the step start() -> step(), aligned and averaged.

    start() puts the DUT at the starting level (then we wait tc.rest_time),
    step() is called by the scope driver once the trigger is armed.
    """
    caps = []
    for k in range(tc.averages):
        start()
        bench.sleep(tc.rest_time)
        try:
            cap = bench.scope.capture(
                sample_rate=tc.sample_rate,
                n_samples=tc.n_samples,
                setups=setups,
                trigger_channel=trigger_channel,
                trigger_level=trigger_level,
                edge=trigger_edge,
                pre_trigger=tc.pre_trigger,
                stimulus=step,
                timeout=tc.timeout,
            )
        except InstrumentError as e:
            raise InstrumentError(
                f"{label}: capture {k + 1} got no trigger ({e}). Check that scope channel "
                f"{trigger_channel} is wired to the stimulus."
            ) from e
        caps.append(cap)
    log.info("%s: averaged %d captures at %.3g MS/s", label, len(caps), caps[0].sample_rate / 1e6)
    return align_average(caps)


def plot_edges(edges, suptitle: str):
    """One panel per edge: averaged + smoothed VOUT, settling band, peak and settling time.

    Each edge needs .title, .metrics (SettlingResult), .t, .vout and .vout_smoothed.
    """
    n = len(edges)
    fig, axes = plotting.figure(1, n, size=(5.2 * n, 4.4), squeeze=False)
    for ax, e in zip(axes[0], edges, strict=True):
        m = e.metrics
        us = e.t * 1e6
        dev = (e.vout - m.nominal) * 1e3
        dev_s = (e.vout_smoothed - m.nominal) * 1e3
        # Show from a little before the step to well past settling.
        t_end = (m.settling_time * 1e6 * 2.5 + 10) if np.isfinite(m.settling_time) else us[-1]
        view = (us >= -0.15 * t_end) & (us <= t_end)
        if np.isfinite(m.band):
            lvl = (m.settle_level - m.nominal) * 1e3
            ax.axhspan(lvl - m.band * 1e3, lvl + m.band * 1e3, color=plotting.GRID, alpha=0.8, lw=0, zorder=0)
        ax.axvline(0, color=plotting.AXIS, lw=0.8, zorder=1)
        ax.plot(us[view], dev[view], color=plotting.SERIES[0], lw=0.8, alpha=0.35, zorder=2, label="Averaged")
        ax.plot(us[view], dev_s[view], color=plotting.SERIES[0], lw=1.8, zorder=3, label="Smoothed (1 µs)")
        if np.isfinite(m.settling_time):
            ax.axvline(m.settling_time * 1e6, color=plotting.INK_2, lw=1, ls=(0, (3, 2)), zorder=2)
            ax.annotate(
                f"settled {m.settling_time * 1e6:.1f} µs",
                (m.settling_time * 1e6, 1.0),
                xycoords=("data", "axes fraction"),
                xytext=(4, -12),
                textcoords="offset points",
                fontsize=8,
                color=plotting.INK_2,
            )
        i_pk = int(np.argmin(np.abs(e.vout_smoothed - m.peak))) if np.isfinite(m.peak) else None
        if i_pk is not None:
            ax.plot(
                us[i_pk],
                dev_s[i_pk],
                "o",
                ms=6,
                color=plotting.SERIES[0],
                mec=plotting.SURFACE,
                mew=1.5,
                zorder=4,
            )
            ax.annotate(
                f"{m.deviation * 1e3:+.1f} mV",
                (us[i_pk], dev_s[i_pk]),
                xytext=(8, 0),
                textcoords="offset points",
                fontsize=8,
                color=plotting.INK,
                va="center",
            )
        # Scale to the smoothed response and band; the 1-sample switching glitch in the raw
        # trace would otherwise set the axis and squash the response.
        span = [dev_s[view].min(), dev_s[view].max()]
        if np.isfinite(m.band):
            span += [lvl - m.band * 1e3, lvl + m.band * 1e3]
        lo, hi = min(span), max(span)
        pad = 0.15 * (hi - lo or 1.0)
        ax.set_ylim(lo - pad, hi + pad)
        ax.set_xlabel("Time from step (µs)")
        ax.set_ylabel(f"VOUT minus {m.nominal:.4f} V (mV)" if np.isfinite(m.nominal) else "VOUT (mV)")
        ax.set_title(e.title, fontsize=10, pad=6)
        ax.legend(loc="lower right" if m.deviation > 0 else "upper right")
    fig.suptitle(suptitle, x=0.01, ha="left", fontsize=11, fontweight="bold", color=plotting.INK)
    return fig
