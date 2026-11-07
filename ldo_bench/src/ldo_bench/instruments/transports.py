"""Message transports for SCPI instruments on GPIB.

Two kinds of USB-GPIB adapter exist and we support both:

* VISA adapters. Two flavours, both handled by PyVISA:
    - USBTMC adapters such as the XyphroLabs UsbGpib (the one on our bench):
      appear as ``USB0::0x03EB::0x2065::<serial built from *IDN?>::INSTR``.
      Works with NI-VISA, or with pyvisa-py + pyusb + libusb-package (no
      driver install needed on macOS). One instrument per adapter, GPIB
      address found automatically.
    - GPIB interface cards / Keysight 82357B / NI GPIB-USB-HS: appear as
      ``GPIB0::5::INSTR`` in NI-VISA / Keysight IO Libraries.
* Prologix-style adapters (Prologix GPIB-USB, AR488 and many clones): they
  appear as a serial port (COMx on Windows, /dev/tty.usbserial-* on macOS)
  and are driven with ``++`` commands. Uses pyserial.

Resource strings:
    "USB0::0x03EB::0x2065::GPIB_5_...::INSTR"   UsbGpib adapter (VISA)
    "GPIB0::5::INSTR"            VISA, default backend
    "GPIB0::5::INSTR@py"         VISA via pyvisa-py
    "prologix:COM5:5"            Prologix adapter on COM5, GPIB address 5
    "prologix:/dev/tty.usbserial-A1B2:5"
    "serial:/dev/cu.usbserial-ABB83W3S"   instrument on its own RS-232 port (e.g. 34401A)
    "serial:COM3"
"""

from __future__ import annotations

import contextlib
import logging
import time
from dataclasses import dataclass
from typing import Protocol

from ldo_bench.instruments.base import InstrumentError

log = logging.getLogger(__name__)

PROLOGIX_PREFIX = "prologix:"
SERIAL_PREFIX = "serial:"
# parity setting -> (data bits, pyserial parity)
SERIAL_FRAMES = {"none": (8, "N"), "even": (7, "E"), "odd": (7, "O")}


class Transport(Protocol):
    """What every driver may use; implemented by VISA, Prologix and serial alike."""

    name: str
    kind: str  # "visa" | "prologix" | "serial"

    def write(self, cmd: str) -> None: ...
    def query(self, cmd: str) -> str: ...
    def read_line(self) -> str: ...  # one reply after write(); "" on timeout
    def read_block(self, cmd: str) -> bytes: ...  # query returning an IEEE 488.2 #-block
    def set_timeout(self, seconds: float) -> float: ...  # returns the previous timeout
    def close(self) -> None: ...


def parse_block(raw: bytes, cmd: str, name: str) -> bytes:
    """Data of an IEEE 488.2 definite-length block: #<n><n-digit length><data>[NL]."""
    if len(raw) < 2 or raw[:1] != b"#" or not raw[1:2].isdigit():
        raise InstrumentError(f"{name}: '{cmd}' did not return a data block (got {raw[:16]!r})")
    nd = int(raw[1:2])
    n = int(raw[2 : 2 + nd])
    data = raw[2 + nd : 2 + nd + n]
    if len(data) != n:
        raise InstrumentError(f"{name}: '{cmd}' block truncated: {len(data)} of {n} bytes")
    return data


def parse_prologix(resource: str) -> tuple[str, int]:
    """'prologix:COM5:5' -> ('COM5', 5). The port itself may contain ':'."""
    body = resource[len(PROLOGIX_PREFIX) :]
    port, sep, addr = body.rpartition(":")
    if not sep or not port or not addr.isdigit():
        raise InstrumentError(f"bad Prologix resource '{resource}', expected prologix:<port>:<gpib address>")
    return port, int(addr)


@dataclass(frozen=True)
class SerialSettings:
    """How to talk to an instrument when it's on RS-232 (ignored for GPIB / USB / Prologix)."""

    baud: int = 9600
    parity: str = "none"  # none = 8 data bits; even / odd = 7 data bits
    stopbits: int = 2
    flow: str = "dsr-pacing"  # dsr-pacing (HP 34401A / E36xx style), dsrdtr, xonxoff, none


def open_transport(
    resource: str,
    *,
    timeout: float = 5.0,
    baud: int = 9600,
    parity: str = "none",
    serial: SerialSettings | None = None,
) -> Transport:
    """Any resource form: VISA ('GPIB0::5::INSTR', 'USB0::...'), 'prologix:<port>:<addr>', 'serial:<port>'.

    `serial` (or the older baud/parity) only matters for 'serial:' resources.
    """
    if resource.lower().startswith(SERIAL_PREFIX):
        s = serial or SerialSettings(baud=baud, parity=parity)
        return SerialTransport(
            resource[len(SERIAL_PREFIX) :],
            baud=s.baud,
            parity=s.parity,
            stopbits=s.stopbits,
            flow=s.flow,
            timeout=timeout,
        )
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
        self.kind = "visa"

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

    def read_line(self) -> str:
        try:
            return self._inst.read().strip()
        except self._error:
            return ""  # timeout: nothing to read yet

    def read_block(self, cmd: str) -> bytes:
        # pyvisa reads the exact byte count from the header, so data bytes equal to '\n' are safe
        try:
            data = self._inst.query_binary_values(cmd, datatype="B", container=bytes)
        except (self._error, ValueError) as e:
            raise InstrumentError(f"{self.name}: '{cmd}' block read failed: {e}") from e
        log.debug("%s <- %s -> %d-byte block", self.name, cmd, len(data))
        return data

    def set_timeout(self, seconds: float) -> float:
        old = self._inst.timeout / 1000
        self._inst.timeout = int(seconds * 1000)
        return old

    def close(self) -> None:
        with contextlib.suppress(self._error):
            self._inst.close()


class PrologixTransport:
    """Prologix-protocol USB-GPIB adapter in controller mode."""

    def __init__(self, port: str, address: int, *, timeout: float = 5.0, serial_obj=None) -> None:
        self.name = f"{PROLOGIX_PREFIX}{port}:{address}"
        self.kind = "prologix"
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

    def read_line(self) -> str:
        self._raw("++read eoi")
        return self._ser.readline().decode("ascii", "replace").strip()

    def read_block(self, cmd: str) -> bytes:
        self._ser.reset_input_buffer()
        self.write(cmd)
        self._raw("++read eoi")
        head = self._ser.read(2)
        nd = int(head[1:2]) if len(head) == 2 and head[1:2].isdigit() else 0
        length = self._ser.read(nd) if nd else b""
        n = int(length) if length.isdigit() else 0
        data = parse_block(head + length + self._ser.read(n), cmd, self.name)
        self._ser.readline()  # trailing NL
        return data

    def set_timeout(self, seconds: float) -> float:
        old = self._ser.timeout
        self._ser.timeout = seconds
        self._raw(f"++read_tmo_ms {max(1, min(3000, int(seconds * 1000)))}")  # adapter's own limit
        return old if old is not None else seconds

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


class SerialTransport:
    """An instrument's own RS-232 port (SCPI over serial), e.g. the 34401A's DB9.

    HP/Agilent bench instruments use 2 stop bits and either 8 data bits with no
    parity or 7 data bits with even/odd parity (a front-panel setting), plus a
    DTR/DSR handshake. We assert DTR (so the instrument may send) and, before
    each command, wait briefly for the instrument's DSR (it drops it while busy;
    the 34401A loses characters otherwise). If DSR never shows up (adapter or
    cable doesn't wire it) we stop waiting for it. Needs a null-modem cable.
    """

    CMD_GAP = 0.05  # s after each command: the 34401A drops back-to-back input
    DSR_WAIT = 0.5  # s to wait for DSR before giving up on the handshake

    def __init__(
        self,
        port: str,
        *,
        baud: int = 9600,
        parity: str = "none",
        stopbits: int = 2,
        flow: str = "dsr-pacing",
        timeout: float = 5.0,
        serial_obj=None,
    ) -> None:
        """flow: "dsr-pacing" (wait for DSR before each command; the 34401A), "dsrdtr" (pyserial
        hardware DTR/DSR flow control; the 54621A's DTR mode), "xonxoff", or "none"."""
        self.name = f"{SERIAL_PREFIX}{port}"
        self.kind = "serial"
        self.port = port
        if parity not in SERIAL_FRAMES:
            raise InstrumentError(f"unknown parity '{parity}', expected one of {list(SERIAL_FRAMES)}")
        if flow not in ("dsr-pacing", "dsrdtr", "xonxoff", "none"):
            raise InstrumentError(f"unknown flow control '{flow}'")
        if stopbits not in (1, 2):
            raise InstrumentError(f"stopbits must be 1 or 2, not {stopbits}")
        self.parity = parity
        if serial_obj is None:
            import serial

            bytesize, par = SERIAL_FRAMES[parity]
            try:
                serial_obj = serial.Serial(
                    port,
                    baud,
                    bytesize=bytesize,
                    parity=par,
                    stopbits=serial.STOPBITS_ONE if stopbits == 1 else serial.STOPBITS_TWO,
                    timeout=timeout,
                    write_timeout=timeout,
                    dsrdtr=flow == "dsrdtr",
                    xonxoff=flow == "xonxoff",
                )
            except serial.SerialException as e:
                raise InstrumentError(f"could not open serial port {port}: {e}") from e
            serial_obj.dtr = True
        self._ser = serial_obj
        self._use_dsr = flow == "dsr-pacing"

    def _wait_dsr(self) -> None:
        if not self._use_dsr:
            return
        t_end = time.monotonic() + self.DSR_WAIT
        while not getattr(self._ser, "dsr", True):
            if time.monotonic() > t_end:
                log.debug("%s: DSR never asserted; not using the handshake", self.name)
                self._use_dsr = False
                return
            time.sleep(0.005)

    def write(self, cmd: str) -> None:
        log.debug("%s <- %s", self.name, cmd)
        self._wait_dsr()
        try:
            self._ser.write((cmd + "\n").encode("ascii"))
            if hasattr(self._ser, "flush"):
                self._ser.flush()  # wait until it's actually on the wire
        except OSError as e:  # pyserial's SerialException / SerialTimeoutException are OSErrors
            raise InstrumentError(
                f"{self.name}: could not send '{cmd}' ({e}): with hardware flow control this means the "
                "instrument isn't asserting its handshake line"
            ) from e
        time.sleep(self.CMD_GAP)

    def query(self, cmd: str) -> str:
        self._ser.reset_input_buffer()
        self.write(cmd)
        raw = self._ser.readline()
        if any(b > 0x7E or (b < 0x20 and b not in (0x0A, 0x0D)) for b in raw):
            raise InstrumentError(
                f"{self.name}: garbled reply to '{cmd}' ({raw[:24]!r}): the instrument's baud rate or "
                f"parity doesn't match ({self.parity})"
            )
        reply = raw.decode("ascii").strip()
        if not reply:
            raise InstrumentError(
                f"{self.name}: no reply to '{cmd}'. Check the instrument's interface is set to RS-232 "
                "(baud and parity matching), and that the cable is null-modem."
            )
        log.debug("%s <- %s -> %s", self.name, cmd, reply)
        return reply

    def read_line(self) -> str:
        """Read one reply line (after a write()); '' on timeout."""
        raw = self._ser.readline()
        return raw.decode("ascii", "replace").strip()

    def set_timeout(self, seconds: float) -> float:
        """Change the read timeout (for long operations); returns the previous one."""
        old = self._ser.timeout
        self._ser.timeout = seconds
        return old

    def read_block(self, cmd: str) -> bytes:
        """Query returning an IEEE 488.2 definite-length block: #<n><n digits of length><data><NL>."""
        self._ser.reset_input_buffer()
        self.write(cmd)
        head = self._ser.read(2)
        if len(head) != 2 or head[:1] != b"#" or not head[1:2].isdigit():
            raise InstrumentError(f"{self.name}: '{cmd}' did not return a data block (got {head!r})")
        ndigits = int(head[1:2])
        length_txt = self._ser.read(ndigits)
        if len(length_txt) != ndigits or not length_txt.isdigit():
            raise InstrumentError(f"{self.name}: bad block length {length_txt!r} after '{cmd}'")
        n = int(length_txt)
        data = self._ser.read(n)
        if len(data) != n:
            raise InstrumentError(f"{self.name}: '{cmd}' block truncated: {len(data)} of {n} bytes")
        self._ser.readline()  # trailing NL
        log.debug("%s <- %s -> %d-byte block", self.name, cmd, n)
        return data

    def close(self) -> None:
        self._ser.close()
