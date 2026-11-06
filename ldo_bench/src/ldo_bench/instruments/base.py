"""Instrument interfaces.

Test procedures only use these Protocols, never a concrete driver. Supporting a
different supply, scope or a bench DMM means writing one class that satisfies
the relevant Protocol; the procedures do not change.

Channel numbers are 1-based everywhere (matching the front panel / WaveForms),
drivers convert to whatever their SDK uses.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

import numpy as np

Edge = Literal["rising", "falling"]


class InstrumentError(RuntimeError):
    """Raised for any instrument communication or configuration problem."""


@dataclass(frozen=True)
class DcReading:
    mean: float
    std: float
    n: int


@dataclass(frozen=True)
class ChannelSetup:
    range: float = 5.0
    offset: float = 0.0


@dataclass
class Capture:
    """One (possibly averaged) two-channel scope capture."""

    sample_rate: float
    ch1: np.ndarray
    ch2: np.ndarray
    trigger_index: int  # sample index where the hardware trigger was placed

    def channel(self, ch: int) -> np.ndarray:
        if ch == 1:
            return self.ch1
        if ch == 2:
            return self.ch2
        raise ValueError(f"no channel {ch}")

    @property
    def time(self) -> np.ndarray:
        """Time axis (s) with 0 at the hardware trigger."""
        return (np.arange(len(self.ch1)) - self.trigger_index) / self.sample_rate


@runtime_checkable
class PowerSupply(Protocol):
    idn: str

    def setup(self, *, current_limit: float, ovp: float | None, voltage_range: str | None) -> None: ...
    def set_voltage(self, volts: float) -> None: ...
    def get_voltage_setpoint(self) -> float: ...
    def output(self, on: bool) -> None: ...
    def measure_voltage(self) -> float: ...
    def measure_current(self) -> float: ...
    def close(self) -> None: ...


@runtime_checkable
class Oscilloscope(Protocol):
    idn: str

    def measure_dc(self, duration: float, setups: dict[int, ChannelSetup]) -> dict[int, DcReading]:
        """Average each channel over `duration` seconds (free-running, no trigger)."""
        ...

    def capture(
        self,
        *,
        sample_rate: float,
        n_samples: int,
        setups: dict[int, ChannelSetup],
        trigger_channel: int,
        trigger_level: float,
        edge: Edge,
        pre_trigger: float,
        stimulus: Callable[[], None],
        timeout: float,
    ) -> Capture:
        """Arm a single triggered capture, call `stimulus()` once armed, return the data."""
        ...

    def close(self) -> None: ...


@runtime_checkable
class DigitalOut(Protocol):
    def write_pins(self, levels: dict[int, bool]) -> None:
        """Drive the given DIO pins (others keep their state)."""
        ...

    def release_all(self) -> None:
        """Stop driving (tri-state) every pin this object has touched.

        Both load banks have 10k gate pull-downs, so released pins mean load off.
        """
        ...


@runtime_checkable
class Wavegen(Protocol):
    def set_dc(self, channel: int, volts: float) -> None: ...
    def off(self, channel: int) -> None: ...
