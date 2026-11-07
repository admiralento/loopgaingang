"""Message transports: how a driver's SCPI strings reach the instrument.

Drivers (e36xx, hp34401a, agilent546xx) only call write / query / read_block on a
Transport, so the same driver works whether the instrument is on GPIB or RS-232.
open_transport() picks the class from the resource string.

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
# parity setting -> (data bits, pyserial parity). HP instruments send 7 data bits when parity is
# on (7E2 / 7O2) and 8 when it's off (8N2): the parity bit takes the place of the 8th data bit.
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
    """Data of an IEEE 488.2 definite-length block: #<n><n-digit length><data>[NL].

    E.g. b"#42000" + 2000 data bytes: '4' says the length field has 4 digits. Binary data may
    contain '\\n', so the length, not a terminator, says where the block ends.
    """
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
    """A PyVISA ResourceManager; backend "" = the installed VISA (NI / Keysight), "@py" = pyvisa-py."""
    import pyvisa

    try:
        return pyvisa.ResourceManager(backend)
    except (OSError, ValueError) as e:
        raise InstrumentError(
            f"could not load VISA{' backend ' + backend if backend else ''}: {e}. "
            "Install NI-VISA or Keysight IO Libraries."
        ) from e


class VisaTransport:
    """Anything PyVISA can open: GPIB cards, Keysight / NI USB-GPIB, the UsbGpib (USBTMC).

    Messages are terminated with LF both ways, as SCPI instruments expect.
    """

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
    """Prologix-protocol USB-GPIB adapter in controller mode.

    Lines starting with '++' configure the adapter; anything else is sent to the instrument at
    the current GPIB address. One adapter can reach many addresses (set_address()).
    """

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
            "++mode 1",  # controller (we are the GPIB controller-in-charge)
            f"++addr {address}",
            "++auto 0",  # don't auto-read after each write: we ask with '++read eoi' when a reply is due
            "++eoi 1",  # assert EOI with the last byte we send
            "++eos 2",  # append LF to commands sent to the instrument
            f"++read_tmo_ms {tmo_ms}",  # inter-character read timeout; adapter allows up to 3000 ms
        ):
            self._raw(c)

    def _raw(self, line: str) -> None:
        self._ser.write((line + "\n").encode("ascii"))

    def set_address(self, address: int) -> None:
        """Talk to another GPIB address on the same adapter (used when scanning)."""
        self.address = address
        self._raw(f"++addr {address}")

    def write(self, cmd: str) -> None:
        log.debug("%s <- %s", self.name, cmd)
        # Escape characters the adapter would otherwise interpret (ESC, '+', CR) with ESC.
        esc = cmd.replace("\x1b", "\x1b\x1b").replace("+", "\x1b+").replace("\r", "\x1b\r")
        self._raw(esc)

    def query(self, cmd: str) -> str:
        self._ser.reset_input_buffer()
        self.write(cmd)
        self._raw("++read eoi")
        raw = self._ser.readline()
        reply = raw.decode("ascii", "replace").strip()
        if not reply:
            raise InstrumentError(f"{self.name}: no reply to '{cmd}' (wrong GPIB address?)")
        if not raw.endswith(b"\n"):  # the adapter ends every reply with LF; no LF = timed out mid-reply
            raise InstrumentError(f"{self.name}: reply to '{cmd}' was cut off ({raw[:24]!r})")
        log.debug("%s <- %s -> %s", self.name, cmd, reply)
        return reply

    def read_line(self) -> str:
        self._raw("++read eoi")
        return self._ser.readline().decode("ascii", "replace").strip()

    def read_block(self, cmd: str) -> bytes:
        self._ser.reset_input_buffer()
        self.write(cmd)
        self._raw("++read eoi")
        # Read exactly the header-announced byte count: binary data may contain LF.
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
        """The adapter's '++ver' reply; '' means whatever is on this port isn't a Prologix adapter."""
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

    This is software pacing, not pyserial's dsrdtr flow control, so it degrades
    gracefully when DSR isn't wired. The 54621A, whose DTR mode is a real
    hardware handshake, uses flow="dsrdtr" instead. Replies are checked for
    non-ASCII bytes, the usual symptom of a baud / parity mismatch.
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
        """Wait up to DSR_WAIT for the instrument to say it's ready; give up on DSR for good if it never does."""
        if not self._use_dsr:
            return
        t_end = time.monotonic() + self.DSR_WAIT
        while not getattr(self._ser, "dsr", True):  # test fakes may have no dsr attribute: treat as ready
            if time.monotonic() > t_end:
                log.warning(
                    "%s: DSR never asserted; pacing commands without the handshake from now on "
                    "(the instrument may drop characters)",
                    self.name,
                )
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
        # Anything outside printable ASCII + CR/LF means the framing is wrong (e.g. 8N2 vs 7E2).
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
        if not raw.endswith(b"\n"):  # readline() stopped at the timeout, not at the terminator
            raise InstrumentError(
                f"{self.name}: reply to '{cmd}' was cut off ({raw[:24]!r}): timed out mid-reply"
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
