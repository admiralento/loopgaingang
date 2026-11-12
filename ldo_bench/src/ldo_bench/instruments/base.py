"""Instrument interfaces: the contract between test procedures and drivers.

Test procedures only use these Protocols, never a concrete driver. Supporting a
different supply, scope or a bench DMM means writing one class that satisfies
the relevant Protocol; the procedures do not change.

Channel numbers are 1-based everywhere (matching the front panel / WaveForms),
drivers convert to whatever their SDK uses. Units are SI throughout: volts (V),
amps (A), seconds (s), hertz (Hz).

The Protocols are `runtime_checkable`, so `isinstance(x, PowerSupply)` checks
that the methods exist (not their signatures). The real drivers (e36xx, ad2,
hp34401a) and the simulator (sim) all implement them.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

import numpy as np

Edge = Literal["rising", "falling"]
WaveShape = Literal["sine", "triangle"]  # periodic wavegen shapes (Wavegen.set_wave)


class InstrumentError(RuntimeError):
    """Raised for any instrument communication or configuration problem."""


@dataclass(frozen=True)
class DcReading:
    """Average of one scope channel over a free-running acquisition."""

    mean: float  # (V)
    std: float  # (V), sample standard deviation: noise plus any ripple
    n: int  # samples averaged


@dataclass(frozen=True)
class ChannelSetup:
    """Scope channel input settings.

    range is the peak-to-peak input span (V); the AD2 has two hardware ranges (about 5 V and
    50 V) and rounds to the nearest. offset (V) is passed straight to the driver's channel
    offset setting (the AD2 SDK's channelOffsetSet).
    """

    range: float = 5.0
    offset: float = 0.0


@dataclass
class Capture:
    """One (possibly averaged) two-channel scope capture."""

    sample_rate: float
    ch1: np.ndarray
    ch2: np.ndarray
    trigger_index: int  # sample index of the trigger edge (found in the data, or the configured position)
    edge_found: bool = (
        True  # False: no edge in the data, so trigger_index is where the scope was set to trigger
    )

    def channel(self, ch: int) -> np.ndarray:
        """Samples (V) of channel `ch` (1 or 2)."""
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
    """Bench DC supply feeding VIN (or the BUF634 rail for line transient / PSRR)."""

    idn: str  # *IDN? reply, recorded in results

    def setup(self, *, current_limit: float, ovp: float | None, voltage_range: str | None) -> None:
        """Prepare the output with it switched off.

        current_limit (A) is the constant-current limit. ovp (V) is the hardware over-voltage
        trip, None for none; a supply that can't set it may warn instead of failing (software
        limits still apply). voltage_range is the SCPI range name sent as VOLT:RANG (config allows
        "LOW" / "HIGH"), None to leave the supply's current range.
        """
        ...

    def set_voltage(self, volts: float) -> None:
        """Program the output voltage setpoint (V). Does not switch the output on."""
        ...

    def get_voltage_setpoint(self) -> float:
        """The programmed setpoint (V), read back from the supply."""
        ...

    def output(self, on: bool) -> None:
        """Switch the output on or off."""
        ...

    def measure_voltage(self) -> float:
        """The supply's own readback of its output terminals (V)."""
        ...

    def measure_current(self) -> float:
        """The supply's own readback of its output current (A)."""
        ...

    def close(self) -> None:
        """Release the instrument (return it to front-panel control). Leaves the output as is."""
        ...


@runtime_checkable
class SupplyOutput(Protocol):
    """A second output of the same supply (E3648A OUT2), e.g. the external reference on VREF_IN.

    It switches on and off with the main output (OUTP is global on the E3648A), so it has no
    output() of its own: it is held at 0 V whenever it shouldn't be driving.
    """

    channel: int  # 1-based output number

    def setup(self, *, current_limit: float, ovp: float | None) -> None:
        """Current limit (A) and OVP (V, None for none) on this output, then 0 V. Raises if refused."""
        ...

    def set_voltage(self, volts: float) -> None:
        """Program this output's setpoint (V)."""
        ...

    def get_voltage_setpoint(self) -> float:
        """This output's programmed setpoint (V), read back."""
        ...

    def measure_voltage(self) -> float:
        """The supply's readback of this output (V)."""
        ...

    def measure_current(self) -> float:
        """The supply's readback of this output's current (A)."""
        ...


@runtime_checkable
class Oscilloscope(Protocol):
    """Two-channel scope (the AD2's analog inputs). Channels are 1 and 2."""

    idn: str

    def measure_dc(self, duration: float, setups: dict[int, ChannelSetup]) -> dict[int, DcReading]:
        """Average each channel in `setups` over `duration` (s), free-running with no trigger.

        Only the channels in `setups` are enabled and returned.
        """
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
        force: bool = False,
        dio_trigger: tuple[int, Edge] | None = None,
    ) -> Capture:
        """Arm a single triggered capture, call `stimulus()` once armed, return the data.

        sample_rate (Hz) is a request; the driver may round it, and Capture.sample_rate is the
        rate actually used. n_samples may likewise be cut to the buffer size. The trigger is an
        `edge` crossing of trigger_level (V) on trigger_channel (which must be in `setups`).
        pre_trigger is the fraction (0..1) of the record before the trigger. stimulus() makes
        the event happen (switch a load, step the wavegen, toggle EN) and is called exactly once,
        after the scope is armed so the edge can't be missed. Raises InstrumentError if no
        trigger arrives within `timeout` (s).
        force=True: don't wait for a trigger (start acquiring at once), for showing what is on the
        channels when the trigger never comes. Such captures have edge_found=False.
        dio_trigger=(pin, edge): trigger on the scope's own digital pin instead of an analog channel
        (frees both channels for signals); trigger_channel / trigger_level are then ignored and
        t = 0 is the hardware trigger.
        """
        ...

    def close(self) -> None: ...


@runtime_checkable
class DigitalOut(Protocol):
    """Static digital outputs (the AD2's DIO 0..15, 3.3 V logic) switching loads and EN."""

    def write_pins(self, levels: dict[int, bool]) -> None:
        """Drive the given DIO pins (pin number -> True for high); others keep their state."""
        ...

    def release_all(self) -> None:
        """Stop driving (tri-state) every pin this object has touched.

        Both load banks have 10k gate pull-downs, so released pins mean load off.
        """
        ...


@runtime_checkable
class Wavegen(Protocol):
    """Arbitrary waveform generator outputs (AD2 W1 / W2, channels 1 and 2, +/-5 V)."""

    def set_dc(self, channel: int, volts: float) -> None:
        """Output a DC level (V). Repeated calls on a running channel update it without a restart glitch."""
        ...

    def set_sine(self, channel: int, *, frequency: float, low: float, high: float) -> None:
        """Output a continuous sine (Hz) swinging between `low` and `high` (V)."""
        ...

    def set_wave(self, channel: int, *, shape: WaveShape, frequency: float, low: float, high: float) -> None:
        """Output a continuous periodic wave of `shape` (Hz) swinging between `low` and `high` (V).

        "triangle" rises and falls linearly, at 2 (high - low) frequency V/s.
        """
        ...

    def off(self, channel: int) -> None:
        """Stop the channel (output returns to 0 V)."""
        ...


@runtime_checkable
class Voltmeter(Protocol):
    """Bench DMM (34401A) for accurate DC voltages."""

    idn: str

    def configure_dcv(self, *, range: float | None, nplc: float) -> None:
        """Set up DC volts. range (V) is the full-scale range, None for autorange. nplc is the
        integration time in power-line cycles (10 = 1/6 s at 60 Hz; longer is quieter and
        rejects mains hum)."""
        ...

    def read_dcv(self) -> float:
        """Trigger and return one DC voltage reading (V)."""
        ...

    def close(self) -> None:
        """Release the meter (return it to front-panel control)."""
        ...
