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
import warnings
from dataclasses import dataclass

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
            log.debug("%s: GPIB adapter '%s'", port, ver)
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
        try:
            return AgilentE36xx(open_transport(resource), channel)
        except InstrumentError as e:
            raise InstrumentError(f"{e}\n{GPIB_HINT}") from e
    psus = [f for f in _visa_candidates() if _is_psu(f)]
    if not psus:
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
    return AgilentE36xx(open_transport(psus[0].resource), channel)


def _is_dmm(f: Found) -> bool:
    return DMM_MODEL in f.idn.upper()


def open_dmm(resource: str = "", *, baud: int = 9600, parity: str = "none") -> HP34401A:
    if resource:
        return HP34401A(open_transport(resource, baud=baud, parity=parity))
    dmms = [f for f in _serial_instrument_candidates(baud, parity) if _is_dmm(f)]
    if not dmms:
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
    if f.parity and f.parity != parity:
        log.warning(
            "The DMM is set to parity '%s', not '%s': set dmm.parity = \"%s\"", f.parity, parity, f.parity
        )
    return HP34401A(open_transport(f.resource, baud=baud, parity=f.parity or parity))


def open_ad2(serial: str = "") -> Oscilloscope:
    from ldo_bench.instruments.ad2 import open_ad2_device

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
    serial_found = _serial_instrument_candidates(baud)
    skip = frozenset(f.resource.removeprefix(SERIAL_PREFIX) for f in serial_found)
    instruments = _visa_candidates() + _prologix_candidates(gpib_address, skip) + serial_found
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
    return lines + _ad2_lines()
