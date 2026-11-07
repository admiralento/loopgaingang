"""Find and open instruments by model, not by address.

The PSU is found by asking every candidate `*IDN?` and keeping supported Agilent
E36xxA supplies (E3638A, E3648A):
first over VISA (GPIB / USB resources), then on serial ports that answer like
a Prologix-style USB-GPIB adapter. If there is more than one match, the user
picks one with --psu or psu.resource in the config.

The DMM (34401A) is found the same way: on USB serial ports (its own RS-232
port through a USB-serial adapter) and over VISA if it is on GPIB.
"""

from __future__ import annotations

import logging
import time
import warnings
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from ldo_bench.instruments.agilent546xx import MODELS as SCOPE_MODELS
from ldo_bench.instruments.agilent546xx import RS232_HINT as SCOPE_HINT
from ldo_bench.instruments.agilent546xx import Agilent54621A
from ldo_bench.instruments.base import InstrumentError, Oscilloscope
from ldo_bench.instruments.e36xx import GPIB_HINT, SUPPORTED, AgilentE36xx, model_of
from ldo_bench.instruments.hp34401a import HP34401A, RS232_HINT
from ldo_bench.instruments.hp34401a import MODEL as DMM_MODEL
from ldo_bench.instruments.transports import (
    PROLOGIX_PREFIX,
    SERIAL_FRAMES,
    SERIAL_PREFIX,
    PrologixTransport,
    SerialTransport,
    open_transport,
    visa_resource_manager,
)

log = logging.getLogger(__name__)

SCAN_TIMEOUT = 0.5  # s per probe while scanning


@dataclass(frozen=True)
class Found:
    resource: str
    idn: str
    parity: str | None = None  # serial instruments: the parity that worked


@contextmanager
def _step(what: str) -> Iterator[None]:
    """Log what we're doing before a slow step and how long it took, so a scan never looks hung."""
    log.info("%s ...", what)
    t0 = time.monotonic()
    yield
    log.info("  done (%.1f s)", time.monotonic() - t0)


# --- probing -----------------------------------------------------------------
def _visa_candidates() -> list[Found]:
    found: list[Found] = []
    for backend in ("", "@py"):
        try:
            rm = visa_resource_manager(backend)
            # Only GPIB / USB: avoids slow network scans; serial ports are probed as Prologix below.
            resources = [r for q in ("GPIB?*::INSTR", "USB?*::INSTR") for r in _list(rm, q)]
        except Exception as e:  # missing VISA / backend: just skip it
            log.debug("VISA backend %r unavailable: %s", backend or "default", e)
            continue
        for res in resources:
            name = res + backend
            if any(f.resource.split("@")[0] == res for f in found):
                continue  # already seen through the other backend
            log.info("  asking %s who it is", name)
            try:
                t = open_transport(name, timeout=SCAN_TIMEOUT * 4)
                try:
                    found.append(Found(name, t.query("*IDN?")))
                finally:
                    t.close()
            except InstrumentError as e:
                log.debug("no reply from %s: %s", name, e)
    return found


def _list(rm, query: str) -> tuple[str, ...]:
    try:
        with warnings.catch_warnings():  # pyvisa-py warns about optional network-scan packages
            warnings.simplefilter("ignore", UserWarning)
            return rm.list_resources(query)
    except Exception as e:  # e.g. pyvisa-py without GPIB support
        log.debug("list_resources(%r) failed: %s", query, e)
        return ()


def _serial_ports() -> list[str]:
    try:
        from serial.tools import list_ports
    except ImportError:
        return []
    ports = []
    for p in list_ports.comports():
        if p.vid is None:
            continue  # not USB (Bluetooth, debug console, motherboard COM1, ...)
        if "digilent" in f"{p.manufacturer} {p.description}".lower():
            continue  # the Analog Discovery's own FTDI port: never send it ++ commands
        ports.append(p.device)
    return ports


def _serial_instrument_candidates(baud: int = 9600, parity: str = "none") -> list[Found]:
    """Instruments on their own RS-232 port (e.g. the 34401A) behind USB-serial adapters.

    Tries the configured parity first, then the others, since it's a front-panel
    setting that is easy to get out of sync with the config.
    """
    found: list[Found] = []
    order = [parity, *(p for p in SERIAL_FRAMES if p != parity)]
    for port in _serial_ports():
        for par in order:
            log.info("  trying %s at %d baud, parity %s", port, baud, par)
            try:
                t = SerialTransport(port, baud=baud, parity=par, timeout=2.0)
            except InstrumentError:
                break
            try:
                t.write("")  # end any junk a previous probe left in the instrument's buffer
                t.write("SYST:REM")
                idn = t.query("*IDN?")
                t.write("*CLS")  # clear parity errors from earlier mismatched probes
                t.write("SYST:LOC")
                found.append(Found(f"{SERIAL_PREFIX}{port}", idn, par))
                break
            except InstrumentError as e:
                log.debug("%s: no RS-232 instrument at %d baud, parity %s: %s", port, baud, par, e)
            finally:
                t.close()
    return found


def _prologix_candidates(preferred_address: int, skip: frozenset[str] = frozenset()) -> list[Found]:
    found: list[Found] = []
    for port in _serial_ports():
        if port in skip:
            continue
        try:
            t = PrologixTransport(port, preferred_address, timeout=SCAN_TIMEOUT)
        except InstrumentError:
            continue
        try:
            ver = t.adapter_version()
            if not ver:
                continue  # not a Prologix-style adapter
            log.info("  %s is a GPIB adapter (%s); scanning GPIB addresses", port, ver)
            order = [preferred_address, *(a for a in range(1, 31) if a != preferred_address)]
            for addr in order:
                t.set_address(addr)
                try:
                    idn = t.query("*IDN?")
                except InstrumentError:
                    continue
                found.append(Found(f"{PROLOGIX_PREFIX}{port}:{addr}", idn))
                if model_of(idn):
                    break  # found the supply on this adapter; skip the slow full scan
        finally:
            t.close()
    return found


def _is_psu(f: Found) -> bool:
    return model_of(f.idn) is not None


# --- public API ----------------------------------------------------------------
def open_psu(resource: str = "", *, gpib_address: int = 5, channel: int = 1) -> AgilentE36xx:
    if resource:
        with _step(f"Opening the power supply at {resource}"):
            try:
                return AgilentE36xx(open_transport(resource), channel)
            except InstrumentError as e:
                raise InstrumentError(f"{e}\n{GPIB_HINT}") from e
    with _step("Looking for the power supply over VISA (GPIB / USB; can take ~10 s)"):
        psus = [f for f in _visa_candidates() if _is_psu(f)]
    if not psus:
        with _step("No supply on VISA; checking USB-serial ports for a Prologix-style GPIB adapter"):
            psus = [f for f in _prologix_candidates(gpib_address) if _is_psu(f)]
    if not psus:
        raise InstrumentError(
            f"no supported power supply ({', '.join(SUPPORTED)}) found.\n{GPIB_HINT}\n"
            "Also check the GPIB cable and the USB connection. Run `ldo-bench discover -v` for details."
        )
    if len(psus) > 1:
        listing = "\n  ".join(f"{f.resource}  ({f.idn})" for f in psus)
        raise InstrumentError(f"more than one power supply found; pick one with --psu:\n  {listing}")
    log.info("Using PSU %s (%s)", psus[0].resource, psus[0].idn)
    log.info('  tip: set psu.resource = "%s" in the config to skip this search', psus[0].resource)
    return AgilentE36xx(open_transport(psus[0].resource), channel)


def _is_dmm(f: Found) -> bool:
    return DMM_MODEL in f.idn.upper()


def open_dmm(resource: str = "", *, baud: int = 9600, parity: str = "none") -> HP34401A:
    if resource:
        with _step(f"Opening the DMM at {resource}"):
            return HP34401A(open_transport(resource, baud=baud, parity=parity))
    with _step("Looking for the 34401A DMM on USB-serial ports (~2 s per port and parity tried)"):
        dmms = [f for f in _serial_instrument_candidates(baud, parity) if _is_dmm(f)]
    if not dmms:
        with _step("No DMM on serial ports; looking over VISA"):
            dmms = [f for f in _visa_candidates() if _is_dmm(f)]
    if not dmms:
        raise InstrumentError(
            f"no {DMM_MODEL} found on USB-serial ports or VISA.\n{RS232_HINT}\n"
            "Run without the DMM with --no-dmm, or set dmm.enabled = false."
        )
    if len(dmms) > 1:
        listing = "\n  ".join(f"{f.resource}  ({f.idn})" for f in dmms)
        raise InstrumentError(f"more than one {DMM_MODEL} found; set dmm.resource:\n  {listing}")
    f = dmms[0]
    log.info("Using DMM %s (%s)", f.resource, f.idn)
    log.info('  tip: set dmm.resource = "%s" in the config to skip this search', f.resource)
    if f.parity and f.parity != parity:
        log.warning(
            "The DMM is set to parity '%s', not '%s': set dmm.parity = \"%s\"", f.parity, parity, f.parity
        )
    return HP34401A(open_transport(f.resource, baud=baud, parity=f.parity or parity))


def _is_xscope(f: Found) -> bool:
    return any(m in f.idn.upper() for m in SCOPE_MODELS)


def _xscope_candidates(baud: int, flow: str, skip: frozenset[str] = frozenset()) -> list[Found]:
    """54620-series scopes on USB-serial ports (8N1)."""
    found = []
    for port in _serial_ports():
        if port in skip:
            continue
        log.info("  trying %s at %d baud 8N1, %s handshake", port, baud, flow)
        try:
            t = SerialTransport(port, baud=baud, parity="none", stopbits=1, flow=flow, timeout=2.0)
        except InstrumentError:
            continue
        try:
            t.write("")
            found.append(Found(f"{SERIAL_PREFIX}{port}", t.query("*IDN?")))
        except InstrumentError as e:
            log.debug("%s: no 54620-series scope: %s", port, e)
        finally:
            t.close()
    return [f for f in found if _is_xscope(f)]


def open_xscope(
    resource: str = "", *, baud: int = 57600, flow: str = "dsrdtr", skip: frozenset[str] = frozenset()
) -> Agilent54621A:
    """`skip`: serial ports already in use by other instruments (never probe the DMM's port)."""
    if resource:
        if not resource.lower().startswith(SERIAL_PREFIX):
            raise InstrumentError(
                "xscope.resource must be a serial port: 'serial:/dev/cu...' or 'serial:COM4'"
            )
        port = resource[len(SERIAL_PREFIX) :]
        with _step(f"Opening the cross-check scope at {resource}"):
            try:
                return Agilent54621A(SerialTransport(port, baud=baud, stopbits=1, flow=flow, timeout=5.0))
            except InstrumentError as e:
                raise InstrumentError(f"{e}\n{SCOPE_HINT}") from e
    with _step("Looking for the 54621A cross-check scope on USB-serial ports (~2 s per port)"):
        scopes = _xscope_candidates(baud, flow, skip)
    if not scopes:
        raise InstrumentError(
            f"no 54620-series scope found on USB-serial ports at {baud} baud.\n{SCOPE_HINT}\n"
            "Run without it with --no-xscope, or set xscope.enabled = false."
        )
    if len(scopes) > 1:
        raise InstrumentError(
            "more than one scope found; set xscope.resource: " + ", ".join(f.resource for f in scopes)
        )
    f = scopes[0]
    log.info("Using cross-check scope %s (%s)", f.resource, f.idn)
    log.info('  tip: set xscope.resource = "%s" in the config to skip this search', f.resource)
    port = f.resource[len(SERIAL_PREFIX) :]
    return Agilent54621A(SerialTransport(port, baud=baud, stopbits=1, flow=flow, timeout=5.0))


def open_ad2(serial: str = "") -> Oscilloscope:
    from ldo_bench.instruments.ad2 import open_ad2_device

    with _step("Opening the Analog Discovery 2" + (f" {serial}" if serial else "")):
        return open_ad2_device(serial)


def _ad2_lines() -> list[str]:
    from ldo_bench.instruments.ad2 import list_ad2, load_library

    try:
        devices = list_ad2(load_library())
    except InstrumentError as e:
        return [f"Analog Discovery 2: {e}"]
    if not devices:
        return ["No Analog Discovery 2 found."]
    return [
        f"{'AD2 SN:' + sn:<40} Digilent Analog Discovery 2{'  (in use by another program)' if busy else ''}"
        for _, sn, busy in devices
    ]


def discover(gpib_address: int = 5, baud: int = 9600) -> list[str]:
    lines = []
    with _step("Checking USB-serial ports for RS-232 instruments (DMM)"):
        serial_found = _serial_instrument_candidates(baud)
    skip = frozenset(f.resource.removeprefix(SERIAL_PREFIX) for f in serial_found)
    with _step("Checking VISA (GPIB / USB) instruments"):
        visa = _visa_candidates()
    with _step("Checking for Prologix-style GPIB adapters"):
        prologix = _prologix_candidates(gpib_address, skip)
    instruments = visa + prologix + serial_found
    for f in instruments:
        tag = "  <- PSU" if _is_psu(f) else "  <- DMM" if _is_dmm(f) else ""
        if f.parity:
            tag += f" (parity {f.parity})"
        lines.append(f"{f.resource:<40} {f.idn}{tag}")
    if not any(_is_psu(f) for f in instruments):
        lines.append(f"No supported power supply found ({', '.join(SUPPORTED)}).")
        lines.append(GPIB_HINT)
    if not any(_is_dmm(f) for f in instruments):
        lines.append(f"No {DMM_MODEL} DMM found (optional).")
        lines.append(RS232_HINT)
    with _step("Checking USB-serial ports for a 54620-series scope (57600 baud, DTR)"):
        scope_skip = skip | {f.resource.removeprefix(PROLOGIX_PREFIX).rsplit(":", 1)[0] for f in prologix}
        scopes = _xscope_candidates(57600, "dsrdtr", frozenset(scope_skip))
    for f in scopes:
        lines.append(f"{f.resource:<40} {f.idn}  <- cross-check scope")
    if not scopes:
        lines.append("No 54621A cross-check scope found on serial ports (optional).")
        lines.append(SCOPE_HINT)
    with _step("Checking for Analog Discovery 2s"):
        ad2 = _ad2_lines()
    return lines + ad2
