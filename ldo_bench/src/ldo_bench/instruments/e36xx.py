"""Agilent / Keysight E36xxA DC power supplies (SCPI over GPIB or RS-232).

The PowerSupply driver. Over RS-232 the supply ignores commands until it is put in
remote mode (SYST:REM), and is returned to local (SYST:LOC) on close so the front
panel works again; on GPIB remote/local is handled by the bus.

Supported models:
    E3638A   single output
    E3648A   dual output (OUT1 / OUT2). OUTP ON/OFF switches both outputs together,
             so the output we don't use is held at 0 V.

`psu.channel` in the config picks the output on a dual-output supply.
"""

from __future__ import annotations

import logging

from ldo_bench.instruments.base import InstrumentError
from ldo_bench.instruments.transports import Transport, drain_errors

log = logging.getLogger(__name__)

# model -> number of outputs
SUPPORTED = {"E3638A": 1, "E3648A": 2}

# Accuracy (12 months, 25 +/- 5 C) as (% of value, offset V), from the datasheets.
# E3648A: E364xA data sheet. E3638A: same figures as the E363xA family; check yours.
ACCURACY = {
    "E3648A": {"programming": (0.05, 10e-3), "readback": (0.05, 5e-3)},
    "E3638A": {"programming": (0.05, 10e-3), "readback": (0.05, 5e-3)},
}

# Shown whenever we can't find or talk to the supply.
GPIB_HINT = (
    "Make sure the supply is switched on and its remote interface (front-panel I/O config) matches "
    "how it's connected: GPIB (HP-IB) for a GPIB adapter, RS-232 for a serial cable (then psu.baud / "
    "psu.parity must match too). The UsbGpib adapter only appears on USB when the supply is on and in "
    "GPIB mode (adapter LED solid, not blinking)."
)


def model_of(idn: str) -> str | None:
    """'Agilent Technologies,E3648A,0,1.7-5.0-1.0' -> 'E3648A' if supported."""
    up = idn.upper()
    return next((m for m in SUPPORTED if m in up), None)


class AgilentE36xx:
    """PowerSupply for the E3638A / E3648A. `channel` (1-based) is the output we drive."""

    def __init__(self, transport: Transport, channel: int = 1) -> None:
        self.t = transport
        self.serial = getattr(transport, "kind", "") == "serial"
        if self.serial:  # RS-232 needs remote mode before it accepts commands
            self.t.write("")  # bare LF: ends any partial command left in the input buffer
            self.t.write("SYST:REM")
        self.idn = self.t.query("*IDN?")
        model = model_of(self.idn)
        if model is None:
            raise InstrumentError(
                f"{transport.name} is '{self.idn}', not a supported supply ({', '.join(SUPPORTED)})"
            )
        self.model = model
        self.n_outputs = SUPPORTED[model]
        if not 1 <= channel <= self.n_outputs:
            raise InstrumentError(f"{model} has no output {channel} (it has {self.n_outputs})")
        self.channel = channel
        self.t.write("*CLS")  # empty the error queue so _check() only sees our own errors

    def _select(self, channel: int | None = None) -> None:
        """INST:SEL the output that VOLT / CURR / MEAS apply to (dual-output models only)."""
        if self.n_outputs > 1:
            self.t.write(f"INST:SEL OUT{channel or self.channel}")

    def _check(self, what: str) -> None:
        """Raise InstrumentError if the supply queued any SCPI errors (e.g. a value out of range)."""
        errs = drain_errors(self.t)
        if errs:
            raise InstrumentError(f"{self.model} error after {what}: {'; '.join(errs)}")

    def _set_ovp(self, ovp: float, channel: int, key: str) -> None:
        """Hardware OVP on `channel` (already selected). It's a safety limit: if the supply refuses
        it, stop rather than run without it (`key` names the config setting to change)."""
        self.t.write(f"VOLT:PROT {ovp:.3f}")
        self.t.write("VOLT:PROT:STAT ON")
        errs = drain_errors(self.t)
        if errs:
            raise InstrumentError(
                f"{self.model} did not accept OVP {ovp:.3f} V on OUT{channel} ({'; '.join(errs)}). Stopping: "
                f"set {key} to a level the supply accepts, or remove it from the config to run on the "
                "software limits alone"
            )

    def setup(self, *, current_limit: float, ovp: float | None, voltage_range: str | None) -> None:
        self.t.write("OUTP OFF")
        # On a dual supply OUTP ON enables both outputs: hold the unused ones at 0 V.
        for other in range(1, self.n_outputs + 1):
            if other != self.channel:
                self._select(other)
                self.t.write("VOLT 0")
                log.info("%s OUT%d held at 0 V (outputs switch on together)", self.model, other)
        self._select()
        if voltage_range:
            self.t.write(f"VOLT:RANG {voltage_range}")
            self._check(f"VOLT:RANG {voltage_range}")
        self.t.write(f"CURR {current_limit:.4f}")
        self._check("setting the current limit")
        if ovp is not None:
            self._set_ovp(ovp, self.channel, "psu.ovp")
        log.info(
            "PSU %s OUT%d set up: I limit %.3f A, OVP %s, range %s",
            self.model,
            self.channel,
            current_limit,
            f"{ovp} V" if ovp else "off",
            voltage_range or "default",
        )

    def set_voltage(self, volts: float) -> None:
        self._select()
        self.t.write(f"VOLT {volts:.4f}")

    def get_voltage_setpoint(self) -> float:
        self._select()
        return float(self.t.query("VOLT?"))

    def output(self, on: bool) -> None:
        self.t.write("OUTP ON" if on else "OUTP OFF")  # global: both outputs on the E3648A
        if on:  # e.g. refused while an OVP trip is latched
            self._check("OUTP ON")
        elif self.output_state():  # a dropped OUTP OFF would leave the board powered: say so
            raise InstrumentError(f"{self.model}: sent OUTP OFF but the output is still on")

    def output_state(self) -> bool:
        """True if the output is on (OUTP? replies 1 / 0). Not part of the PowerSupply Protocol."""
        return self.t.query("OUTP?").strip() in ("1", "ON")

    def measure_voltage(self) -> float:
        self._select()
        return float(self.t.query("MEAS:VOLT?"))  # readback ADC, not the setpoint

    def measure_current(self) -> float:
        self._select()
        return float(self.t.query("MEAS:CURR?"))

    def aux(self, channel: int) -> E36xxOutput:
        """Another output of this supply (SupplyOutput), e.g. OUT2 for an external reference."""
        if channel == self.channel or not 1 <= channel <= self.n_outputs:
            raise InstrumentError(
                f"{self.model} has no spare output {channel} (outputs 1..{self.n_outputs}, "
                f"OUT{self.channel} drives the DUT)"
            )
        return E36xxOutput(self, channel)

    def close(self) -> None:
        try:
            if self.serial:
                self.t.write("SYST:LOC")  # hand the front panel back
        finally:
            self.t.close()


class E36xxOutput:
    """SupplyOutput: one more output of an E3648A, sharing the supply's connection.

    Every command selects this output first (INST:SEL), and the supply's own methods select theirs,
    so the two can be used in any order. OUTP ON / OFF stays with the main supply object.
    """

    def __init__(self, supply: AgilentE36xx, channel: int) -> None:
        self.supply = supply
        self.channel = channel

    def setup(self, *, current_limit: float, ovp: float | None) -> None:
        s = self.supply
        s._select(self.channel)
        s.t.write("VOLT 0")
        s.t.write(f"CURR {current_limit:.4f}")
        s._check(f"setting the OUT{self.channel} current limit")
        if ovp is not None:
            s._set_ovp(ovp, self.channel, "vref.ovp (external reference) or buffer.ovp (U2's V-)")
        log.info("PSU %s OUT%d set up: I limit %.4f A, OVP %s", s.model, self.channel, current_limit, ovp)

    def set_voltage(self, volts: float) -> None:
        self.supply._select(self.channel)
        self.supply.t.write(f"VOLT {volts:.4f}")

    def get_voltage_setpoint(self) -> float:
        self.supply._select(self.channel)
        return float(self.supply.t.query("VOLT?"))

    def measure_voltage(self) -> float:
        self.supply._select(self.channel)
        return float(self.supply.t.query("MEAS:VOLT?"))

    def measure_current(self) -> float:
        self.supply._select(self.channel)
        return float(self.supply.t.query("MEAS:CURR?"))
