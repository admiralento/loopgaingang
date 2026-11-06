"""Shared machinery for the transient tests: repeated triggered captures, aligned and averaged."""

from __future__ import annotations

import logging
from collections.abc import Callable

import numpy as np

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
