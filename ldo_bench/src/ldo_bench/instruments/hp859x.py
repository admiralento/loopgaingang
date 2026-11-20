"""HP 8590 E-series spectrum analyzer (the HP 8591E) over GPIB: the amplitude of one tone at a time.

The 8590 series predates SCPI: it answers ``ID?`` (not ``*IDN?``) and takes HP's own command set,
semicolon-terminated. Confirmed on the bench (2026-11-20, UsbGpib adapter, LF termination):
``ID?`` -> ``HP8591E``, ``REV?`` -> ``940822``, ``SER?`` -> ``3728``. The other commands follow the
8590 E-series Programmer's Guide (08590-90235) from memory, not checked against its text: the
``sa-check`` command exercises every one of them against the analyzer's own CAL OUT before any
measurement relies on them.

Commands used: IP (preset), SNGLS (single-sweep mode), CF / SP / RB / VB (Hz), RL (dBm), AT (dB),
TS;DONE? (take one sweep, wait for it), MKPK HI (marker to the highest peak), MKF (marker to a
frequency), MKF? / MKA? / RB? (read back), ERR? (error codes, "0" when none; reading clears them).
Amplitudes are read in dBm (the preset unit) and converted to volts into the 50 ohm input.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass

from ldo_bench.instruments.base import InstrumentError
from ldo_bench.instruments.transports import Transport

log = logging.getLogger(__name__)

MODEL_PREFIX = "HP859"  # ID? replies "HP8591E", "HP8593E", ... (one command set across the E series)
INPUT_OHMS = 50.0  # the 8591E's standard input (Option 001 is 75 ohm)
AMPLITUDE_RANGE_DBM = (-200.0, 40.0)  # a marker amplitude outside this isn't a reading
SWEEP_TIMEOUT = 60.0  # s: a narrow-RBW sweep can take tens of seconds


def is_hp859x(idn: str) -> bool:
    """True if an ID? reply is an 8590 E-series analyzer."""
    return idn.strip().upper().startswith(MODEL_PREFIX)


def dbm_to_vpeak(dbm: float, ohms: float = INPUT_OHMS) -> float:
    """Sine power (dBm into `ohms`) to its peak voltage (V): Vrms = sqrt(P R), Vpk = sqrt(2) Vrms."""
    return math.sqrt(2.0 * ohms * 1e-3 * 10.0 ** (dbm / 10.0))


def vpeak_to_dbm(vpeak: float, ohms: float = INPUT_OHMS) -> float:
    """Inverse of dbm_to_vpeak (V peak -> dBm into `ohms`)."""
    return 10.0 * math.log10(vpeak**2 / (2.0 * ohms * 1e-3))


@dataclass(frozen=True)
class Marker:
    """One marker reading: frequency (Hz) and amplitude (dBm)."""

    frequency: float
    dbm: float

    @property
    def vpeak(self) -> float:
        """The amplitude as a sine's peak voltage (V) into the 50 ohm input."""
        return dbm_to_vpeak(self.dbm)


class HP859x:
    """An 8590 E-series analyzer on `transport` (LF-terminated; GPIB through VISA or Prologix).

    Every setting is followed by an ERR? check, and every reply is parsed and range-checked here:
    a garbled or out-of-range reply is an InstrumentError, never a number.
    """

    def __init__(self, transport: Transport) -> None:
        self.t = transport
        idn = self.t.query("ID?").strip()
        if not is_hp859x(idn):
            raise InstrumentError(f"{transport.name}: ID? replied {idn!r}, not an HP 8590 E-series analyzer")
        self.model = idn
        self.idn = f"{idn} (firmware {self._query('REV?')}, serial {self._query('SER?')})"
        self.errors()  # clear anything queued before we started (a *IDN? from discovery is one)

    # --- low level ------------------------------------------------------------------------
    def _query(self, cmd: str) -> str:
        reply = self.t.query(cmd).strip()
        if not reply:
            raise InstrumentError(f"{self.t.name}: no reply to {cmd}")
        return reply

    def _number(self, cmd: str) -> float:
        reply = self._query(cmd)
        try:
            return float(reply)
        except ValueError as e:
            raise InstrumentError(f"{self.t.name}: {cmd} replied {reply!r}, not a number") from e

    def errors(self) -> list[int]:
        """The analyzer's error codes (ERR?, which also clears them); [] when there are none."""
        reply = self._query("ERR?")
        try:
            codes = [int(c) for c in reply.replace(";", ",").split(",") if c.strip()]
        except ValueError as e:
            raise InstrumentError(f"{self.t.name}: ERR? replied {reply!r}") from e
        return [c for c in codes if c != 0]

    def _send(self, cmd: str) -> None:
        """Write `cmd` and raise if the analyzer flagged an error for it."""
        self.t.write(cmd)
        if codes := self.errors():
            raise InstrumentError(f"{self.t.name}: '{cmd}' gave error code(s) {codes}")

    # --- setup ---------------------------------------------------------------------------
    def preset(self) -> None:
        """Instrument preset, then single-sweep mode (sweeps only when asked: sweep())."""
        self._send("IP;")
        self._send("SNGLS;")

    def front_panel(self) -> None:
        """Instrument preset only (IP): continuous sweep again, as after power-on, for someone at the front panel."""
        self._send("IP;")

    def tune(
        self,
        *,
        center: float,
        span: float,
        rbw: float,
        vbw: float | None = None,
        ref_level_dbm: float | None = None,
        attenuation_db: float | None = None,
    ) -> float:
        """Centre (Hz), span (Hz, 0 = zero span), resolution / video bandwidth (Hz), reference level
        (dBm) and input attenuation (dB; None = coupled to the reference level). Returns the RBW
        (Hz) the analyzer actually set: it rounds to its own steps and stops at its narrowest."""
        if center <= 0 or span < 0 or rbw <= 0:
            raise InstrumentError(f"bad tuning: centre {center} Hz, span {span} Hz, RBW {rbw} Hz")
        self._send(f"CF {center:.9g}HZ;")
        self._send(f"SP {span:.9g}HZ;")
        self._send(f"RB {rbw:.6g}HZ;")
        if vbw is not None:
            self._send(f"VB {vbw:.6g}HZ;")
        if ref_level_dbm is not None:
            self._send(f"RL {ref_level_dbm:.4g}DBM;")
        if attenuation_db is not None:
            self._send(f"AT {attenuation_db:.4g}DB;")
        return self.rbw()

    def rbw(self) -> float:
        """The resolution bandwidth (Hz) now set."""
        v = self._number("RB?")
        if v <= 0:
            raise InstrumentError(f"{self.t.name}: RB? replied {v}")
        return v

    # --- measuring -----------------------------------------------------------------------
    def sweep(self, timeout: float = SWEEP_TIMEOUT) -> None:
        """Take one sweep and wait for it to finish (TS;DONE? replies 1 when it has)."""
        old = self.t.set_timeout(timeout) if hasattr(self.t, "set_timeout") else None
        try:
            reply = self._query("TS;DONE?")
        finally:
            if old is not None:
                self.t.set_timeout(old)
        if reply.strip() not in ("1", "+1"):
            raise InstrumentError(f"{self.t.name}: TS;DONE? replied {reply!r}, not 1 (sweep not done)")

    def _marker(self) -> Marker:
        f, a = self._number("MKF?"), self._number("MKA?")
        lo, hi = AMPLITUDE_RANGE_DBM
        if not lo <= a <= hi:
            raise InstrumentError(f"{self.t.name}: marker amplitude {a} dBm is outside {lo}..{hi} dBm")
        return Marker(f, a)

    def peak(self) -> Marker:
        """Marker to the highest peak of the last sweep."""
        self._send("MKPK HI;")
        return self._marker()

    def marker_at(self, frequency: float) -> Marker:
        """Marker at `frequency` (Hz) on the last sweep (e.g. off the tone, for the noise floor)."""
        self._send(f"MKF {frequency:.9g}HZ;")
        return self._marker()

    def close(self) -> None:
        """Release the GPIB link (the analyzer keeps its last settings)."""
        self.t.close()
