"""Agilent 54621A (54620-series) oscilloscope over RS-232, used as a cross-check scope.

Optional. During the transient tests it records the same edge as the AD2, averaged
over many repeats, so the AD2 result can be compared with an independent scope.
It is not an Oscilloscope Protocol implementation: it has its own arm() / collect()
API because one averaged acquisition spans many stimulus repeats. Unlike the HP
DMM / supply it needs no SYST:REM on RS-232.

Command syntax from the Agilent 54620-Series Programmer's Guide (pub. 54622-97038):
  RS-232      8 data bits, no parity, 1 stop bit (fixed); 9600 / 19200 / 38400 / 57600 baud;
              DTR or XON/XOFF handshake (Utility > I/O on the scope)
  *IDN?       AGILENT TECHNOLOGIES,<model>,<serial>,X.XX.XX
  :TIMebase:RANGe <full scale>, :REFerence {LEFT|CENTer|RIGHt},
  :POSition   "time from the trigger event to the display reference point"
  :TIMebase:MODE must be normal (MAIN) for :DIGitize and :WAVeform queries
  :TRIGger[:EDGE]:SOURce / :SLOPe {POSitive|NEGative} / :LEVel, :TRIGger:SWEep NORMal
  :ACQuire:TYPE AVERage + :COUNt n: :DIGitize runs until n triggers are averaged, then stops
  :WAVeform:FORMat WORD, :BYTeorder MSBFirst, :UNSigned, :POINts {100..2000}
  :WAVeform:PREamble? format,type,points,count,xinc,xorigin,xref,yinc,yorigin,yref
  *OPC?       '1' per the guide; the bench 54624A (A.02.20) answers '+1', so any numeric 1 is accepted
  :WAVeform:DATA? IEEE block #8nnnnnnnn<data>; V = (code - yref) * yinc + yorigin,
              t = (i - xref) * xinc + xorigin (s from the trigger)

Not yet bench-tested: written from the guide, so check the first captures against the front panel.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np

from ldo_bench.instruments.base import Edge, InstrumentError

log = logging.getLogger(__name__)

MODELS = ("54621A", "54622A", "54624A", "54641A", "54642A")  # same 54620/54640 command set

RS232_HINT = (
    "On the 54621A (Utility > I/O): on GPIB, note its GPIB address. On RS-232, set the baud rate to "
    'match xscope.baud (57600 is fastest) and the handshake to DTR (xscope.flow = "dsrdtr") or '
    'XON/XOFF ("xonxoff"); the port is 8 data bits, no parity, 1 stop bit, on a null-modem cable.'
)


def _is_opc_done(reply: str) -> bool:
    """True if an *OPC? reply means 'complete'. The 54624A (firmware A.02.20) answers '+1', not '1'."""
    try:
        return float(reply) == 1.0
    except ValueError:
        return False


@dataclass
class ScopeTrace:
    """One channel's waveform as the scope returned it."""

    t: np.ndarray  # s, 0 at the scope's trigger
    v: np.ndarray  # V
    averages: int  # acquisitions actually averaged (the preamble's count)


class Agilent54621A:
    """Cross-check scope: arm() an averaged, triggered acquisition, then collect() it."""

    def __init__(self, transport) -> None:
        self.t = transport
        self.t.write("")  # end any partial command
        self.idn = self.t.query("*IDN?")
        up = self.idn.upper()
        self.model = next((m for m in MODELS if m in up), None)
        if self.model is None:
            raise InstrumentError(f"{transport.name} is '{self.idn}', not a 54620/54640-series scope")
        self.t.write("*CLS")
        self._channels: tuple[int, int] = (1, 2)
        self._points = 2000
        self.busy = False  # True if a :DIGitize never completed (commands are queued behind it)

    def _errors(self) -> list[str]:
        """Drain :SYSTem:ERRor? ('<code>,"<message>"', code 0 = no error)."""
        errs = []
        for _ in range(10):
            e = self.t.query(":SYSTem:ERRor?")
            try:
                if int(e.split(",")[0]) == 0:
                    break
            except ValueError:
                pass
            errs.append(e)
        return errs

    def _send(self, *cmds: str) -> None:
        """Send commands, then raise if the scope rejected any of them."""
        for c in cmds:
            self.t.write(c)
        errs = self._errors()
        if errs:
            raise InstrumentError(f"{self.model} rejected {cmds}: {'; '.join(errs)}")

    def arm(
        self,
        *,
        window: float,
        pre_trigger: float,
        vout_channel: int,
        vout_range: float,
        vout_offset: float,
        ref_channel: int,
        ref_range: float,
        ref_offset: float,
        trigger_level: float,
        edge: Edge,
        averages: int,
        points: int = 2000,
    ) -> None:
        """Set up and start :DIGitize; it completes after `averages` triggers on the ref channel.

        window (s) is the full time span; pre_trigger the fraction of it before the trigger.
        Ranges (V full scale) and offsets (V, centre of screen) are per channel. The trigger is
        an `edge` through trigger_level (V) on the ref channel. points: 100..2000 per channel.
        Returns at once; the caller then runs the stimulus and calls collect().
        """
        self._channels = (vout_channel, ref_channel)
        self._points = points
        self._send(":STOP", ":TIMebase:MODE MAIN")
        for ch, rng, off in ((vout_channel, vout_range, vout_offset), (ref_channel, ref_range, ref_offset)):
            self._send(
                f":CHANnel{ch}:DISPlay ON",
                f":CHANnel{ch}:COUPling DC",
                f":CHANnel{ch}:RANGe {rng:.6g}",
                f":CHANnel{ch}:OFFSet {off:.6g}",
            )
        self._send(
            f":TIMebase:RANGe {window:.6g}",
            ":TIMebase:REFerence LEFT",
            # left edge of the screen is pre_trigger * window before the trigger
            f":TIMebase:POSition {-pre_trigger * window:.6g}",
            ":TRIGger:MODE EDGE",
            f":TRIGger:EDGE:SOURce CHANnel{ref_channel}",
            f":TRIGger:EDGE:SLOPe {'POSitive' if edge == 'rising' else 'NEGative'}",
            f":TRIGger:EDGE:LEVel {trigger_level:.6g}",
            ":TRIGger:SWEep NORMal",  # only real triggers count toward the average
            f":ACQuire:TYPE {'AVERage' if averages > 1 else 'NORMal'}",
            f":ACQuire:COUNt {max(1, averages)}",
            ":ACQuire:COMPlete 100",  # :DIGitize isn't done until every point has data
            ":WAVeform:FORMat WORD",  # 16-bit codes: keeps the extra resolution averaging gives
            ":WAVeform:BYTeorder MSBFirst",
            ":WAVeform:UNSigned ON",
            f":WAVeform:POINts {points}",
        )
        # Starts the acquisition; commands sent meanwhile are buffered until it completes.
        self.t.write(f":DIGitize CHANnel{vout_channel},CHANnel{ref_channel}")
        log.info(
            "%s armed: %d averages, %.3g s window, %s edge on ch%d",
            self.model,
            averages,
            window,
            edge,
            ref_channel,
        )

    def collect(self, step_again, max_extra: int, timeout: float) -> dict[int, ScopeTrace]:
        """Wait for :DIGitize to finish and read both channels.

        *OPC? is answered only once the averaged acquisition is complete. If it doesn't come
        promptly (the scope missed some triggers), step_again() repeats the stimulus, up to
        max_extra times / `timeout` s. If it never completes the scope is left busy (commands
        queue behind :DIGitize) and every later call is refused until Run/Stop is pressed.
        """
        import time

        self.t.write("*OPC?")
        old = self.t.set_timeout(0.5)
        t_end = time.monotonic() + timeout
        extra = 0
        done = ""
        try:
            while not done:
                done = self.t.read_line()
                if done or time.monotonic() > t_end or extra >= max_extra:
                    break
                step_again()
                extra += 1
        finally:
            self.t.set_timeout(old)
        if done and not _is_opc_done(done):
            self.busy = True  # state unknown: don't send it more commands this run
            raise InstrumentError(
                f"{self.model} answered '{done}' to *OPC? (expected 1): its acquisition state is unknown; "
                "press Run/Stop on it before the next run"
            )
        if not done:
            self.busy = True
            raise InstrumentError(
                f"{self.model} never finished its averaged acquisition ({extra} extra steps, "
                f"{timeout:.0f} s): is its ch{self._channels[1]} on the trigger signal? The scope is now "
                "waiting for triggers; press Run/Stop on it to free it"
            )
        if extra:
            log.info("%s needed %d extra steps to complete its average", self.model, extra)
        out = {}
        old = self.t.set_timeout(
            max(old or 0, 10.0)
        )  # a 2000-point WORD block is 4 kB: ~0.7 s at 57600 baud, ~4 s at 9600
        try:
            for ch in self._channels:
                self.t.write(f":WAVeform:SOURce CHANnel{ch}")
                pre = [float(x) for x in self.t.query(":WAVeform:PREamble?").split(",")]
                _fmt, _typ, n, count, xinc, xorg, xref, yinc, yorg, yref = pre
                raw = self.t.read_block(":WAVeform:DATA?")
                codes = np.frombuffer(raw, dtype=">u2").astype(float)  # MSB first, unsigned (set in arm())
                v = (codes - yref) * yinc + yorg
                t = (np.arange(len(codes)) - xref) * xinc + xorg
                out[ch] = ScopeTrace(t, v, int(count))
                if len(codes) != int(n):
                    log.warning(
                        "%s ch%d: preamble says %d points, got %d", self.model, ch, int(n), len(codes)
                    )
        finally:
            self.t.set_timeout(old)
        return out

    def close(self) -> None:
        try:
            self.t.write(":RUN")  # leave the scope running for the front panel
        finally:
            self.t.close()
