"""HP / Agilent / Keysight 34401A 6.5-digit DMM (SCPI over RS-232 or GPIB).

Over RS-232 the meter must be put in remote mode (SYST:REM) before it takes
commands, and back to local (SYST:LOC) at the end so the front panel works.
"""

from __future__ import annotations

import logging
import time

from ldo_bench.instruments.base import InstrumentError
from ldo_bench.instruments.transports import Transport

log = logging.getLogger(__name__)

MODEL = "34401A"
OVERLOAD = 9.0e37  # the 34401A reads +/-9.9E+37 on overload

RS232_HINT = (
    "On the 34401A front panel (Shift > I/O MENU), set INTERFACE to match the cable. On GPIB: HP-IB / "
    "488 and note its address. On RS-232: RS-232, with BAUD RATE / PARITY matching dmm.baud and "
    "dmm.parity (NONE: 8 BITS = 'none', EVEN: 7 BITS = 'even'), and a full-handshake null-modem "
    "cable (e.g. Agilent 34398A)."
)


class HP34401A:
    def __init__(self, transport: Transport) -> None:
        self.t = transport
        self.serial = getattr(transport, "kind", "") == "serial"
        if self.serial:
            self.t.write("")  # terminate any partial command left in the meter's buffer
            self.t.write("SYST:REM")
            time.sleep(0.1)
        try:
            self.idn = self.t.query("*IDN?")
        except InstrumentError as e:
            raise InstrumentError(f"{e}\n{RS232_HINT}" if self.serial else str(e)) from e
        if MODEL not in self.idn.upper():
            raise InstrumentError(f"{transport.name} is '{self.idn}', not a {MODEL}")
        self.t.write("*CLS")
        self._nplc = 10.0

    def _check(self, what: str) -> None:
        errs = []
        for _ in range(20):
            e = self.t.query("SYST:ERR?")
            if e.startswith(("+0", "0")):
                break
            errs.append(e)
        if errs:
            raise InstrumentError(f"34401A error after {what}: {'; '.join(errs)}")

    def configure_dcv(self, *, range: float | None, nplc: float) -> None:
        self.t.write(f"CONF:VOLT:DC {range:g}" if range else "CONF:VOLT:DC AUTO")
        self.t.write(f"VOLT:DC:NPLC {nplc:g}")
        self.t.write("ZERO:AUTO ON")
        self.t.write("INP:IMP:AUTO ON")  # >10 GOhm on the 0.1/1/10 V ranges: doesn't load VOUT
        self.t.write("TRIG:SOUR IMM")
        self._check("configuring DC volts")
        self._nplc = nplc
        log.info("DMM: DCV %s, %g NPLC, autozero on, high-Z input", f"{range:g} V" if range else "auto", nplc)

    def configure_dci(self, *, range: float | None, nplc: float) -> None:
        """DC amps on the 3 A / mA input (I and LO terminals). Ranges 10 mA, 100 mA, 1 A, 3 A."""
        self.t.write(f"CONF:CURR:DC {range:g}" if range else "CONF:CURR:DC AUTO")
        self.t.write(f"CURR:DC:NPLC {nplc:g}")
        self.t.write("ZERO:AUTO ON")
        self.t.write("TRIG:SOUR IMM")
        self._check("configuring DC current")
        self._nplc = nplc
        log.info("DMM: DCI %s, %g NPLC, autozero on", f"{range:g} A" if range else "auto", nplc)

    def read(self) -> float:
        """One reading in whatever function is configured; +/-inf if the meter is overloaded."""
        v = float(self.t.query("READ?"))
        return float("inf") * (1 if v > 0 else -1) if abs(v) >= OVERLOAD else v

    def read_dcv(self) -> float:
        return float(self.t.query("READ?"))

    def close(self) -> None:
        try:
            if self.serial:
                self.t.write("SYST:LOC")
        finally:
            self.t.close()
