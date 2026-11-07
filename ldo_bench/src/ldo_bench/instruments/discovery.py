"""Find and open instruments by model, on any interface.

Every GPIB/RS-232 instrument (E36xxA supply, 34401A DMM, 54621A scope) can be on:
  - VISA: GPIB cards, Keysight 82357B / NI GPIB-USB, the XyphroLabs UsbGpib (USB0::...::INSTR)
  - a Prologix-style USB-GPIB adapter (prologix:<port>:<gpib address>)
  - its own RS-232 port through a USB-serial adapter (serial:<port>)
Set the instrument's `resource` to any of those forms, or leave it '' and it is searched for:
VISA first, then RS-232 ports (with that instrument's serial settings, and the other parities
for the HP-style ones), then Prologix adapters (every GPIB address). One Finder per run caches
each scan, and a port or adapter address claimed by one instrument is never probed for another.

Why this order: VISA listing is fast and harmless. RS-232 probing has to try each framing,
because an HP instrument set to 7E2 answers garbage (or nothing) at 8N2; the HP ones also need
SYST:REM before they answer at all. Prologix is last because it means talking to up to 30 GPIB
addresses per adapter. The 54621A is fixed at 8N1 and is probed only with its own settings.
USB serial ports from Digilent (the AD2) and non-USB ports are never probed.
"""

from __future__ import annotations

import logging
import time
import warnings
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace

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
    SerialSettings,
    SerialTransport,
    open_transport,
    visa_resource_manager,
)

log = logging.getLogger(__name__)

SCAN_TIMEOUT = 0.5  # s per probe while scanning


@dataclass(frozen=True)
class Found:
    """An instrument that answered *IDN?, and the resource string that reaches it."""

    resource: str
    idn: str
    serial: SerialSettings | None = None  # RS-232 instruments: the settings that worked

    @property
    def parity(self) -> str | None:  # kept for messages / older callers
        return self.serial.parity if self.serial else None

    @property
    def port(self) -> str | None:
        """The local port this uses (serial or Prologix adapter), to avoid probing it again."""
        if self.resource.startswith(SERIAL_PREFIX):
            return self.resource[len(SERIAL_PREFIX) :]
        if self.resource.startswith(PROLOGIX_PREFIX):
            return self.resource[len(PROLOGIX_PREFIX) :].rsplit(":", 1)[0]
        return None


@contextmanager
def _step(what: str) -> Iterator[None]:
    """Log what we're doing before a slow step and how long it took, so a scan never looks hung."""
    log.info("%s ...", what)
    t0 = time.monotonic()
    yield
    log.info("  done (%.1f s)", time.monotonic() - t0)


# --- what each instrument looks like -----------------------------------------------------------
def _is_psu(f: Found) -> bool:
    return model_of(f.idn) is not None


def _is_dmm(f: Found) -> bool:
    return DMM_MODEL in f.idn.upper()


def _is_xscope(f: Found) -> bool:
    return any(m in f.idn.upper() for m in SCOPE_MODELS)


def _hp_serial_variants(s: SerialSettings) -> list[SerialSettings]:
    """HP-style RS-232 (E36xx, 34401A): the configured parity first, then the others."""
    return [s, *(replace(s, parity=p) for p in SERIAL_FRAMES if p != s.parity)]


# --- low-level probes ----------------------------------------------------------------------------
def _list(rm, query: str) -> tuple[str, ...]:
    try:
        with warnings.catch_warnings():  # pyvisa-py warns about optional network-scan packages
            warnings.simplefilter("ignore", UserWarning)
            return rm.list_resources(query)
    except Exception as e:  # e.g. pyvisa-py without GPIB support
        log.debug("list_resources(%r) failed: %s", query, e)
        return ()


def _visa_candidates() -> list[Found]:
    """*IDN? of every GPIB / USB VISA instrument, on the installed VISA and on pyvisa-py."""
    found: list[Found] = []
    for backend in ("", "@py"):
        try:
            rm = visa_resource_manager(backend)
            # Only GPIB / USB: avoids slow network scans; serial ports are probed separately.
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


def _serial_ports() -> list[str]:
    """USB serial ports that might be an instrument or a Prologix adapter."""
    try:
        from serial.tools import list_ports
    except ImportError:
        return []
    ports = []
    for p in list_ports.comports():
        if p.vid is None:
            continue  # not USB (Bluetooth, debug console, motherboard COM1, ...)
        if "digilent" in f"{p.manufacturer} {p.description}".lower():
            continue  # the Analog Discovery's own FTDI port: never probe it
        ports.append(p.device)
    return ports


def _probe_serial(port: str, s: SerialSettings, remote: bool) -> str | None:
    """*IDN? of whatever answers on `port` with settings `s`, or None."""
    log.info("  trying %s at %d baud, %s, %d stop, %s", port, s.baud, s.parity, s.stopbits, s.flow)
    try:
        t = SerialTransport(port, baud=s.baud, parity=s.parity, stopbits=s.stopbits, flow=s.flow, timeout=2.0)
    except InstrumentError:
        return None
    try:
        t.write("")  # end any junk an earlier probe left in the instrument's buffer
        if remote:
            t.write("SYST:REM")  # HP RS-232 instruments ignore commands until remote
        idn = t.query("*IDN?")
        t.write("*CLS")  # clear errors from earlier mismatched probes
        if remote:
            t.write("SYST:LOC")
        return idn
    except InstrumentError as e:
        log.debug("%s: nothing at %s: %s", port, s, e)
        return None
    finally:
        t.close()


def _prologix_candidates(preferred_address: int, skip: frozenset[str] = frozenset()) -> list[Found]:
    """Every instrument on every Prologix-style adapter (all GPIB addresses, preferred first)."""
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
            log.info("  %s is a GPIB adapter (%s); scanning GPIB addresses 1-30", port, ver)
            for addr in [preferred_address, *(a for a in range(1, 31) if a != preferred_address)]:
                t.set_address(addr)
                try:
                    found.append(Found(f"{PROLOGIX_PREFIX}{port}:{addr}", t.query("*IDN?")))
                except InstrumentError:
                    continue
        finally:
            t.close()
    return found


# --- the search ------------------------------------------------------------------------------
@dataclass
class Finder:
    """One search per run, shared by all instruments. Scans happen only when needed, once.

    `claimed` holds resource strings and bare port names already in use, so e.g. once the DMM
    owns /dev/cu.usbserial-X, looking for the supply never sends it SYST:REM / *IDN?.
    RS-232 probe results are cached per (port, settings).
    """

    gpib_address: int = 5
    claimed: set[str] = field(default_factory=set)  # resources and local ports in use
    _visa: list[Found] | None = None
    _prologix: list[Found] | None = None
    _serial: dict[tuple[str, SerialSettings], str | None] = field(default_factory=dict)

    def claim(self, f: Found | str) -> None:
        """Mark a resource (and its local port, if any) as taken."""
        if isinstance(f, str):
            f = Found(f, "")
        self.claimed.add(f.resource)
        if f.port:
            self.claimed.add(f.port)

    def visa(self) -> list[Found]:
        """Unclaimed VISA instruments (scanned on first call)."""
        if self._visa is None:
            with _step("Checking VISA (GPIB / USB) instruments"):
                self._visa = _visa_candidates()
        return [f for f in self._visa if f.resource not in self.claimed]

    def serial(self, variants: list[SerialSettings], remote: bool) -> list[Found]:
        """Whatever answers on each unclaimed USB serial port, trying `variants` in order.

        The first settings that get a reply win for that port. remote=True sends SYST:REM first
        (HP supply / DMM); the 54621A doesn't need it.
        """
        out = []
        for port in _serial_ports():
            if port in self.claimed or self._is_prologix_port(port):
                continue
            for s in variants:
                key = (port, s)
                if key not in self._serial:
                    self._serial[key] = _probe_serial(port, s, remote)
                if self._serial[key]:
                    out.append(Found(f"{SERIAL_PREFIX}{port}", self._serial[key], s))
                    break
        return out

    def prologix(self) -> list[Found]:
        """Unclaimed instruments on Prologix-style adapters (scanned once, every address 1-30)."""
        if self._prologix is None:
            with _step("Checking for Prologix-style GPIB adapters (all GPIB addresses)"):
                skip = frozenset(p for p in self.claimed if not p.startswith(("GPIB", "USB")))
                self._prologix = _prologix_candidates(self.gpib_address, skip)
        return [f for f in self._prologix if f.resource not in self.claimed]

    def _is_prologix_port(self, port: str) -> bool:
        return self._prologix is not None and any(f.port == port for f in self._prologix)

    def find(
        self,
        what: str,
        match: Callable[[Found], bool],
        variants: list[SerialSettings],
        *,
        remote: bool,
        hint: str,
        config_key: str,
    ) -> Found:
        """The single instrument matching `match`, searching VISA, RS-232, then Prologix."""
        hits = [f for f in self.visa() if match(f)]
        if not hits:
            with _step(f"Looking for the {what} on RS-232 ports"):
                hits = [f for f in self.serial(variants, remote) if match(f)]
        if not hits:
            hits = [f for f in self.prologix() if match(f)]
        if not hits:
            raise InstrumentError(
                f"no {what} found on VISA (GPIB / USB), RS-232 ports or Prologix adapters.\n{hint}\n"
                "Run `ldo-bench discover -v` for details."
            )
        if len(hits) > 1:
            listing = "\n  ".join(f"{f.resource}  ({f.idn})" for f in hits)
            raise InstrumentError(f"more than one {what} found; set {config_key}:\n  {listing}")
        f = hits[0]
        self.claim(f)
        log.info("Using %s %s (%s)", what, f.resource, f.idn)
        log.info('  tip: set %s = "%s" in the config to skip this search', config_key, f.resource)
        return f


def _open_at(resource: str, serial: SerialSettings, what: str, hint: str, make):
    """Open a configured resource with driver factory `make`; failures carry the setup hint."""
    with _step(f"Opening the {what} at {resource}"):
        try:
            return make(open_transport(resource, serial=serial, timeout=5.0))
        except InstrumentError as e:
            raise InstrumentError(f"{e}\n{hint}") from e


# --- public API ------------------------------------------------------------------------------
def open_psu(
    resource: str = "",
    *,
    gpib_address: int = 5,
    channel: int = 1,
    serial: SerialSettings | None = None,
    finder: Finder | None = None,
) -> AgilentE36xx:
    """The power supply at `resource`, or the one found by searching if it's ''.

    serial: the supply's RS-232 settings (default 9600 8N2, DSR pacing); when searching, the
    other parities are tried too and a mismatch with the config is logged.
    """
    serial = serial or SerialSettings(9600, "none", 2, "dsr-pacing")
    finder = finder or Finder(gpib_address)
    make = lambda t: AgilentE36xx(t, channel)  # noqa: E731
    if resource:
        finder.claim(resource)
        return _open_at(resource, serial, "power supply", GPIB_HINT, make)
    f = finder.find(
        "power supply",
        _is_psu,
        _hp_serial_variants(serial),
        remote=True,
        hint=f"Supported: {', '.join(SUPPORTED)}. {GPIB_HINT}",
        config_key="psu.resource",
    )
    if f.serial and f.serial.parity != serial.parity:
        log.warning(
            'The supply is set to parity "%s": set psu.parity = "%s"', f.serial.parity, f.serial.parity
        )
    return make(open_transport(f.resource, serial=f.serial or serial))


def open_dmm(
    resource: str = "",
    *,
    baud: int = 9600,
    parity: str = "none",
    serial: SerialSettings | None = None,
    finder: Finder | None = None,
) -> HP34401A:
    """The 34401A at `resource`, or the one found by searching if it's ''. RS-232 as for open_psu()."""
    serial = serial or SerialSettings(baud, parity, 2, "dsr-pacing")
    finder = finder or Finder()
    hint = f"{RS232_HINT}\nRun without the DMM with --no-dmm, or set dmm.enabled = false."
    if resource:
        finder.claim(resource)
        return _open_at(resource, serial, "DMM", hint, HP34401A)
    f = finder.find(
        f"{DMM_MODEL} DMM",
        _is_dmm,
        _hp_serial_variants(serial),
        remote=True,
        hint=hint,
        config_key="dmm.resource",
    )
    if f.serial and f.serial.parity != serial.parity:
        log.warning('The DMM is set to parity "%s": set dmm.parity = "%s"', f.serial.parity, f.serial.parity)
    return HP34401A(open_transport(f.resource, serial=f.serial or serial))


def open_xscope(
    resource: str = "",
    *,
    baud: int = 57600,
    flow: str = "dsrdtr",
    serial: SerialSettings | None = None,
    finder: Finder | None = None,
    skip: frozenset[str] = frozenset(),
) -> Agilent54621A:
    """The cross-check scope at `resource`, or found by searching. `skip`: ports not to probe."""
    serial = serial or SerialSettings(baud, "none", 1, flow)
    finder = finder or Finder()
    for p in skip:
        finder.claimed.add(p)
    hint = f"{SCOPE_HINT}\nRun without it with --no-xscope, or set xscope.enabled = false."
    if resource:
        finder.claim(resource)
        return _open_at(resource, serial, "cross-check scope", hint, Agilent54621A)
    f = finder.find(
        "54620-series scope", _is_xscope, [serial], remote=False, hint=hint, config_key="xscope.resource"
    )
    return Agilent54621A(open_transport(f.resource, serial=f.serial or serial))


def open_ad2(serial: str = "") -> Oscilloscope:
    """The Analog Discovery 2 (by serial number, or the only one attached)."""
    from ldo_bench.instruments.ad2 import open_ad2_device

    with _step("Opening the Analog Discovery 2" + (f" {serial}" if serial else "")):
        return open_ad2_device(serial)


def _ad2_lines() -> list[str]:
    """`discover` report lines for the attached AD2s (found through the WaveForms SDK, not VISA)."""
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


def discover(gpib_address: int = 5, cfg=None) -> list[str]:
    """Everything reachable on every interface. `cfg` (a BenchConfig) supplies the RS-232 settings."""
    hp_psu = cfg.psu.serial_settings() if cfg else SerialSettings(9600, "none", 2, "dsr-pacing")
    hp_dmm = cfg.dmm.serial_settings() if cfg and cfg.dmm else SerialSettings(9600, "none", 2, "dsr-pacing")
    scope = cfg.xscope.serial_settings() if cfg and cfg.xscope else SerialSettings(57600, "none", 1, "dsrdtr")
    finder = Finder(cfg.psu.gpib_address if cfg else gpib_address)
    found: list[Found] = list(finder.visa())
    with _step("Checking RS-232 ports (HP-style 8N2 / 7E2 / 7O2 for the supply and DMM)"):
        variants = list(dict.fromkeys(_hp_serial_variants(hp_psu) + _hp_serial_variants(hp_dmm)))
        hp = finder.serial(variants, remote=True)
    for f in hp:
        finder.claim(f)
    with _step(f"Checking RS-232 ports for a 54620-series scope ({scope.baud} baud 8N1, {scope.flow})"):
        scopes = finder.serial([scope], remote=False)
    for f in scopes:
        finder.claim(f)
    found += hp + scopes + finder.prologix()

    lines = []
    for f in found:
        tag = (
            "  <- PSU"
            if _is_psu(f)
            else "  <- DMM"
            if _is_dmm(f)
            else "  <- cross-check scope"
            if _is_xscope(f)
            else ""
        )
        if f.serial:
            tag += f" ({f.serial.baud} baud, parity {f.serial.parity}, {f.serial.stopbits} stop)"
        lines.append(f"{f.resource:<40} {f.idn}{tag}")
    for what, test, hint in (
        (f"supported power supply ({', '.join(SUPPORTED)})", _is_psu, GPIB_HINT),
        (f"{DMM_MODEL} DMM (optional)", _is_dmm, RS232_HINT),
        ("54621A cross-check scope (optional)", _is_xscope, SCOPE_HINT),
    ):
        if not any(test(f) for f in found):
            lines.append(f"No {what} found on GPIB, USB, RS-232 or Prologix.")
            lines.append(f"  {hint}")
    with _step("Checking for Analog Discovery 2s"):
        lines += _ad2_lines()
    return lines
