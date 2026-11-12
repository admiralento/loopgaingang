"""Digilent Analog Discovery 2 via pydwf (WaveForms SDK).

One object implements three Protocols: Oscilloscope (analog in), DigitalOut
(static DIO) and Wavegen (W1/W2 as DC sources). Channel numbers are 1-based
here and converted to the SDK's 0-based indices internally.

Requires the WaveForms runtime (dwf.dll / dwf.framework). Only one program can
open the AD2, so close the WaveForms GUI first.

SDK points worth knowing:
* Sample rates derive from the 100 MHz clock by an integer divider, so the
  requested rate is rounded; we read back frequencyGet() and use the real one.
* The buffer is 8192 samples per channel in the default device configuration.
* In Single acquisition mode triggerPositionSet() is the time of the buffer
  centre relative to the trigger (see capture()).
* Each channel uses the Average filter, which averages the 100 MS/s ADC samples
  falling within each output sample (less noise when decimating).
* dio_trigger uses the Digital In instrument's trigger detector as the analog-in
  trigger source, so a DIO edge the AD2 itself drives can start the capture.
* V+ (analogIO channel 0) is the AD2's positive user supply, used as a logic rail.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

import numpy as np

from ldo_bench.instruments.base import Capture, ChannelSetup, DcReading, Edge, InstrumentError, WaveShape

log = logging.getLogger(__name__)

AD2_DEVICE_ID = 3  # DwfDeviceID.Discovery2
MAX_BUFFER = 8192  # AD2 default configuration
POLL = 0.002  # s


def _pydwf():
    try:
        import pydwf
    except ImportError as e:  # pragma: no cover - declared dependency
        raise InstrumentError("pydwf is not installed") from e
    return pydwf


def load_library():
    """The WaveForms SDK library handle (pydwf.DwfLibrary)."""
    pydwf = _pydwf()
    try:
        return pydwf.DwfLibrary()
    except pydwf.PyDwfError as e:
        raise InstrumentError(
            "could not load the Digilent WaveForms runtime. Install WaveForms from digilent.com "
            "(it provides dwf.dll / dwf.framework)."
        ) from e


def list_ad2(dwf) -> list[tuple[int, str, bool]]:
    """[(device index, serial number, already open)] for every AD2 attached."""
    out = []
    n = dwf.deviceEnum.enumerateDevices()
    for i in range(n):
        devid, _ = dwf.deviceEnum.deviceType(i)
        if int(devid) == AD2_DEVICE_ID:
            out.append((i, dwf.deviceEnum.serialNumber(i), dwf.deviceEnum.deviceIsOpened(i)))
    return out


def open_ad2_device(serial: str = "") -> AnalogDiscovery2:
    """Open the AD2 whose serial number contains `serial` ('SN:' prefix optional), or the only free one."""
    dwf = load_library()
    devices = list_ad2(dwf)
    if serial:
        devices = [d for d in devices if serial.upper().removeprefix("SN:") in d[1].upper()]
    if not devices:
        raise InstrumentError(
            f"no Analog Discovery 2{' with serial ' + serial if serial else ''} found. "
            "Check the USB cable and run `ldo-bench discover`."
        )
    free = [d for d in devices if not d[2]]
    if not free:
        raise InstrumentError("the Analog Discovery 2 is in use by another program. Close WaveForms.")
    if len(free) > 1 and not serial:
        raise InstrumentError(
            "more than one Analog Discovery 2 found; pick one with --ad-serial: "
            + ", ".join(d[1] for d in free)
        )
    index, sn, _ = free[0]
    pydwf = _pydwf()
    try:
        device = dwf.deviceControl.open(index)
    except pydwf.DwfLibraryError as e:
        raise InstrumentError(f"could not open Analog Discovery 2 {sn}: {e}") from e
    log.info("Using Analog Discovery 2 %s", sn)
    return AnalogDiscovery2(device, sn)


class AnalogDiscovery2:
    """Oscilloscope + DigitalOut + Wavegen on one AD2, plus set_vplus() for its V+ supply."""

    def __init__(self, device, serial: str) -> None:
        self._p = _pydwf()
        self.dev = device
        self.idn = f"Digilent Analog Discovery 2 (SN:{serial})"
        self._dio_enable = 0  # bit mask of DIO pins we drive (output enable)
        self._dio_value = 0  # bit mask of their levels
        self._awg_running: set[int] = set()  # wavegen channels (1-based) already running DC
        self.dev.analogIn.reset()
        self.dev.digitalIO.reset()

    # --- helpers --------------------------------------------------------------
    def _wrap(self, what: str, fn: Callable, *args):
        """Call an SDK function, turning its errors into InstrumentError."""
        try:
            return fn(*args)
        except self._p.DwfLibraryError as e:
            raise InstrumentError(f"AD2 {what}: {e}") from e

    def _setup_channels(self, setups: dict[int, ChannelSetup]) -> None:
        """Enable exactly the channels in `setups` and set their range / offset / filter."""
        ai = self.dev.analogIn
        filt = self._p.DwfAnalogInFilter.Average  # averages between samples: lower noise below 100 MS/s
        for ch in (1, 2):
            ai.channelEnableSet(ch - 1, ch in setups)
        for ch, s in setups.items():
            ai.channelRangeSet(ch - 1, s.range)
            ai.channelOffsetSet(ch - 1, s.offset)
            ai.channelFilterSet(ch - 1, filt)

    def _wait(self, states: tuple, timeout: float, what: str) -> None:
        """Poll the analog-in state machine until it reaches one of `states`."""
        ai = self.dev.analogIn
        t_end = time.monotonic() + timeout
        while True:
            st = ai.status(False)  # False: state only, don't transfer data
            if st in states:
                return
            if time.monotonic() > t_end:
                raise InstrumentError(f"AD2 timed out waiting for {what} (state {st.name})")
            time.sleep(POLL)

    def _read(self, setups: dict[int, ChannelSetup], n: int) -> dict[int, np.ndarray]:
        """Transfer the finished acquisition: n samples (V) per enabled channel."""
        ai = self.dev.analogIn
        ai.status(True)  # fetch the data
        return {ch: np.asarray(ai.statusData(ch - 1, n), dtype=float) for ch in setups}

    # --- Oscilloscope ---------------------------------------------------------
    def measure_dc(self, duration: float, setups: dict[int, ChannelSetup]) -> dict[int, DcReading]:
        p, ai = self._p, self.dev.analogIn
        ai.reset()
        self._setup_channels(setups)
        n = min(MAX_BUFFER, ai.bufferSizeInfo()[1])
        ai.frequencySet(n / duration)  # one full buffer spans `duration` (after rate rounding)
        ai.bufferSizeSet(n)
        ai.acquisitionModeSet(p.DwfAcquisitionMode.Single)
        ai.triggerSourceSet(p.DwfTriggerSource.None_)
        self._wrap("start", ai.configure, False, True)  # (reconfigure=False, start=True)
        self._wait((p.DwfState.Done,), duration * 3 + 2.0, "a DC acquisition")
        data = self._read(setups, ai.bufferSizeGet())
        return {ch: DcReading(float(x.mean()), float(x.std()), len(x)) for ch, x in data.items()}

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
        p, ai = self._p, self.dev.analogIn
        if trigger_channel not in setups:
            raise InstrumentError(f"trigger channel {trigger_channel} is not enabled")
        ai.reset()
        self._setup_channels(setups)
        ai.frequencySet(sample_rate)
        rate = ai.frequencyGet()  # the AD2 rounds to 100 MHz / integer
        n = min(n_samples, MAX_BUFFER, ai.bufferSizeInfo()[1])
        ai.bufferSizeSet(n)
        n = ai.bufferSizeGet()
        window = n / rate

        ai.acquisitionModeSet(p.DwfAcquisitionMode.Single)
        if force:  # untriggered: record whatever is on the channels right now
            ai.triggerSourceSet(p.DwfTriggerSource.None_)
            self._wrap("start", ai.configure, False, True)
            stimulus()
            self._wait((p.DwfState.Done,), timeout, "an untriggered capture")
            data = self._read(setups, n)
            return Capture(
                rate,
                data.get(1, np.zeros(n)),
                data.get(2, np.zeros(n)),
                round(pre_trigger * n),
                edge_found=False,
            )
        ai.triggerAutoTimeoutSet(0.0)  # normal mode: wait for a real trigger (no auto-trigger)
        if dio_trigger is not None:
            # Digital In's trigger detector watches the AD2's own DIO pin (e.g. the enable line it drives)
            pin, dedge = dio_trigger
            di = self.dev.digitalIn
            di.reset()
            mask = 1 << pin
            # triggerSet(level low, level high, edge rise, edge fall) bit masks
            di.triggerSet(0, 0, mask if dedge == "rising" else 0, mask if dedge == "falling" else 0)
            ai.triggerSourceSet(p.DwfTriggerSource.DetectorDigitalIn)
        else:
            ai.triggerSourceSet(p.DwfTriggerSource.DetectorAnalogIn)
            ai.triggerChannelSet(trigger_channel - 1)
            ai.triggerTypeSet(p.DwfAnalogInTriggerType.Edge)
            ai.triggerLevelSet(trigger_level)
            # 1 % of range hysteresis, so noise on a slow edge can't retrigger
            ai.triggerHysteresisSet(0.01 * setups[trigger_channel].range)
            ai.triggerConditionSet(p.DwfTriggerSlope.Rise if edge == "rising" else p.DwfTriggerSlope.Fall)
        # Single mode: position = time of the window centre relative to the trigger.
        # E.g. pre_trigger 0.1: centre is 0.4 window after the trigger, so 10 % of samples precede it.
        ai.triggerPositionSet(window * (0.5 - pre_trigger))

        self._wrap("arm", ai.configure, False, True)
        self._wait((p.DwfState.Armed,), timeout, "the scope to arm")
        stimulus()
        what = f"DIO {dio_trigger[0]}" if dio_trigger else f"scope channel {trigger_channel}"
        self._wait((p.DwfState.Done,), timeout, f"a {edge} trigger on {what}")
        data = self._read(setups, n)
        ch1 = data.get(1, np.zeros(n))
        ch2 = data.get(2, np.zeros(n))

        expected = round(pre_trigger * n)
        if dio_trigger is not None:  # the trigger isn't on an analog channel: t = 0 is the hardware trigger
            return Capture(rate, ch1, ch2, expected)
        # Locate the edge in the data: more exact than the nominal position (trigger latency, rounding).
        found = _find_crossing(data[trigger_channel], trigger_level, edge, expected)
        if found is None:
            # The hardware triggered but the data has no such edge. Keep the real data; its time axis is
            # the configured trigger position, and edge_found=False says so (reported as a setup issue).
            log.warning("scope triggered, but channel %d's data has no %s edge", trigger_channel, edge)
            return Capture(rate, ch1, ch2, expected, edge_found=False)
        if abs(found - expected) > 0.05 * n:
            log.warning(
                "trigger edge at sample %d, expected near %d; check triggerPositionSet sign on this SDK",
                found,
                expected,
            )
        return Capture(rate, ch1, ch2, found)

    # --- DigitalOut -----------------------------------------------------------
    def write_pins(self, levels: dict[int, bool]) -> None:
        """Drive DIO pins 0..15 (pin -> high); pins not named keep their state."""
        for pin, high in levels.items():
            if not 0 <= pin < 16:
                raise InstrumentError(f"DIO {pin} does not exist on the AD2")
            bit = 1 << pin
            self._dio_enable |= bit
            self._dio_value = self._dio_value | bit if high else self._dio_value & ~bit
        dio = self.dev.digitalIO
        dio.outputSet(self._dio_value)  # set the value before enabling to avoid a glitch
        dio.outputEnableSet(self._dio_enable)
        self._wrap("DIO", dio.configure)

    def release_all(self) -> None:
        """Tri-state every DIO pin (the load banks' pull-downs then switch the loads off)."""
        dio = self.dev.digitalIO
        self._dio_enable = 0
        self._dio_value = 0
        dio.outputEnableSet(0)
        dio.outputSet(0)
        self._wrap("DIO", dio.configure)

    # --- Wavegen --------------------------------------------------------------
    def set_dc(self, channel: int, volts: float) -> None:
        """W1 / W2 (channel 1 / 2) as a DC source (V, +/-5 V). Later calls update the level in place."""
        p, ao = self._p, self.dev.analogOut
        if abs(volts) > 5.0:
            raise InstrumentError("AD2 wavegen is limited to +/-5 V")
        ch, carrier = channel - 1, p.DwfAnalogOutNode.Carrier
        if channel not in self._awg_running:
            ao.reset(ch)
            ao.idleSet(ch, p.DwfAnalogOutIdle.Offset)  # when stopped, hold the offset level, not 0 V
            ao.nodeEnableSet(ch, carrier, True)
            ao.nodeFunctionSet(ch, carrier, p.DwfAnalogOutFunction.DC)
            ao.nodeOffsetSet(ch, carrier, volts)
            self._wrap("wavegen start", ao.configure, ch, 1)  # configure(start): 0 stop, 1 start, 3 apply
            self._awg_running.add(channel)
        else:
            ao.nodeOffsetSet(ch, carrier, volts)
            self._wrap("wavegen update", ao.configure, ch, 3)  # apply without restarting

    def set_sine(self, channel: int, *, frequency: float, low: float, high: float) -> None:
        """Continuous sine between `low` and `high` volts (AD2 W1/W2: +/-5 V)."""
        self.set_wave(channel, shape="sine", frequency=frequency, low=low, high=high)

    def set_wave(self, channel: int, *, shape: WaveShape, frequency: float, low: float, high: float) -> None:
        """Continuous sine or triangle (Hz) between `low` and `high` volts (AD2 W1/W2: +/-5 V)."""
        p, ao = self._p, self.dev.analogOut
        functions = {"sine": p.DwfAnalogOutFunction.Sine, "triangle": p.DwfAnalogOutFunction.Triangle}
        if shape not in functions:
            raise InstrumentError(f"unknown wavegen shape {shape!r} (one of {', '.join(functions)})")
        if not (-5.0 <= low < high <= 5.0):
            raise InstrumentError(f"{shape} {low}..{high} V is outside the AD2 wavegen's +/-5 V")
        ch, carrier = channel - 1, p.DwfAnalogOutNode.Carrier
        ao.reset(ch)
        ao.idleSet(ch, p.DwfAnalogOutIdle.Offset)
        ao.nodeEnableSet(ch, carrier, True)
        ao.nodeFunctionSet(ch, carrier, functions[shape])
        ao.nodeFrequencySet(ch, carrier, frequency)
        ao.nodeAmplitudeSet(ch, carrier, (high - low) / 2)
        ao.nodeOffsetSet(ch, carrier, (high + low) / 2)
        self._wrap(f"wavegen {shape}", ao.configure, ch, 1)
        self._awg_running.discard(channel)  # a later set_dc() must reconfigure from scratch

    def set_vplus(self, volts: float | None) -> None:
        """AD2 V+ supply (0.5..5 V), or None to switch it off. Used for the board's 3.3 V logic rail."""
        aio = self.dev.analogIO
        if volts is None:
            aio.channelNodeSet(0, 0, 0)
            aio.enableSet(False)
        else:
            if not 0.5 <= volts <= 5.0:
                raise InstrumentError(f"AD2 V+ can't be set to {volts} V (0.5..5 V)")
            aio.channelNodeSet(0, 1, volts)  # V+ voltage
            aio.channelNodeSet(0, 0, 1)  # V+ enable
            aio.enableSet(True)  # master enable
        self._wrap("V+ supply", aio.configure)

    def off(self, channel: int) -> None:
        """Set the output to 0 V, then stop the channel."""
        p, ao = self._p, self.dev.analogOut
        ch = channel - 1
        ao.nodeOffsetSet(ch, p.DwfAnalogOutNode.Carrier, 0.0)
        ao.configure(ch, 3)  # apply 0 V first, so stopping (idle = offset) leaves 0 V
        ao.configure(ch, 0)
        self._awg_running.discard(channel)

    def close(self) -> None:
        """Reset every instrument on the AD2 (wavegen, V+, DIO, scope) and close it.

        Each step runs even if an earlier one fails; failures are logged as warnings, since a
        wavegen or V+ that didn't switch off may still be driving the board.
        """
        for name, fn in (
            ("wavegen reset", lambda: self.dev.analogOut.reset(-1)),
            ("V+ supply off", lambda: self.set_vplus(None)),
            ("DIO reset", lambda: self.dev.digitalIO.reset()),
            ("scope reset", lambda: self.dev.analogIn.reset()),
            ("close", lambda: self.dev.close()),
        ):
            try:
                fn()
            except Exception as e:
                log.warning("AD2 %s failed: %s", name, e)


def _find_crossing(x: np.ndarray, level: float, edge: Edge, near: int) -> int | None:
    """Index of the `edge` crossing of `level` closest to `near` (noise can add extra crossings)."""
    if edge == "rising":
        idx = np.nonzero((x[:-1] < level) & (x[1:] >= level))[0]
    else:
        idx = np.nonzero((x[:-1] > level) & (x[1:] <= level))[0]
    if not len(idx):
        return None
    return int(idx[np.argmin(np.abs(idx + 1 - near))]) + 1
