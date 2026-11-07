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
from ldo_bench.instruments.transports import Transport

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

    def _errors(self) -> list[str]:
        """Drain the SCPI error queue."""
        errs = []
        for _ in range(20):
            e = self.t.query("SYST:ERR?")
            if e.startswith(("+0", "0")):  # '+0,"No error"'
                break
            errs.append(e)
        return errs

    def _check(self, what: str) -> None:
        """Raise InstrumentError if the supply queued any SCPI errors (e.g. a value out of range)."""
        errs = self._errors()
        if errs:
            raise InstrumentError(f"{self.model} error after {what}: {'; '.join(errs)}")

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
            self.t.write(f"VOLT:PROT {ovp:.3f}")
            self.t.write("VOLT:PROT:STAT ON")
            errs = self._errors()
            if errs:
                # Software limits (dut.vin_max / psu.v_max) still apply, but say so loudly.
                log.warning(
                    "%s did not accept OVP settings (%s); relying on software limits", self.model, errs
                )
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

    def output_state(self) -> bool:
        """True if the output is on (OUTP? replies 1 / 0). Not part of the PowerSupply Protocol."""
        return self.t.query("OUTP?").strip() in ("1", "ON")

    def measure_voltage(self) -> float:
        self._select()
        return float(self.t.query("MEAS:VOLT?"))  # readback ADC, not the setpoint

    def measure_current(self) -> float:
        self._select()
        return float(self.t.query("MEAS:CURR?"))

    def close(self) -> None:
        try:
            if self.serial:
                self.t.write("SYST:LOC")  # hand the front panel back
        finally:
            self.t.close()
