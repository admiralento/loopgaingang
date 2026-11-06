"""Message transports for SCPI instruments on GPIB.

Two kinds of USB-GPIB adapter exist and we support both:

* VISA adapters (Keysight 82357B, NI GPIB-USB-HS, ...): they appear as
  ``GPIB0::5::INSTR`` in NI-VISA / Keysight IO Libraries. Uses PyVISA.
* Prologix-style adapters (Prologix GPIB-USB, AR488 and many clones): they
  appear as a serial port (COMx on Windows, /dev/tty.usbserial-* on macOS)
  and are driven with ``++`` commands. Uses pyserial.

Resource strings:
    "GPIB0::5::INSTR"            VISA, default backend
    "GPIB0::5::INSTR@py"         VISA via pyvisa-py
    "prologix:COM5:5"            Prologix adapter on COM5, GPIB address 5
    "prologix:/dev/tty.usbserial-A1B2:5"
"""

from __future__ import annotations

import contextlib
import logging
import time
from typing import Protocol

from ldo_bench.instruments.base import InstrumentError

log = logging.getLogger(__name__)

PROLOGIX_PREFIX = "prologix:"


class Transport(Protocol):
    name: str

    def write(self, cmd: str) -> None: ...
    def query(self, cmd: str) -> str: ...
    def close(self) -> None: ...


def parse_prologix(resource: str) -> tuple[str, int]:
    """'prologix:COM5:5' -> ('COM5', 5). The port itself may contain ':'."""
    body = resource[len(PROLOGIX_PREFIX) :]
    port, sep, addr = body.rpartition(":")
    if not sep or not port or not addr.isdigit():
        raise InstrumentError(f"bad Prologix resource '{resource}', expected prologix:<port>:<gpib address>")
    return port, int(addr)


def open_transport(resource: str, *, timeout: float = 5.0) -> Transport:
    if resource.lower().startswith(PROLOGIX_PREFIX):
        port, addr = parse_prologix(resource)
        return PrologixTransport(port, addr, timeout=timeout)
    backend = ""
    if "@" in resource:
        resource, backend = resource.split("@", 1)
        backend = "@" + backend
    return VisaTransport(resource, backend=backend, timeout=timeout)


def visa_resource_manager(backend: str = ""):
    import pyvisa

    try:
        return pyvisa.ResourceManager(backend)
    except (OSError, ValueError) as e:
        raise InstrumentError(
            f"could not load VISA{' backend ' + backend if backend else ''}: {e}. "
            "Install NI-VISA or Keysight IO Libraries."
        ) from e


class VisaTransport:
    def __init__(self, resource: str, *, backend: str = "", timeout: float = 5.0) -> None:
        import pyvisa

        self.name = resource + backend
        rm = visa_resource_manager(backend)
        try:
            self._inst = rm.open_resource(resource)
        except pyvisa.Error as e:
            raise InstrumentError(f"could not open {resource}: {e}") from e
        self._inst.timeout = int(timeout * 1000)
        self._inst.read_termination = "\n"
        self._inst.write_termination = "\n"
        self._error = pyvisa.Error

    def write(self, cmd: str) -> None:
        log.debug("%s <- %s", self.name, cmd)
        try:
            self._inst.write(cmd)
        except self._error as e:
            raise InstrumentError(f"{self.name}: write '{cmd}' failed: {e}") from e

    def query(self, cmd: str) -> str:
        try:
            reply = self._inst.query(cmd).strip()
        except self._error as e:
            raise InstrumentError(f"{self.name}: query '{cmd}' failed: {e}") from e
        log.debug("%s <- %s -> %s", self.name, cmd, reply)
        return reply

    def close(self) -> None:
        with contextlib.suppress(self._error):
            self._inst.close()


class PrologixTransport:
    """Prologix-protocol USB-GPIB adapter in controller mode."""

    def __init__(self, port: str, address: int, *, timeout: float = 5.0, serial_obj=None) -> None:
        self.name = f"{PROLOGIX_PREFIX}{port}:{address}"
        self.address = address
        if serial_obj is None:
            import serial

            try:
                # Baud rate is ignored by USB adapters but some clones need a sane value.
                serial_obj = serial.Serial(port, 115200, timeout=timeout)
            except serial.SerialException as e:
                raise InstrumentError(f"could not open serial port {port}: {e}") from e
        self._ser = serial_obj
        tmo_ms = max(500, min(3000, int(timeout * 1000)))
        for c in (
            "++mode 1",
            f"++addr {address}",
            "++auto 0",
            "++eoi 1",
            "++eos 2",
            f"++read_tmo_ms {tmo_ms}",
        ):
            self._raw(c)

    def _raw(self, line: str) -> None:
        self._ser.write((line + "\n").encode("ascii"))

    def set_address(self, address: int) -> None:
        self.address = address
        self._raw(f"++addr {address}")

    def write(self, cmd: str) -> None:
        log.debug("%s <- %s", self.name, cmd)
        # Escape characters the adapter would otherwise interpret.
        esc = cmd.replace("\x1b", "\x1b\x1b").replace("+", "\x1b+").replace("\r", "\x1b\r")
        self._raw(esc)

    def query(self, cmd: str) -> str:
        self._ser.reset_input_buffer()
        self.write(cmd)
        self._raw("++read eoi")
        reply = self._ser.readline().decode("ascii", "replace").strip()
        if not reply:
            raise InstrumentError(f"{self.name}: no reply to '{cmd}' (wrong GPIB address?)")
        log.debug("%s <- %s -> %s", self.name, cmd, reply)
        return reply

    def adapter_version(self) -> str:
        self._ser.reset_input_buffer()
        self._raw("++ver")
        return self._ser.readline().decode("ascii", "replace").strip()

    def close(self) -> None:
        try:
            self._raw("++loc")  # return the instrument to local (front panel) control
            time.sleep(0.05)
        finally:
            self._ser.close()
