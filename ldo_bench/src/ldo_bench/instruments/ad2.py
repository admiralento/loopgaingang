"""Digilent Analog Discovery 2 via pydwf (WaveForms SDK).

One object implements three Protocols: Oscilloscope (analog in), DigitalOut
(static DIO) and Wavegen (W1/W2 as DC sources). Channel numbers are 1-based
here and converted to the SDK's 0-based indices internally.

Requires the WaveForms runtime (dwf.dll / dwf.framework). Only one program can
open the AD2, so close the WaveForms GUI first.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

import numpy as np

from ldo_bench.instruments.base import Capture, ChannelSetup, DcReading, Edge, InstrumentError

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
    def __init__(self, device, serial: str) -> None:
        self._p = _pydwf()
        self.dev = device
        self.idn = f"Digilent Analog Discovery 2 (SN:{serial})"
        self._dio_enable = 0
        self._dio_value = 0
        self._awg_running: set[int] = set()
        self.dev.analogIn.reset()
        self.dev.digitalIO.reset()

    # --- helpers --------------------------------------------------------------
    def _wrap(self, what: str, fn: Callable, *args):
        try:
            return fn(*args)
        except self._p.DwfLibraryError as e:
            raise InstrumentError(f"AD2 {what}: {e}") from e

    def _setup_channels(self, setups: dict[int, ChannelSetup]) -> None:
        ai = self.dev.analogIn
        filt = self._p.DwfAnalogInFilter.Average  # averages between samples: lower noise below 100 MS/s
        for ch in (1, 2):
            ai.channelEnableSet(ch - 1, ch in setups)
        for ch, s in setups.items():
            ai.channelRangeSet(ch - 1, s.range)
            ai.channelOffsetSet(ch - 1, s.offset)
            ai.channelFilterSet(ch - 1, filt)

    def _wait(self, states: tuple, timeout: float, what: str) -> None:
        ai = self.dev.analogIn
        t_end = time.monotonic() + timeout
        while True:
            st = ai.status(False)
            if st in states:
                return
            if time.monotonic() > t_end:
                raise InstrumentError(f"AD2 timed out waiting for {what} (state {st.name})")
            time.sleep(POLL)

    def _read(self, setups: dict[int, ChannelSetup], n: int) -> dict[int, np.ndarray]:
        ai = self.dev.analogIn
        ai.status(True)  # fetch the data
        return {ch: np.asarray(ai.statusData(ch - 1, n), dtype=float) for ch in setups}

    # --- Oscilloscope ---------------------------------------------------------
    def measure_dc(self, duration: float, setups: dict[int, ChannelSetup]) -> dict[int, DcReading]:
        p, ai = self._p, self.dev.analogIn
        ai.reset()
        self._setup_channels(setups)
        n = min(MAX_BUFFER, ai.bufferSizeInfo()[1])
        ai.frequencySet(n / duration)
        ai.bufferSizeSet(n)
        ai.acquisitionModeSet(p.DwfAcquisitionMode.Single)
        ai.triggerSourceSet(p.DwfTriggerSource.None_)
        self._wrap("start", ai.configure, False, True)
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
        ai.triggerSourceSet(p.DwfTriggerSource.DetectorAnalogIn)
        ai.triggerAutoTimeoutSet(0.0)  # normal mode: wait for a real trigger
        ai.triggerChannelSet(trigger_channel - 1)
        ai.triggerTypeSet(p.DwfAnalogInTriggerType.Edge)
        ai.triggerLevelSet(trigger_level)
        ai.triggerHysteresisSet(0.01 * setups[trigger_channel].range)
        ai.triggerConditionSet(p.DwfTriggerSlope.Rise if edge == "rising" else p.DwfTriggerSlope.Fall)
        # Single mode: position = time of the window centre relative to the trigger.
        ai.triggerPositionSet(window * (0.5 - pre_trigger))

        self._wrap("arm", ai.configure, False, True)
        self._wait((p.DwfState.Armed,), timeout, "the scope to arm")
        stimulus()
        self._wait((p.DwfState.Done,), timeout, f"a {edge} trigger on scope channel {trigger_channel}")
        data = self._read(setups, n)
        ch1 = data.get(1, np.zeros(n))
        ch2 = data.get(2, np.zeros(n))

        expected = round(pre_trigger * n)
        found = _find_crossing(data[trigger_channel], trigger_level, edge, expected)
        if found is None:
            # The hardware triggered but the captured data has no such edge: don't guess where it was.
            raise InstrumentError(
                f"scope triggered, but channel {trigger_channel}'s data has no {edge} crossing of "
                f"{trigger_level:.3f} V. The trigger signal may be marginal or noisy; check the wiring."
            )
        if abs(found - expected) > 0.05 * n:
            log.warning(
                "trigger edge at sample %d, expected near %d; check triggerPositionSet sign on this SDK",
                found,
                expected,
            )
        return Capture(rate, ch1, ch2, found)

    # --- DigitalOut -----------------------------------------------------------
    def write_pins(self, levels: dict[int, bool]) -> None:
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
        dio = self.dev.digitalIO
        self._dio_enable = 0
        self._dio_value = 0
        dio.outputEnableSet(0)
        dio.outputSet(0)
        self._wrap("DIO", dio.configure)

    # --- Wavegen --------------------------------------------------------------
    def set_dc(self, channel: int, volts: float) -> None:
        p, ao = self._p, self.dev.analogOut
        if abs(volts) > 5.0:
            raise InstrumentError("AD2 wavegen is limited to +/-5 V")
        ch, carrier = channel - 1, p.DwfAnalogOutNode.Carrier
        if channel not in self._awg_running:
            ao.reset(ch)
            ao.idleSet(ch, p.DwfAnalogOutIdle.Offset)
            ao.nodeEnableSet(ch, carrier, True)
            ao.nodeFunctionSet(ch, carrier, p.DwfAnalogOutFunction.DC)
            ao.nodeOffsetSet(ch, carrier, volts)
            self._wrap("wavegen start", ao.configure, ch, 1)
            self._awg_running.add(channel)
        else:
            ao.nodeOffsetSet(ch, carrier, volts)
            self._wrap("wavegen update", ao.configure, ch, 3)  # apply without restarting

    def off(self, channel: int) -> None:
        p, ao = self._p, self.dev.analogOut
        ch = channel - 1
        ao.nodeOffsetSet(ch, p.DwfAnalogOutNode.Carrier, 0.0)
        ao.configure(ch, 3)
        ao.configure(ch, 0)
        self._awg_running.discard(channel)

    def close(self) -> None:
        for name, fn in (
            ("wavegen reset", lambda: self.dev.analogOut.reset(-1)),
            ("DIO reset", self.dev.digitalIO.reset),
            ("scope reset", self.dev.analogIn.reset),
            ("close", self.dev.close),
        ):
            try:
                fn()
            except Exception as e:
                log.debug("AD2 %s failed: %s", name, e)


def _find_crossing(x: np.ndarray, level: float, edge: Edge, near: int) -> int | None:
    """Index of the `edge` crossing of `level` closest to `near` (noise can add extra crossings)."""
    if edge == "rising":
        idx = np.nonzero((x[:-1] < level) & (x[1:] >= level))[0]
    else:
        idx = np.nonzero((x[:-1] > level) & (x[1:] <= level))[0]
    if not len(idx):
        return None
    return int(idx[np.argmin(np.abs(idx + 1 - near))]) + 1
