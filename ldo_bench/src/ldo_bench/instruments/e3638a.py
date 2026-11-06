"""Agilent / Keysight E3638A single-output DC power supply (SCPI over GPIB)."""

from __future__ import annotations

import logging

from ldo_bench.instruments.base import InstrumentError
from ldo_bench.instruments.transports import Transport

log = logging.getLogger(__name__)

MODEL = "E3638A"


class E3638A:
    def __init__(self, transport: Transport) -> None:
        self.t = transport
        self.idn = self.t.query("*IDN?")
        if MODEL not in self.idn.upper():
            raise InstrumentError(f"{transport.name} is '{self.idn}', not an {MODEL}")
        self.t.write("*CLS")

    def _errors(self) -> list[str]:
        """Drain the SCPI error queue."""
        errs = []
        for _ in range(20):
            e = self.t.query("SYST:ERR?")
            if e.startswith(("+0", "0")):
                break
            errs.append(e)
        return errs

    def _check(self, what: str) -> None:
        errs = self._errors()
        if errs:
            raise InstrumentError(f"E3638A error after {what}: {'; '.join(errs)}")

    def setup(self, *, current_limit: float, ovp: float | None, voltage_range: str | None) -> None:
        self.t.write("OUTP OFF")
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
                log.warning("E3638A did not accept OVP settings (%s); relying on software limits", errs)
        log.info(
            "PSU set up: I limit %.3f A, OVP %s, range %s",
            current_limit,
            f"{ovp} V" if ovp else "off",
            voltage_range or "default",
        )

    def set_voltage(self, volts: float) -> None:
        self.t.write(f"VOLT {volts:.4f}")

    def get_voltage_setpoint(self) -> float:
        return float(self.t.query("VOLT?"))

    def output(self, on: bool) -> None:
        self.t.write("OUTP ON" if on else "OUTP OFF")
        if on:
            self._check("OUTP ON")

    def measure_voltage(self) -> float:
        return float(self.t.query("MEAS:VOLT?"))

    def measure_current(self) -> float:
        return float(self.t.query("MEAS:CURR?"))

    def close(self) -> None:
        self.t.close()
