"""Find and open instruments by model, not by address.

The PSU is found by asking every candidate `*IDN?` and keeping E3638As:
first over VISA (GPIB / USB resources), then on serial ports that answer like
a Prologix-style USB-GPIB adapter. If there is more than one match, the user
picks one with --psu or psu.resource in the config.
"""

from __future__ import annotations

import logging
import warnings
from dataclasses import dataclass

from ldo_bench.instruments.base import InstrumentError, Oscilloscope
from ldo_bench.instruments.e3638a import E3638A, MODEL
from ldo_bench.instruments.transports import (
    PROLOGIX_PREFIX,
    PrologixTransport,
    open_transport,
    visa_resource_manager,
)

log = logging.getLogger(__name__)

SCAN_TIMEOUT = 0.5  # s per probe while scanning


@dataclass(frozen=True)
class Found:
    resource: str
    idn: str


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
    return [p.device for p in list_ports.comports()]


def _prologix_candidates(preferred_address: int) -> list[Found]:
    found: list[Found] = []
    for port in _serial_ports():
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
                if MODEL in idn.upper():
                    break  # found the supply on this adapter; skip the slow full scan
        finally:
            t.close()
    return found


def _is_psu(f: Found) -> bool:
    return MODEL in f.idn.upper()


# --- public API ----------------------------------------------------------------
def open_psu(resource: str = "", *, gpib_address: int = 5) -> E3638A:
    if resource:
        return E3638A(open_transport(resource))
    psus = [f for f in _visa_candidates() if _is_psu(f)]
    if not psus:
        psus = [f for f in _prologix_candidates(gpib_address) if _is_psu(f)]
    if not psus:
        raise InstrumentError(
            f"no {MODEL} found. Check the GPIB cable, the supply's GPIB address and that the adapter "
            "driver is installed. Run `ldo-bench discover -v` for details."
        )
    if len(psus) > 1:
        listing = "\n  ".join(f"{f.resource}  ({f.idn})" for f in psus)
        raise InstrumentError(f"more than one {MODEL} found; pick one with --psu:\n  {listing}")
    log.info("Using PSU %s (%s)", psus[0].resource, psus[0].idn)
    return E3638A(open_transport(psus[0].resource))


def open_ad2(serial: str = "") -> Oscilloscope:
    raise InstrumentError("Analog Discovery 2 driver not implemented yet; use --sim")


def discover(gpib_address: int = 5) -> list[str]:
    lines = []
    instruments = _visa_candidates()
    instruments += _prologix_candidates(gpib_address)
    for f in instruments:
        tag = "  <- PSU" if _is_psu(f) else ""
        lines.append(f"{f.resource:<40} {f.idn}{tag}")
    if not instruments:
        lines.append("No GPIB instruments found (VISA or Prologix-style adapter).")
    return lines
