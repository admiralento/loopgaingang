"""HP / Agilent / Keysight 34401A 6.5-digit DMM (SCPI over RS-232 or GPIB).

Over RS-232 the meter must be put in remote mode (SYST:REM) before it takes
commands, and back to local (SYST:LOC) at the end so the front panel works.
"""

from __future__ import annotations

import logging
import time

from ldo_bench.instruments.base import InstrumentError
from ldo_bench.instruments.transports import SerialTransport, Transport

log = logging.getLogger(__name__)

MODEL = "34401A"

RS232_HINT = (
    "On the 34401A front panel (Shift > I/O MENU): set INTERFACE to RS-232, and BAUD RATE / PARITY "
    "to match dmm.baud and dmm.parity (NONE: 8 BITS = 'none', EVEN: 7 BITS = 'even'). The cable "
    "must be a full-handshake null-modem cable (e.g. Agilent 34398A)."
)


class HP34401A:
    def __init__(self, transport: Transport) -> None:
        self.t = transport
        self.serial = isinstance(transport, SerialTransport)
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

    def read_dcv(self) -> float:
        return float(self.t.query("READ?"))

    def close(self) -> None:
        try:
            if self.serial:
                self.t.write("SYST:LOC")
        finally:
            self.t.close()
