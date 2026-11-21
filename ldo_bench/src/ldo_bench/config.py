"""Bench / DUT configuration loaded from a TOML file.

Everything that depends on the board, the wiring or the test conditions lives in
the config file, so the code never hard-codes an instrument address or a pin.
The config is validated before any instrument is powered up.

Each model class below is one TOML section ([dut], [psu], [load_tran], ...).
`BenchConfig` is the whole file; its `_cross_checks` validator enforces the
limits that span sections (e.g. every test VIN against dut.vin_max). The CLI
re-validates the whole config after applying command-line overrides, so an
option can never bypass these checks. The Bench enforces the same ceilings
again at run time (see bench.py).
"""

from __future__ import annotations

import math
import tomllib
from pathlib import Path
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

# AD2 limits
AD2_DIO_PINS = 16  # DIO 0..15
AD2_WAVEGEN_MAX_V = 5.0  # highest wavegen output used for VIN (V)
W1_MAX_ABOVE_VIN_MAX = 0.2  # V: line_tran.w1_max may sit at most this far above dut.vin_max
AD2_MAX_SAMPLE_RATE = 100e6  # scope sample rate ceiling (Hz)


# RS-232 handshake: 'dsr-pacing' = wait for DSR before each command (HP E36xxA / 34401A),
# 'dsrdtr' = hardware DTR/DSR flow control, 'xonxoff' = software flow control.
FlowName = Literal["dsr-pacing", "dsrdtr", "xonxoff", "none"]


class _Model(BaseModel):
    """Base for every section: unknown keys are an error."""

    # Typos in the TOML file should fail loudly, not be silently ignored.
    model_config = ConfigDict(extra="forbid")


class DutConfig(_Model):
    """[dut]: the board under test: its expected output and the absolute limits every test obeys."""

    name: str = Field("LDO", description="Board name; used in run folder names and plot titles")
    vout_nominal: float = Field(gt=0, description="Expected LDO output voltage (V)")
    vin_max: float = Field(gt=0, description="Highest voltage any test may put on the LDO's VIN (V)")
    vout_tolerance: float = Field(
        0.2,
        gt=0,
        lt=1,
        description=(
            "Before measuring, VOUT must be within this fraction of vout_nominal, else the test stops "
            "(probe not connected, LDO off, wrong board)"
        ),
    )
    i_max: float | None = Field(
        None,
        gt=0,
        description=(
            "Highest load current any test may switch in (A), estimated at vout_nominal; None = no limit"
        ),
    )


class _Instrument(_Model):
    """Shared by [psu], [dmm] and [xscope]: where to find the instrument, and its RS-232 settings.

    The RS-232 fields matter only if the instrument is on a serial port; each section overrides
    the defaults to match its instrument.
    """

    resource: str = Field(
        "",
        description=(
            "'' = auto-discover over VISA (GPIB / USB), RS-232 ports and Prologix adapters. Or any of: "
            "'GPIB0::5::INSTR', 'USB0::...::INSTR' (UsbGpib), 'prologix:COM5:5', 'serial:COM3' / "
            "'serial:/dev/cu.usbserial-XXXX'"
        ),
    )
    baud: int = Field(9600, description="RS-232 baud rate (must match the instrument)")
    parity: Literal["none", "even", "odd"] = Field("none", description="none = 8 data bits; even/odd = 7")
    stopbits: Literal[1, 2] = Field(2, description="RS-232 stop bits (must match the instrument)")
    flow: FlowName = Field("dsr-pacing", description="HP DTR/DSR handshake: wait for DSR before each command")

    def serial_settings(self):
        """RS-232 settings to use if this instrument is on a serial port."""
        from ldo_bench.instruments.transports import SerialSettings

        return SerialSettings(self.baud, self.parity, self.stopbits, self.flow)


class PsuConfig(_Instrument):
    """[psu]: the bench supply (Agilent E3648A / E36xxA): address, RS-232 settings and safety limits.

    current_limit and ovp are programmed before the output is turned on; v_max is the software
    ceiling the Bench checks before every voltage change.
    """

    gpib_address: int = Field(5, ge=0, le=30, description="Tried first when scanning a Prologix adapter")
    channel: int = Field(1, ge=1, le=2, description="Output to use on a dual-output supply (E3648A)")
    voltage_range: Literal["LOW", "HIGH"] | None = Field(None, description="Output range (LOW/HIGH)")
    current_limit: float = Field(gt=0, description="Applied before the output is enabled (A)")
    high_current_limit: float | None = Field(
        None,
        gt=0,
        description=(
            "Higher limit (A) for the tests whose section has high_current = true (load-reg, line-reg, "
            "load-tran, smoke, loadbank-check); None = every test uses current_limit"
        ),
    )
    ovp: float | None = Field(None, gt=0, description="Over-voltage protection level (V)")
    v_max: float = Field(
        gt=0,
        description=(
            "The PSU is never programmed above this (V). Usually equals dut.vin_max; it is higher only "
            "when the PSU powers the buffer rail (line-tran, psrr)."
        ),
    )
    idle_voltage: float | None = Field(
        None,
        ge=0,
        description=(
            "Setpoint (V) left on the output after shutdown, output off, with current_limit (so turning it "
            "on by hand gives the normal VIN); None = 0 V. At most min(dut.vin_max, v_max)"
        ),
    )
    ramp_step: float = Field(0.25, gt=0, description="Max voltage step when ramping (V)")
    settle_time: float = Field(0.3, ge=0, description="Wait after each PSU voltage change (s)")

    @model_validator(mode="after")
    def _ovp_above_v_max(self) -> PsuConfig:
        # OVP is the hardware backstop: if it sat at or below v_max, a legal setpoint could trip it.
        if self.ovp is not None and self.ovp <= self.v_max:
            raise ValueError(f"psu.ovp ({self.ovp} V) must be above psu.v_max ({self.v_max} V)")
        if self.high_current_limit is not None and self.high_current_limit <= self.current_limit:
            raise ValueError(
                f"psu.high_current_limit ({self.high_current_limit} A) must be above psu.current_limit "
                f"({self.current_limit} A)"
            )
        return self


class ScopeChannelConfig(_Model):
    """[ad2.vout] / [ad2.ref]: input range and offset of one AD2 scope channel."""

    range: float = Field(5.0, gt=0, description="Peak-to-peak input range (V). AD2: 5 or 50")
    offset: float = Field(0.0, description="Channel offset (V)")


class Ad2Config(_Model):
    """[ad2]: the Analog Discovery 2: which scope channel watches VOUT, which the reference signal.

    The reference channel is VIN in the DC tests, or the trigger signal (a load bank DIO, the
    enable line, ...) in the transient tests. The same AD2 also drives the load bank DIO pins and
    the wavegen.
    """

    serial: str = Field("", description="Empty = first Analog Discovery 2 found")
    vout_channel: Literal[1, 2] = Field(1, description="Scope channel on VOUT")
    ref_channel: Literal[1, 2] = Field(2, description="Scope channel on VIN or the trigger signal")
    vout: ScopeChannelConfig = ScopeChannelConfig()
    ref: ScopeChannelConfig = ScopeChannelConfig(range=50.0)
    wavegen_channel: Literal[1, 2] = Field(
        1, description="Wavegen output (W1/W2) that drives VIN via the BUF634"
    )
    # How far a DC reading on the fine (5 V) range can be from the truth. Used as the margin
    # between a measured VIN and dut.vin_max whenever the wavegen drives VIN through the buffer.
    # UNVERIFIED: recalled from the AD2 datasheet (about +/-10 mV +/-0.5 %); confirm against
    # Digilent's specification before relying on it.
    vin_offset_error: float = Field(
        0.010, ge=0, description="AD2 DC offset error on the 5 V range (V); unverified datasheet value"
    )
    vin_gain_error: float = Field(
        0.005, ge=0, lt=0.1, description="AD2 DC gain error on the 5 V range (fraction); unverified"
    )

    def vin_margin(self, volts: float) -> float:
        """Worst-case error (V) of an AD2 reading of `volts` on the 5 V range."""
        return self.vin_offset_error + self.vin_gain_error * abs(volts)

    @model_validator(mode="after")
    def _distinct(self) -> Ad2Config:
        if self.vout_channel == self.ref_channel:
            raise ValueError("ad2.vout_channel and ad2.ref_channel must differ")
        return self


class DmmConfig(_Instrument):
    """[dmm]: bench DMM (Agilent 34401A) measuring VOUT in the DC tests (and current in iq / psu-check).

    Leave the section out, set enabled = false, or pass --no-dmm to measure VOUT with the AD2.
    """

    enabled: bool = Field(True, description="false = don't open the DMM (VOUT from the AD2)")
    # RS-232 (34401A: Shift > I/O MENU): the defaults, except parity: NONE: 8 BITS = 'none', EVEN: 7 BITS = 'even'
    nplc: Literal[0.02, 0.2, 1, 10, 100] = Field(10, description="Integration time in power-line cycles")
    range: float | None = Field(None, gt=0, description="Fixed DCV range (V); None = auto range")
    samples: int = Field(1, ge=1, description="Readings averaged per point")


class SaConfig(_Model):
    """[sa]: the HP 8591E spectrum analyzer (8590 E-series) on GPIB, and what `sa-check` expects of it.

    `sa-check` measures the analyzer's own calibrator: CAL OUT cabled to the INPUT, nothing else. It
    exercises every command the driver uses and checks the amplitude reading before PSRR relies on it.
    """

    enabled: bool = Field(True, description="false = don't use the spectrum analyzer")
    resource: str = Field(
        "",
        description="'' = find it on VISA (GPIB / USB: it answers ID?, not *IDN?). Or 'USB0::...::INSTR', "
        "'GPIB0::18::INSTR', 'prologix:COM5:18'",
    )
    timeout: float = Field(
        5.0, gt=0, description="Reply timeout for ordinary commands (s); sweeps get longer"
    )
    cal_frequency: float = Field(300e6, gt=0, description="CAL OUT frequency (Hz)")
    cal_level: float = Field(-20.0, description="CAL OUT level (dBm)")
    cal_tolerance_db: float = Field(
        1.0,
        gt=0,
        description="sa-check: the calibrator must read within this of cal_level (dB). Not from the "
        "datasheet (not read yet): tighten it once its CAL OUT accuracy is known",
    )
    cal_span: float = Field(1e6, gt=0, description="sa-check: span around CAL OUT (Hz)")
    cal_rbw: float = Field(3e3, gt=0, description="sa-check: RBW for the calibrator (Hz)")
    floor_frequency: float = Field(
        1e6, gt=0, description="sa-check: where the noise floor is read, no tone there (Hz)"
    )


class XScopeConfig(_Instrument):
    """[xscope]: Agilent 54621A on RS-232, capturing each transient edge alongside the AD2 as a cross-check.

    load-tran and line-tran use it as an optional cross-check: if it can't be opened the run goes ahead
    with the AD2 alone (--no-xscope skips it for one run). enable-startup reads the input current with
    it (INA - INB, triggered on EN_AIN via Ext Trig; settings in [startup]); without it that run is invalid.
    """

    enabled: bool = Field(True, description="false = don't use the cross-check scope")
    # RS-232 only (Utility > I/O on the scope; the port itself is fixed at 8N1); ignored on GPIB
    baud: Literal[9600, 19200, 38400, 57600] = Field(
        57600, description="Must match Utility > I/O on the scope"
    )
    parity: Literal["none"] = Field("none", description="Fixed: the 54621A port is 8N1")
    stopbits: Literal[1] = Field(1, description="Fixed: the 54621A port is 8N1")
    flow: FlowName = Field("dsrdtr", description="DTR = 'dsrdtr', XON/XOFF = 'xonxoff'")
    vout_channel: Literal[1, 2] = Field(1, description="Scope channel on VOUT")
    ref_channel: Literal[1, 2] = Field(2, description="Scope channel on the same trigger signal as the AD2's")
    vout_range: float = Field(
        0.16, gt=0, description="Ch full-scale range (8 div) on VOUT, centred on vout_nominal (V)"
    )
    points: Literal[100, 250, 500, 1000, 2000] = Field(2000, description="Waveform points read back per edge")
    timeout: float = Field(60.0, gt=0, description="Max wait for the averaged acquisition to complete (s)")

    @model_validator(mode="after")
    def _distinct(self) -> XScopeConfig:
        if self.vout_channel == self.ref_channel:
            raise ValueError("xscope.vout_channel and xscope.ref_channel must differ")
        return self


class LoadBankConfig(_Model):
    """[loadbank]: the binary-weighted load bank: which AD2 DIO drives each bit and its resistor.

    A load "code" is the binary number on the pins (bit 0 = first entry). The resistor values are
    used to estimate the load current (VOUT / R per set bit); nothing measures it directly.
    """

    dio_pins: list[int] = Field(description="AD2 DIO pin for each bank bit, LSB first")
    resistors: list[float] = Field(description="Load resistor for each bit, LSB first (ohm)")
    switch_resistance: float = Field(0.0, ge=0, description="Per-switch on-resistance (ohm)")
    active_high: bool = Field(True, description="true = DIO high switches the bit's resistor in")
    external_ohm: float | None = Field(
        None,
        gt=0,
        description=(
            "A fixed resistor from VOUT to GND (ohm), always connected (e.g. 12 kOhm on JAFWAK's J5 for "
            "0.1 mA): its current is added to every code's estimate and to the dut.i_max check. "
            "None = none (--ext-load-ohm sets it)"
        ),
    )

    @model_validator(mode="after")
    def _check(self) -> LoadBankConfig:
        if len(self.dio_pins) != len(self.resistors):
            raise ValueError("loadbank.dio_pins and loadbank.resistors must be the same length")
        if len(set(self.dio_pins)) != len(self.dio_pins):
            raise ValueError("loadbank.dio_pins has duplicates")
        if any(not 0 <= p < AD2_DIO_PINS for p in self.dio_pins):
            raise ValueError(f"loadbank.dio_pins must be in 0..{AD2_DIO_PINS - 1}")
        if any(r <= 0 for r in self.resistors):
            raise ValueError("loadbank.resistors must be positive")
        return self

    @property
    def max_code(self) -> int:
        """Highest code (all bits on), ignoring dut.i_max."""
        return (1 << len(self.dio_pins)) - 1

    def conductance(self, code: int) -> float:
        """Total conductance (S) on VOUT for `code`, the external resistor included (no range check:
        LoadBank does that)."""
        return self.bank_conductance(code) + (1.0 / self.external_ohm if self.external_ohm else 0.0)

    def bank_conductance(self, code: int) -> float:
        """Conductance (S) of the bank's resistors alone for `code`."""
        return sum(
            1.0 / (r + self.switch_resistance) for bit, r in enumerate(self.resistors) if code >> bit & 1
        )


class AnalysisConfig(_Model):
    """[analysis]: settling analysis of the transient tests (load-tran, line-tran).

    Same parameters and defaults as AD_workspaces/load_tran_script.js; see
    analysis.settling_analysis.
    """

    settle_fraction: float = Field(
        0.1, gt=0, lt=1, description="Settling band = this fraction of the peak deviation from nominal"
    )
    smooth_time: float = Field(1e-6, gt=0, description="Moving-average length applied to VOUT (s)")
    peak_window: float = Field(20e-6, gt=0, description="Search for the peak this long after the step (s)")
    blank_time: float = Field(
        0.5e-6, ge=0, description="Switching glitch on each side of the step replaced by a straight line (s)"
    )
    settle_reference: Literal["pre", "post"] = Field(
        "pre",
        description=(
            "'pre' = settle back to the pre-step level (same as the WaveForms script); "
            "'post' = settle to the final level after the step"
        ),
    )


class PsuCheckConfig(_Model):
    """[psu_check] (psu-check): DMM across the PSU output, nothing else connected.

    Checks the PSU's setpoint and readback against the DMM. No DUT, so only psu.v_max applies.
    """

    points: list[float] = Field(
        default_factory=lambda: [0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0],
        description="Setpoints (V); anything above psu.v_max is refused",
    )
    readings: int = Field(3, ge=1, description="DMM readings averaged per point")
    settle_time: float = Field(0.5, ge=0, description="Extra wait after each setpoint (s)")


class LoadBankCheckConfig(_Model):
    """[loadbank_check] (loadbank-check): the load bank straight across the PSU (no LDO) to verify the DIO wiring.

    Each bit is switched in on its own and its current (bank-off baseline subtracted) is compared with
    V / R. The current comes from the DMM in series (current_from 'dmm', DC amps) or from the supply's
    own readback ('psu'); with the DMM the supply's readback is recorded beside it.
    """

    voltage: float = Field(
        1.0,
        gt=0,
        le=1.5,
        description="PSU voltage on the bank (V). Capped at 1.5 V: the demo board's 15.4 ohm at 1.5 V is already 0.15 W",
    )
    current_from: Literal["dmm", "psu"] = Field(
        "dmm",
        description=(
            "'dmm' = 34401A DC amps in series between the PSU + and the bank (the supply's readback "
            "recorded too); 'psu' = the supply's current readback only"
        ),
    )
    dmm_range: Literal[0.01, 0.1, 1.0, 3.0] = Field(
        1.0,
        description=(
            "DMM current range (A). 1 A: ~0.1 ohm burden, so the bank keeps its voltage (the 10 / 100 mA "
            "ranges' ~5 ohm would drop ~70 mV at 15 mA); the range offset cancels in the baseline subtraction"
        ),
    )
    readings: int = Field(3, ge=1, description="Current readings averaged per state")
    settle_time: float = Field(0.2, ge=0, description="Wait after switching a bit (s)")
    tolerance: float = Field(0.10, gt=0, lt=1, description="Allowed relative current error per bit")
    abs_tolerance: float = Field(
        0.3e-3,
        ge=0,
        description=(
            "Plus this absolute error (A) with the supply's readback: its repeats scattered ~0.05 mA on "
            "JAFWAK (2026-11-14). Small enough that a dead 1 mA bit fails"
        ),
    )
    dmm_abs_tolerance: float = Field(
        0.05e-3, ge=0, description="Plus this absolute error (A) with the DMM (1 uA counts on the 1 A range)"
    )
    high_current: bool = Field(
        False, description="Run with psu.high_current_limit instead of psu.current_limit (it draws more)"
    )


class SmokeConfig(_Model):
    """[smoke] (smoke): power up, VOUT at no load and at the heaviest code within dut.i_max."""

    high_current: bool = Field(
        False, description="Run with psu.high_current_limit instead of psu.current_limit (it draws more)"
    )


class PsuDriveConfig(_Model):
    """Shared by [psu_hold] and [psu_sweep]: the supply on its own, read back by itself (no AD2, no DMM)."""

    current_limit: float | None = Field(
        None,
        gt=0,
        description="Current limit for this run (A), at most psu.current_limit; None = psu.current_limit",
    )
    to: Literal["vin", "bench"] = Field(
        "vin",
        description=(
            "'vin' = the supply is on a board's VIN (ceiling min(dut.vin_max, psu.v_max)); "
            "'bench' = nothing but a load or meter on it (ceiling psu.v_max)"
        ),
    )
    v_tolerance: float = Field(
        0.05,
        gt=0,
        description=(
            "Readback more than this from the setpoint (V) while the current isn't near the limit is a "
            "setup problem (output off, OVP tripped, readback from another output)"
        ),
    )
    near_limit: float = Field(
        0.9,
        gt=0,
        le=1,
        description=(
            "Readback current at or above this fraction of the limit is flagged 'near the current limit' "
            "(the supply may be in constant current). From the readback: the supply's mode isn't queried"
        ),
    )


class PsuHoldConfig(PsuDriveConfig):
    """[psu_hold] (psu-hold): hold the supply at one voltage until Ctrl-C or max_minutes, printing V and I."""

    voltage: float = Field(gt=0, description="Voltage to hold (V)")
    interval: float = Field(1.0, gt=0, description="Time between readbacks (s)")
    max_minutes: float = Field(
        60.0, gt=0, le=60, description="Hard timeout (min): the hold ends on its own after this (at most 60)"
    )


class PsuSweepConfig(PsuDriveConfig):
    """[psu_sweep] (psu-sweep): step the supply from start to stop (and back with updown), reading V and I."""

    start: float = Field(0.0, ge=0, description="First setpoint (V)")
    stop: float = Field(ge=0, description="Last setpoint (V); below start sweeps down")
    step: float = Field(gt=0, description="Step between setpoints (V)")
    dwell: float = Field(0.5, ge=0, description="Wait at each setpoint before reading back (s)")
    updown: bool = Field(False, description="Sweep back to start after reaching stop")

    MAX_POINTS: ClassVar[int] = 2000

    def setpoints(self) -> list[float]:
        """The setpoints in order (V): start, start ± step, ..., stop (always included), then back if updown."""
        span = self.stop - self.start
        n = math.floor(abs(span) / self.step + 1e-9)
        sign = 1.0 if span >= 0 else -1.0
        pts = [round(self.start + sign * k * self.step, 9) for k in range(n + 1)]
        if abs(pts[-1] - self.stop) > 1e-9:
            pts.append(self.stop)
        return pts + pts[-2::-1] if self.updown else pts

    @model_validator(mode="after")
    def _check(self) -> PsuSweepConfig:
        if self.stop == self.start:
            raise ValueError("psu_sweep.stop must differ from start")
        if abs(self.stop - self.start) / self.step > self.MAX_POINTS:
            raise ValueError(f"psu_sweep: more than {self.MAX_POINTS} points; use a bigger step")
        return self


class LoadRegConfig(_Model):
    """[load_reg] (load-reg): fixed VIN from the PSU, step through load codes, record VOUT vs load current."""

    vin: float = Field(gt=0, description="PSU voltage on VIN (V)")
    codes: list[int] = Field(
        default_factory=list, description="Load codes to measure; empty = every code within dut.i_max"
    )
    settle_time: float = Field(0.05, ge=0, description="Wait after each load change (s)")
    dc_window: float = Field(
        0.1, gt=0, description="DC averaging window (s); 0.1 s spans whole 50/60 Hz cycles"
    )
    psu_readback: bool = Field(True, description="Also record the PSU's V/I readback (~0.8 s per point)")
    high_current: bool = Field(
        False, description="Run with psu.high_current_limit instead of psu.current_limit (it draws more)"
    )


class LineRegConfig(_Model):
    """[line_reg] (line-reg): sweep VIN upward from the PSU at a fixed load; slope and dropout knee."""

    vin_start: float = Field(gt=0, description="First VIN of the sweep (V)")
    vin_stop: float = Field(gt=0, description="Last VIN of the sweep (V)")
    vin_step: float = Field(gt=0, description="VIN step (V)")
    load_code: int = Field(0, ge=0, description="Load bank code during the sweep")
    fit_vin_min: float | None = Field(
        None, description="Only fit the slope above this Vin (V); default = 3 x step above the dropout knee"
    )
    dropout_fraction: float = Field(
        0.01, gt=0, lt=1, description="Dropout = Vout falls this fraction below its max-Vin value"
    )
    dc_window: float = Field(0.1, gt=0, description="AD2 DC averaging window (s)")
    psu_readback: bool = Field(True, description="Also record the PSU's V/I readback (~0.8 s per point)")
    vin_source: Literal["ad2", "psu"] = Field(
        "ad2",
        description=(
            "'ad2' = VIN measured at the board by scope ref channel; "
            "'psu' = the PSU's readback (no scope wiring; includes cable drop and ~1-3 mV readback error)"
        ),
    )
    high_current: bool = Field(
        False, description="Run with psu.high_current_limit instead of psu.current_limit (it draws more)"
    )

    @model_validator(mode="after")
    def _check(self) -> LineRegConfig:
        if self.vin_stop <= self.vin_start:
            raise ValueError("line_reg.vin_stop must be above vin_start (the sweep goes up)")
        return self


class VrefLineConfig(_Model):
    """[vref_line] (vref-line): sweep VIN down with the LDO on and read VREF (TP3) on the DMM.

    Gives the BGR's line sensitivity (mV/V) and the lowest VIN it still works at (where VREF falls
    drop_fraction below its value at vin_high). VOUT on the AD2 shows whether the LDO follows.
    """

    vin_high: float = Field(1.9, gt=0, description="First (highest) VIN of the sweep (V)")
    vin_low: float = Field(1.4, gt=0, description="Last (lowest) VIN of the sweep (V)")
    vin_step: float = Field(0.02, gt=0, description="VIN step (V)")
    load_code: int = Field(0, ge=0, description="Load bank code during the sweep")
    vref_nominal: float = Field(
        1.204, gt=0, description="Expected VREF (V); the run checks the DMM reads near it at vin_high"
    )
    vref_tolerance: float = Field(
        0.2, gt=0, lt=1, description="VREF at vin_high must be within this fraction of vref_nominal"
    )
    drop_fraction: float = Field(
        0.01,
        gt=0,
        lt=1,
        description="BGR minimum VIN = where VREF falls this fraction below its value at vin_high",
    )
    settle_time: float = Field(0.2, ge=0, description="Wait after each VIN step before reading (s)")
    dc_window: float = Field(0.1, gt=0, description="AD2 DC averaging window (s)")
    vin_source: Literal["ad2", "psu"] = Field(
        "ad2",
        description=(
            "'ad2' = VIN measured at the board (scope ref channel on J13 pin 1, INA), VOUT checked on "
            "the AD2; 'psu' = the PSU's readback (no scope wiring, VOUT not checked)"
        ),
    )

    @model_validator(mode="after")
    def _check(self) -> VrefLineConfig:
        if self.vin_low >= self.vin_high:
            raise ValueError("vref_line.vin_low must be below vin_high (the sweep goes down)")
        return self


TEMP_MAX_DURATION = 2 * 3600.0  # s: the temperature log's hard timeout


class TempConfig(_Model):
    """[temp] (temp): log the board temperature (TMP235 on the AD2), VREF (DMM) and VOUT over time.

    The TMP235 (TEMP, TP7, J13 pin 5) runs from VS_BUFF, set by hand. Its output is
    tmp_offset + tmp_slope * T (500 mV at 0 C, 10 mV/C). Where the temperature holds within
    stable_delta over stable_window, the readings are averaged into a plateau; two plateaus at least
    min_spread apart give the temperature coefficient of VREF and VOUT.
    """

    vin: float = Field(1.8, gt=0, description="PSU on VIN (V)")
    load_code: int = Field(0, ge=0, description="Load bank code while logging")
    interval: float = Field(2.0, gt=0, description="Time between samples (s)")
    duration: float = Field(
        1800.0,
        gt=0,
        le=TEMP_MAX_DURATION,
        description="Log length (s), at most 2 h; Ctrl-C ends it early and keeps the data",
    )
    stable_window: float = Field(
        30.0, gt=0, description="A sample is stable if T held within stable_delta this long (s)"
    )
    stable_delta: float = Field(0.5, gt=0, description="Allowed temperature change over stable_window (C)")
    min_spread: float = Field(2.0, gt=0, description="Plateaus must span at least this much for a tempco (C)")
    dc_window: float = Field(0.1, gt=0, description="AD2 averaging window per sample (s)")
    tmp_offset: float = Field(0.5, description="TMP235 output at 0 C (V)")
    tmp_slope: float = Field(0.010, gt=0, description="TMP235 output slope (V/C)")
    temp_min: float = Field(-20.0, description="Readings below this (C) mean the TMP235 isn't being read")
    temp_max: float = Field(80.0, description="Readings above this (C) mean the TMP235 isn't being read")
    vref_nominal: float = Field(1.204, gt=0, description="Expected VREF (V) on the DMM")
    vref_tolerance: float = Field(
        0.2, gt=0, lt=1, description="VREF must stay within this fraction of vref_nominal"
    )
    psu_readback: bool = Field(True, description="Also record the supply current (~0.4 s per sample)")
    # VS_BUFF (J4) from the PSU's other output: it powers the TMP235 (2.3-5.5 V) and U2 (the BUF634)
    vs_buff_v: float = Field(5.0, gt=0, description="PSU's other output on VS_BUFF (V), at most psu.v_max")
    vs_buff_channel: int = Field(2, ge=1, le=2, description="PSU output on VS_BUFF (not psu.channel)")
    vs_buff_current_limit: float = Field(
        0.1, gt=0, description="Current limit on that output (A) (approved 2026-11-21)"
    )
    vs_buff_ovp: float | None = Field(5.5, gt=0, description="OVP on that output (V), above vs_buff_v")
    vs_buff_max_current: float = Field(
        0.03,
        gt=0,
        description=(
            "More than this (A) from VS_BUFF once it is up (U2 draws up to ~15 mA, the TMP235 ~10 uA) "
            "means a miswire or a short: VS_BUFF comes down and the run stops"
        ),
    )

    @model_validator(mode="after")
    def _check(self) -> TempConfig:
        if self.vs_buff_ovp is not None and self.vs_buff_ovp <= self.vs_buff_v:
            raise ValueError("temp.vs_buff_ovp must be above temp.vs_buff_v")
        return self


class TransientCaptureConfig(_Model):
    """AD2 capture settings shared by [load_tran] and [line_tran] (not a section of its own)."""

    sample_rate: float = Field(10e6, gt=0, description="AD2 scope sample rate (Hz)")
    n_samples: int = Field(8192, gt=16, description="Samples per capture; window = n_samples / sample_rate")
    pre_trigger: float = Field(0.2, gt=0, lt=1, description="Fraction of the capture before the step")
    averages: int = Field(16, ge=1, description="Captures averaged per edge")
    edges: list[Literal["rising", "falling"]] = Field(
        default_factory=lambda: ["rising", "falling"], description="Edges to capture, in this order"
    )
    timeout: float = Field(5.0, gt=0, description="Max wait for one AD2 trigger (s)")
    rest_time: float = Field(0.02, ge=0, description="Settle at the starting level before each capture (s)")
    plot_window: float | None = Field(
        None,
        gt=0,
        description=(
            "Zoomed plot (plot.png): time shown after the step (s), the same for every edge. "
            "None = automatic (2.5 x the slowest settling time + 10 us). "
            "E.g. 100e-6 matches a scope at 10 us/div"
        ),
    )


class LoadTranConfig(TransientCaptureConfig):
    """[load_tran] (load-tran): VIN from the PSU, the load bank stepped between two codes.

    The AD2 triggers on one load bank DIO pin wired to the ref channel; 'rising' = light to heavy load.
    """

    vin: float = Field(gt=0, description="PSU voltage on VIN (V)")
    code_low: int = Field(ge=0, description="Light-load code")
    code_high: int = Field(ge=0, description="Heavy-load code (must be above code_low)")
    trigger_pin: int | None = Field(
        None, description="DIO pin wired to the ref channel; default = MSB that changes"
    )
    dio_high_level: float = Field(3.3, gt=0, description="DIO logic-high voltage, for the trigger level")
    high_current: bool = Field(
        False, description="Run with psu.high_current_limit instead of psu.current_limit (it draws more)"
    )


class BoardConfig(_Model):
    """[board]: where things connect on this board, as the wiring lines name them.

    Each value is a point on the board: on JAFWAK the silkscreen label with its connector and pin
    (e.g. "J13 pin 4 (OUTA)"). The defaults are plain names for a board described no further. The
    wiring lines are built from these in wiring.py, one connection per line.
    """

    vin_dc: str = Field("VIN (DC input)", description="Where the PSU's + goes for the DC tests")
    supply_gnd: str = Field("GND", description="Where the PSU's - goes")
    ext_load: str = Field("VOUT to GND", description="Where an external load resistor (--ext-load-ohm) goes")
    dc_jumpers: list[str] = Field(default_factory=list, description="Jumpers that route VIN from vin_dc")
    vin_probe: str = Field("VIN at the board", description="Scope point for VIN")
    vout_probe: str = Field("VOUT at the output pins", description="Scope point for VOUT")
    probe_gnd: str = Field("GND next to VOUT", description="Scope ground point")
    enable_ldo_in: str = Field(
        "EN_IN", description="The LDO's enable input, after the enable block (enable-startup)"
    )
    dmm_vout: str = Field("VOUT at the output pins", description="DMM V point for VOUT")
    dmm_gnd: str = Field("GND at the output pins", description="DMM COM point")
    bank_bits: list[str] = Field(
        default_factory=list,
        description="Load bank gate inputs, one per loadbank.dio_pins; [] = 'load bank B<k>'",
    )
    bank_supply: list[str] = Field(
        default_factory=list,
        description="Connections that power the load bank / its switches by hand (left out when the PSU already does)",
    )
    bank_jumpers: list[str] = Field(default_factory=list, description="Jumpers that connect the load bank")
    ad2_gnd: str = Field("board GND", description="Where AD2 GND goes")
    vs_buff: str = Field("buffer rail (VS_BUFF / VDD_BUF)", description="The buffer's supply input")
    buffer_input: str = Field("buffer input SMA (VIN_FG)", description="Where W1 drives the buffer")
    buffer_jumpers: list[str] = Field(
        default_factory=lambda: ["JP4 to the buffer"], description="Jumpers that route VIN from the buffer"
    )
    enable_in: str = Field("EN_CTRL_33", description="Where the enable DIO goes")
    enable_in_n: str = Field("~EN_CTRL_33", description="Where the inverted enable DIO goes")
    enable_vio: str = Field("V_33", description="Where AD2 V+ (the level shifters' 3.3 V) goes")
    enable_jumpers: list[str] = Field(
        default_factory=list, description="Jumpers for the AD2-driven enable path"
    )
    static_enable_jumpers: list[str] = Field(
        default_factory=list, description="Jumpers that tie the LDO on (the DC tests without --enable dio)"
    )
    standard_jumpers: list[str] = Field(
        default_factory=list,
        description=(
            "Jumpers every test with the chip powered needs (JAFWAK: the four J1 rails and JP2). "
            "Bench.confirm_wiring lists each one after the input routing unless the test's own wiring "
            "already names that jumper (a DMM in its place, Method B), or it is JP2 with --vref ext"
        ),
    )


class EnableConfig(_Model):
    """[enable]: how the AD2 drives the board's enable path (enable-startup, iq 'disabled', --enable dio).

    JAFWAK: AD2 DIO -> J13.12 EN_CTRL_33 -> FXLP34 (U6) -> EN_CTRL_18, and DIO -> J13.10 ~EN_CTRL_33
    -> U5 -> ~EN_CTRL_18. The level shifters' 3.3 V side is V_33 (J13.14), which the AD2's V+ supply
    can provide; their low side runs from VIN.
    """

    dio_pin: int = Field(5, ge=0, le=15, description="AD2 DIO wired to EN_CTRL_33 (J13.12)")
    dio_pin_n: int | None = Field(
        6, ge=0, le=15, description="AD2 DIO wired to ~EN_CTRL_33 (J13.10), driven inverted; None = not wired"
    )
    v33: float | None = Field(
        3.3,
        ge=0.5,
        le=5.0,
        description="AD2 V+ on V_33 (J13.14) for the level shifters; None = supplied externally",
    )
    settle_time: float = Field(
        0.2,
        ge=0,
        description=(
            "--enable dio: wait after setting EN high before measuring (s); JAFWAK's simulated startup "
            "is up to 64 ms (PEX corners)"
        ),
    )

    @property
    def pins(self) -> set[int]:
        """The DIO pins the enable path uses."""
        return {self.dio_pin} | ({self.dio_pin_n} - {None})

    @model_validator(mode="after")
    def _distinct(self) -> EnableConfig:
        if self.dio_pin_n is not None and self.dio_pin_n == self.dio_pin:
            raise ValueError("enable.dio_pin and enable.dio_pin_n must differ")
        return self


class EnSineConfig(_Model):
    """[en_sine] (enable-sine): a sine, triangle or square into the enable block's analog input; its output gives the thresholds (or, for a square, the propagation delay)."""

    vin: float = Field(gt=0, description="PSU on VIN (feeds EN_VDD through J1 9-11) (V)")
    shape: Literal["sine", "triangle", "square"] = Field(
        "sine",
        description=(
            "Input wave: sine, triangle (a linear ramp up and down at 2 (high - low) f V/s) or square "
            "(EN_AIN -> EN_OUT propagation delay; no thresholds)"
        ),
    )
    frequency: float = Field(1e3, gt=0, description="Wave frequency (Hz)")
    low: float = Field(0.0, ge=-0.3, description="Wave minimum (V)")
    high: float | None = Field(None, gt=0, description="Wave maximum (V); None = vin (EN_VDD)")
    cycles: float = Field(4.0, gt=0.5, description="Wave cycles per capture")
    n_samples: int = Field(8192, gt=64, description="Samples per capture (sample rate follows from cycles)")
    captures: int = Field(
        8, ge=1, description="Separate captures; thresholds are reported as mean and spread"
    )
    wavegen_channel: Literal[1, 2] = Field(1, description="AD2 W1/W2 -> EN_FG SMA (J2)")
    timeout: float = Field(
        5.0,
        gt=0,
        description="Max wait for one AD2 trigger (s), on top of one capture window (cycles / frequency)",
    )
    output: Literal["en_out", "vout"] = Field(
        "en_out",
        description=(
            "What scope 1 watches: EN_OUT (J8 pin 2, the block alone), or VOUT (OUTA, J8 on 'LDO': the "
            "LDO following EN_AIN through the whole enable path; use a slow triangle, e.g. 0.1 Hz)"
        ),
    )

    @property
    def sine_high(self) -> float:
        """Wave maximum actually used (V): `high`, or `vin` if not set."""
        return self.vin if self.high is None else self.high

    @model_validator(mode="after")
    def _check(self) -> EnSineConfig:
        if self.sine_high <= self.low:
            raise ValueError("en_sine.high must be above en_sine.low")
        if self.output == "vout" and self.shape == "square":
            raise ValueError(
                "en_sine: VOUT's timing after an enable edge is enable-startup's job, not a square here"
            )
        return self


EN_CURRENT_MAX_FREQUENCY = 1e6  # Hz: enable-current's top frequency (the AD2's square still has sharp edges)


class EnCurrentConfig(_Model):
    """[en_current] (enable-current): the enable block's EN_VDD current against the frequency of a
    square on EN_AIN, from the DMM in place of J1 9-11.

    Each input cycle moves a fixed charge through the block (its nodes charging, shoot-through at
    each threshold crossing), so its average current is about I_static + Q f: a DC current the DMM
    can read even though it can't catch each crossing's spike.
    """

    vin: float = Field(gt=0, description="PSU on VIN (feeds EN_VDD through the DMM) (V)")
    fstart: float = Field(10.0, gt=0, description="Lowest square frequency (Hz)")
    fstop: float = Field(
        EN_CURRENT_MAX_FREQUENCY, gt=0, description="Highest square frequency (Hz), at most 1 MHz"
    )
    low: float = Field(0.0, ge=-0.3, description="Square low level (V)")
    high: float | None = Field(None, gt=0, description="Square high level (V); None = vin (EN_VDD)")
    readings: int = Field(5, ge=3, description="DMM readings averaged per point (and for the zero)")
    settle_time: float = Field(0.5, ge=0, description="Wait after each frequency change (s)")
    dmm_range: float = Field(0.01, gt=0, description="DMM DC amps range (A); fixed, never autorange")
    nplc: Literal[0.02, 0.2, 1, 10, 100] | None = Field(None, description="None = dmm.nplc")
    wavegen_channel: Literal[1, 2] = Field(1, description="AD2 W1/W2 -> EN_FG SMA (J2)")
    swing_tolerance: float = Field(
        0.3, gt=0, lt=1, description="EN_AIN's measured swing must be within this fraction of the square's"
    )

    @property
    def square_high(self) -> float:
        """Square high level actually used (V): `high`, or `vin` if not set."""
        return self.vin if self.high is None else self.high

    @property
    def frequencies(self) -> list[float]:
        """The sweep (Hz): 1, 2, 5 per decade from fstart up to fstop, with fstop itself last."""
        out = []
        decade = 10.0 ** math.floor(math.log10(self.fstart))
        while decade <= self.fstop:
            for m in (1, 2, 5):
                f = float(m * decade)
                if self.fstart * (1 - 1e-9) <= f < self.fstop * (1 - 1e-9):
                    out.append(f)
            decade *= 10
        return [*out, float(self.fstop)]

    @model_validator(mode="after")
    def _check(self) -> EnCurrentConfig:
        if self.fstop > EN_CURRENT_MAX_FREQUENCY:
            raise ValueError(f"en_current.fstop ({self.fstop:g} Hz) is above 1 MHz")
        if self.fstop <= self.fstart:
            raise ValueError("en_current.fstop must be above fstart")
        if self.square_high <= self.low:
            raise ValueError("en_current.high must be above en_current.low")
        return self


class StartupConfig(_Model):
    """[startup] (enable-startup): step the enable line; VOUT and EN_IN on the AD2, the input current on the 54621A.

    The 54621A's channels are ground-referenced (its probe grounds and the AD2's are common), so the
    voltage across R13 is ch INA - ch INB, both on the same fine range centred on VIN, triggered on
    EN_AIN through its Ext Trig input. No scope goes on VREF: a scope's input loads the BGR and pulls
    VREF (and VOUT, which follows it) down.
    """

    vin: float = Field(gt=0, description="PSU voltage on VIN (V)")
    load_code: int = Field(0, ge=0, description="Load during the edges (0 = none)")
    edges: list[Literal["rising", "falling"]] = Field(
        default_factory=lambda: ["rising", "falling"], description="rising = enable, falling = disable"
    )
    window: float = Field(1e-3, gt=0, description="Capture length (s)")
    n_samples: int = Field(8192, gt=64, description="Samples per capture; sample rate = n_samples / window")
    pre_trigger: float = Field(0.1, gt=0, lt=1, description="Fraction of the capture before the edge")
    averages: int = Field(8, ge=1, description="Captures averaged per edge")
    off_time: float = Field(0.05, ge=0, description="Time disabled before each enable edge (s)")
    on_time: float = Field(0.05, ge=0, description="Time enabled before each disable edge (s)")
    en_in_range: float = Field(5.0, gt=0, description="AD2 peak-to-peak range on EN_IN (V)")
    sense_ohm: float = Field(0.1, gt=0, description="R13 value (ohm)")
    xscope_ina_channel: Literal[1, 2] = Field(1, description="54621A channel on INA (the supply side of R13)")
    xscope_inb_channel: Literal[1, 2] = Field(2, description="54621A channel on INB (the LDO side of R13)")
    xscope_shunt_range: float = Field(
        0.16,
        gt=0,
        description=(
            "54621A full-scale range on INA and INB (V), both centred on vin: 0.16 = 20 mV/div. Smaller "
            "resolves less current but clips sooner if VIN sags"
        ),
    )
    xscope_trigger_high: float = Field(
        1.8, gt=0, description="Logic-high on EN_AIN (V); the 54621A triggers on its Ext Trig at half of it"
    )
    settle_fraction: float = Field(
        0.02, gt=0, lt=1, description="Settled = within this fraction of the final level"
    )
    timeout: float = Field(5.0, gt=0, description="Max wait for one AD2 trigger (s)")

    @model_validator(mode="before")
    @classmethod
    def _removed(cls, data):
        # keys from the earlier layouts: say what replaced them instead of only "extra inputs not permitted"
        if isinstance(data, dict):
            for old in ("ch2", "ch2_range", "vref_nominal", "channels", "sense_range", "xscope_vout_range"):
                if old in data:
                    raise ValueError(
                        f"startup.{old} was removed (2026-11-14): VOUT and EN_IN are on the AD2 and the input "
                        "current is the 54621A's INA - INB (xscope_shunt_range, xscope_ina_channel / "
                        "xscope_inb_channel). VREF is no longer probed (a scope on it loads the BGR)"
                    )
        return data

    @model_validator(mode="after")
    def _check(self) -> StartupConfig:
        if self.xscope_ina_channel == self.xscope_inb_channel:
            raise ValueError("startup.xscope_inb_channel must differ from xscope_ina_channel")
        return self


# Supply rail the DMM measures in iq 'series' mode: 'total' = between PSU and VIN_DC; the others are
# JAFWAK J1 jumpers (bgr 1-3, vdd 5-7, ota 13-15). EN_VDD (9-11) isn't one: its ~0.3 nA static current
# is under the DMM's 10 nA count, so enable-current measures it instead.
IqRail = Literal["total", "bgr", "vdd", "ota"]


class IqConfig(_Model):
    """[iq] (iq): quiescent (enabled) and shutdown (disabled) supply current vs VIN, no load, from the DMM.

    method 'shunt': DMM DC volts across the input sense resistor (JAFWAK R13, J13.1-J13.2); measures
    everything drawn from VIN. 'series': DMM DC amps in place of a J1 jumper (one rail), or between the
    PSU and VIN_DC (rail 'total').
    """

    method: Literal["shunt", "series"] = Field(
        "shunt", description="How the DMM measures the current (above)"
    )
    rail: IqRail = Field("total", description="series only: which J1 rail the DMM replaces")
    sense_ohm: float = Field(0.1, gt=0, description="shunt only: R13 value (ohm)")
    vin_start: float = Field(gt=0, description="First VIN of the sweep (V)")
    vin_stop: float = Field(gt=0, description="Last VIN of the sweep (V); = vin_start for one point")
    vin_step: float = Field(0.1, gt=0, description="VIN step (V)")
    states: list[Literal["enabled", "disabled"]] = Field(
        default_factory=lambda: ["enabled", "disabled"],
        description="disabled = through the [enable] path; leave it out on a board with no enable control",
    )
    readings: int = Field(5, ge=3, description="DMM readings averaged per point (and for the zero)")
    settle_time: float = Field(0.5, ge=0, description="Wait after each VIN / enable change (s)")
    dmm_range: float | None = Field(
        None,
        gt=0,
        description="DMM range: V for shunt (default 0.1 = 100 mV), A for series (default 0.01 = 10 mA); never autorange",
    )
    nplc: Literal[0.02, 0.2, 1, 10, 100] | None = Field(None, description="None = dmm.nplc")
    check_code: int = Field(
        1,
        ge=0,
        description=(
            "shunt only: load code switched in once to prove the DMM sees the current through the sense "
            "resistor (0 = skip)"
        ),
    )
    check_tolerance: float = Field(
        0.15, gt=0, lt=1, description="shunt only: allowed relative error of the check_code current"
    )

    @model_validator(mode="after")
    def _check(self) -> IqConfig:
        if self.vin_stop < self.vin_start:
            raise ValueError("iq.vin_stop must be at or above vin_start")
        if self.method == "shunt" and self.rail != "total":
            raise ValueError("iq.method 'shunt' measures the total input current: use rail = 'total'")
        if not self.states:
            raise ValueError("iq.states is empty")
        return self


class BufferConfig(_Model):
    """[buffer]: the reworked injection buffer (JAFWAK U2) on a split supply, for line-tran and psrr.

    After the rework U2's V- (pin 4) is on a negative rail, so its output can reach 0 V and the
    1.7-1.9 V VIN range. The PSU's other output, wired reversed (OUT+ to GND, OUT- to U2 pin 4),
    supplies it; psu.channel stays on VS_BUFF (J4). With this section the buffer tests:
      - drive U2's input (W1) at DC 0 V before either rail comes up and never turn it off while
        they are up (a floating input sends U2's output to a rail),
      - ramp V- and VS_BUFF together in `ramp_step` steps, reading VIN after each one; outside
        vin_window the rails come down and the run stops,
      - shut down with W1 at 0 V until both rails are off.
    """

    v_minus: float = Field(
        5.0, gt=0, le=8.0, description="Magnitude of U2's negative rail (V): pin 4 sits at -v_minus"
    )
    channel: Literal[1, 2] = Field(
        2, description="PSU output wired reversed onto U2 pin 4 (OUT+ -> GND, OUT- -> pin 4)"
    )
    current_limit: float = Field(0.02, gt=0, description="Current limit on the negative rail (A)")
    ovp: float | None = Field(6.0, gt=0, description="OVP on the negative rail's output (V); None = none")
    rail_current_limit: float = Field(
        0.1,
        gt=0,
        description="Current limit on VS_BUFF (psu.channel) while the buffer runs (A); replaces psu.current_limit",
    )
    ramp_step: float = Field(0.1, gt=0, le=0.5, description="Step for ramping the two rails together (V)")
    vin_window: tuple[float, float] = Field(
        (-0.1, 0.3),
        description=(
            "VIN (V) allowed while the rails ramp with W1 at 0 V; U2's offset is up to ~65 mV (datasheet, "
            "first bring-up plan). Outside it the rails come down and the run stops"
        ),
    )

    @model_validator(mode="after")
    def _check(self) -> BufferConfig:
        lo, hi = self.vin_window
        if not lo < 0 < hi:
            raise ValueError("buffer.vin_window must contain 0 V (W1 sits at 0 V while the rails ramp)")
        return self


class BufferProofConfig(_Model):
    """Settings for proving the buffer path before the rail goes above dut.vin_max (line-tran, psrr)."""

    proof_levels: tuple[float, float] = Field(
        (0.1, 0.3),
        description=(
            "Two W1 levels (V) played with the buffer rail held at dut.vin_max: VIN must follow them "
            "before the rail is raised further. They must be within what the BUF634 can drive at that rail"
        ),
    )

    @model_validator(mode="after")
    def _proof_levels(self):
        lo, hi = self.proof_levels
        if not 0 <= lo < hi <= AD2_WAVEGEN_MAX_V:
            raise ValueError("proof_levels must be two rising levels within 0..5 V")
        return self


class PsrrConfig(BufferProofConfig):
    """[psrr] (psrr): a sine on VIN through the BUF634 (line transient path), its amplitude on VIN and VOUT.

    The PSU powers the buffer rail, not VIN; the wavegen sets VIN's DC level and the sine on it.
    PSRR = VOUT / VIN amplitude from a sine fit at each frequency (analysis.sine_fit).
    """

    psu_voltage: float = Field(gt=0, description="PSU on VS_BUFF / VDD_BUF (buffer supply), not VIN (V)")
    vin: float = Field(gt=0, description="DC level on VIN (V); the sine rides on it")
    amplitude: float = Field(0.05, gt=0, description="Sine peak amplitude on VIN (V)")
    f_start: float = Field(100.0, gt=0, description="First frequency (Hz)")
    f_stop: float = Field(1e6, gt=0, le=10e6, description="Last frequency (Hz)")
    points_per_decade: int = Field(3, ge=1, le=50, description="Log-spaced frequencies per decade")
    load_code: int = Field(0, ge=0, description="Load bank code during the sweep")
    averages: int = Field(4, ge=1, description="Captures per frequency (VOUT/VIN is averaged coherently)")
    cycles: float = Field(20.0, ge=2, description="Minimum sine cycles per capture")
    n_samples: int = Field(8192, ge=512, description="Samples per capture")
    max_sample_rate: float = Field(100e6, gt=0, le=100e6, description="AD2 sample rate ceiling (Hz)")
    settle_cycles: float = Field(20.0, ge=0, description="Wait this many cycles after changing frequency")
    level_tolerance: float = Field(
        0.15, gt=0, lt=1, description="Re-level W1 if VIN's amplitude is off by more than this fraction"
    )
    buffer_headroom: float = Field(
        2.0, ge=0, description="Required psu_voltage - VIN peak margin for the BUF634 (V)"
    )
    timeout: float = Field(10.0, gt=0, description="Max wait for one AD2 trigger (s)")
    meter: Literal["ad2", "sa"] = Field(
        "ad2",
        description="VOUT from the AD2's sine fit, or (sa) from the HP 8591E through U4 / J9 from sa_from up",
    )
    sa_from: float = Field(
        30e3,
        ge=9e3,
        description="meter = sa: the 8591E reads VOUT from here up (Hz). Below ~30 kHz its LO feedthrough near "
        "0 Hz swamps VOUT's tens-of-uV tones (bench 2026-11-20); the AD2 measures VOUT there",
    )
    sa_cal_to: float = Field(
        100e3,
        gt=0,
        description="meter = sa: from sa_from up to here (Hz) the sa-path-cal curve is checked against the AD2's "
        "VOUT, where both meters read it sa_cal_margin_db above their floors",
    )
    sa_cal_averages: int = Field(
        128,
        ge=1,
        description="meter = sa: AD2 captures per point from sa_from to sa_cal_to, so its VOUT clears the margin "
        "for the calibration check (VOUT is only ~50-150 uV there, bench 2026-11-20)",
    )
    sa_cal_margin_db: float = Field(
        20.0,
        gt=0,
        description="meter = sa: a calibration check point needs both readings this far above their floors (dB)",
    )
    sa_path_tolerance_db: float = Field(
        1.0,
        gt=0,
        description="meter = sa: where both meters read VOUT they must agree within this (dB) with the sa-path-cal "
        "curve applied, else the curve doesn't describe today's path and the analyzer's points are invalid",
    )

    @model_validator(mode="after")
    def _check(self) -> PsrrConfig:
        if self.meter == "sa" and self.sa_cal_to <= self.sa_from:
            raise ValueError("psrr.sa_cal_to must be above sa_from (the path gain needs an overlap band)")
        # The sine must stay positive, inside the wavegen's range, and below the buffer rail minus
        # the BUF634's headroom (else the buffer clips the peaks).
        if self.f_stop <= self.f_start:
            raise ValueError("psrr.f_stop must be above f_start")
        if self.vin - self.amplitude <= 0:
            raise ValueError("psrr.vin - amplitude must stay above 0 V")
        if self.vin + self.amplitude > AD2_WAVEGEN_MAX_V:
            raise ValueError(f"psrr.vin + amplitude exceeds the AD2 wavegen limit ({AD2_WAVEGEN_MAX_V} V)")
        if self.psu_voltage < self.vin + self.amplitude + self.buffer_headroom:
            raise ValueError(
                f"psrr.psu_voltage ({self.psu_voltage} V) must be at least vin + amplitude + "
                f"buffer_headroom ({self.vin + self.amplitude + self.buffer_headroom} V)"
            )
        return self

    def frequencies(self) -> list[float]:
        """Log-spaced sweep from f_start to f_stop (Hz), both included, rounded to 3 significant digits."""
        import math

        decades = math.log10(self.f_stop / self.f_start)
        n = max(2, round(decades * self.points_per_decade) + 1)
        out = []
        for k in range(n):
            f = self.f_start * 10 ** (decades * k / (n - 1))
            out.append(float(f"{f:.3g}"))
        return out


class LineTranConfig(TransientCaptureConfig, BufferProofConfig):
    """[line_tran] (line-tran): W1 steps VIN between two levels through the BUF634; PSU on the buffer rail."""

    psu_voltage: float = Field(gt=0, description="PSU voltage on VS_BUFF (buffer supply) (V)")
    vin_low: float = Field(ge=0, description="W1 low level -> VIN through the BUF634 (V)")
    vin_high: float = Field(gt=0, description="W1 high level -> VIN through the BUF634 (V)")
    load_code: int = Field(0, ge=0, description="Load bank code during the steps")
    buffer_headroom: float = Field(2.0, ge=0, description="Required VS_BUFF - Vin margin for the BUF634 (V)")
    w1_max: float | None = Field(
        None,
        gt=0,
        description=(
            "Highest W1 setpoint (V) into the buffer during line-tran only; None = dut.vin_max. For a buffer "
            "whose output sits below W1 under load (U2: ~5 ohm x the LDO's input current). VIN itself is "
            "still stopped at dut.vin_max less the AD2 margin on every reading. dut.vin_max .. +0.2 V"
        ),
    )
    settle_window: float | None = Field(
        20e-3,
        gt=0,
        description=(
            "Length (s) of each edge's second, long capture, for how long VOUT takes to flatten at its new "
            "level (plot_full.png). None = no long capture. Same n_samples, so the sample rate is "
            "n_samples / settle_window"
        ),
    )
    square_hz: float | None = Field(
        20.0,
        gt=0,
        description=(
            "W1 runs as a free-running square (Hz) between the two calibrated setpoints, and each capture "
            "takes its next edge. None = one DC step per capture: the AD2's DC level moves with tau ~2 ms "
            "(bench, 2026-11-18/19), its square in ~0.2-0.35 ms. Half a period must be longer than the "
            "capture window (n_samples / sample_rate)"
        ),
    )

    @model_validator(mode="after")
    def _check(self) -> LineTranConfig:
        if self.vin_high <= self.vin_low:
            raise ValueError("line_tran.vin_high must be above vin_low")
        window = self.n_samples / self.sample_rate
        if (
            self.square_hz is not None
            and self.settle_window is not None
            and 1 / (2 * self.square_hz) <= self.settle_window
        ):
            raise ValueError(
                f"line_tran.settle_window ({self.settle_window * 1e3:.1f} ms) must be shorter than half a period "
                f"of square_hz ({1e3 / (2 * self.square_hz):.1f} ms), or the long capture sees the next edge too"
            )
        if self.square_hz is not None and 1 / (2 * self.square_hz) <= window:
            raise ValueError(
                f"line_tran.square_hz ({self.square_hz:g} Hz): half a period "
                f"({1e3 / (2 * self.square_hz):.1f} ms) must be longer than the capture window "
                f"({window * 1e3:.1f} ms), or a capture sees the next edge too. Lower square_hz or the window"
            )
        if self.vin_high > AD2_WAVEGEN_MAX_V:
            raise ValueError(f"line_tran.vin_high exceeds the AD2 wavegen limit ({AD2_WAVEGEN_MAX_V} V)")
        if self.psu_voltage < self.vin_high + self.buffer_headroom:
            raise ValueError(
                f"line_tran.psu_voltage ({self.psu_voltage} V) must be at least vin_high + buffer_headroom "
                f"({self.vin_high + self.buffer_headroom} V) or the BUF634 will clip"
            )
        return self


class SplitRailsConfig(_Model):
    """[split_rails]: dropout "Method B" (line-reg --split-rails, bring-up plan Test 7).

    BGR_VDD and OTA_VDD come off INB (their J1 jumpers removed, 1-3 and 13-15) and onto the PSU's
    other output at a fixed `voltage`, while VDD and EN_VDD stay on VIN through J1 5-7 / 9-11. As
    VIN sweeps down, only the pass device drops out, so line-reg's knee is the pass device's own.
    VIN is up before the rails and stays at or above vin_floor while they are, so the BGR / OTA
    domains are never powered without VDD (only the VDD ring has ESD clamps).
    """

    psu_channel: int = Field(2, ge=1, le=2, description="PSU output on BGR_VDD / OTA_VDD (not psu.channel)")
    voltage: float = Field(1.8, gt=0, description="BGR_VDD / OTA_VDD during the sweep (V)")
    v_max: float = Field(gt=0, description="The rails are never programmed above this (V)")
    current_limit: float = Field(
        0.005, gt=0, description="Current limit on that output (A); BGR + OTA draw ~0.03 mA"
    )
    ovp: float | None = Field(None, gt=0, description="OVP on that output (V), above v_max")
    max_current: float = Field(
        1e-3,
        gt=0,
        description=(
            "More than this (A) from the output once it is up means a J1 1-3 / 13-15 jumper is still "
            "fitted (the output fighting VIN through INB): the rails come down and the run stops"
        ),
    )
    vin_floor: float = Field(1.0, gt=0, description="VIN is never set below this (V) while the rails are up")
    settle_time: float = Field(0.3, ge=0, description="Wait after the rails reach `voltage` (s)")

    @model_validator(mode="after")
    def _check(self) -> SplitRailsConfig:
        if self.voltage > self.v_max:
            raise ValueError("split_rails.voltage is above split_rails.v_max")
        if self.ovp is not None and self.ovp <= self.v_max:
            raise ValueError("split_rails.ovp must be above split_rails.v_max")
        return self


class VrefConfig(_Model):
    """[vref]: an external reference on VREF_IN in place of the on-chip BGR (`--vref ext`).

    On JAFWAK, JP2 joins VREF_OUT (the BGR) to VREF_IN (what the regulator follows). For an
    external-reference run JP2 is removed and the PSU's other output drives VREF_IN, so a test can
    be repeated with the reference at its simulated value and compared against the BGR run
    (`ldo-bench compare`). Without `--vref ext` this section changes nothing.

    The BGR must not be back-driven, and no reading can tell whether JP2 is out before the source
    is connected, so a person confirms it every run (--yes doesn't skip that question). After the
    reference turns on, the Bench steps it by `proof_step` and stops unless VOUT follows: with JP2
    still fitted, the BGR would hold VOUT still.
    """

    external_v: float = Field(
        1.204, gt=0, description="Reference applied to VREF_IN (V); default = the BGR's simulated output"
    )
    bgr_measured: float | None = Field(
        None, gt=0, description="The BGR's measured output (V), for the record only (e.g. 1.17)"
    )
    psu_channel: int = Field(2, ge=1, le=2, description="PSU output wired to VREF_IN (not psu.channel)")
    v_max: float = Field(gt=0, description="VREF_IN is never programmed above this (V)")
    current_limit: float = Field(
        0.005, gt=0, description="Current limit on the reference output (A); VREF_IN draws almost none"
    )
    ovp: float | None = Field(None, gt=0, description="OVP on the reference output (V), above v_max")
    above_vin: float = Field(
        0.3,
        ge=0,
        description=(
            "VREF may sit at most this far above VIN (V): the usual input clamp limit, so the reference "
            "never drives the chip through its input protection while VIN is lower"
        ),
    )
    proof_step: float = Field(0.02, gt=0, description="Step used to prove VOUT follows the reference (V)")
    proof_follow: float = Field(
        0.5, gt=0, lt=1, description="VOUT must move by at least this fraction of proof_step"
    )
    settle_time: float = Field(0.3, ge=0, description="Wait after each reference change (s)")

    @model_validator(mode="after")
    def _check(self) -> VrefConfig:
        if self.external_v > self.v_max:
            raise ValueError(f"vref.external_v ({self.external_v} V) exceeds vref.v_max ({self.v_max} V)")
        if self.ovp is not None and self.ovp <= self.v_max:
            raise ValueError(f"vref.ovp ({self.ovp} V) must be above vref.v_max ({self.v_max} V)")
        if self.proof_step >= self.external_v:
            raise ValueError("vref.proof_step must be below external_v")
        return self


class BenchConfig(_Model):
    """The whole TOML file. [dut], [psu] and [loadbank] are required; a test's section is required
    only to run that test (the CLI says so if it's missing)."""

    dut: DutConfig
    psu: PsuConfig
    ad2: Ad2Config = Ad2Config()
    dmm: DmmConfig | None = None
    xscope: XScopeConfig | None = None
    sa: SaConfig | None = None
    loadbank: LoadBankConfig
    analysis: AnalysisConfig = AnalysisConfig()
    psu_check: PsuCheckConfig = PsuCheckConfig()
    loadbank_check: LoadBankCheckConfig = LoadBankCheckConfig()
    smoke: SmokeConfig = SmokeConfig()
    psu_hold: PsuHoldConfig | None = None
    psu_sweep: PsuSweepConfig | None = None
    load_reg: LoadRegConfig | None = None
    line_reg: LineRegConfig | None = None
    vref_line: VrefLineConfig | None = None
    temp: TempConfig | None = None
    load_tran: LoadTranConfig | None = None
    line_tran: LineTranConfig | None = None
    enable: EnableConfig = EnableConfig()
    board: BoardConfig = BoardConfig()
    en_sine: EnSineConfig | None = None
    en_current: EnCurrentConfig | None = None
    startup: StartupConfig | None = None
    iq: IqConfig | None = None
    psrr: PsrrConfig | None = None
    vref: VrefConfig | None = None
    buffer: BufferConfig | None = None
    split_rails: SplitRailsConfig | None = None

    def enable_pin_clash(self) -> set[int]:
        """Enable DIO pins that are also load bank pins (they would fight); empty if none."""
        return self.enable.pins & set(self.loadbank.dio_pins)

    def psu_limit_for(self, section) -> float:
        """The supply current limit (A) a test runs with: psu.high_current_limit if its section has
        high_current = true, else psu.current_limit. ValueError if it asks for a high limit there isn't."""
        if getattr(section, "high_current", False):
            if self.psu.high_current_limit is None:
                raise ValueError("high_current = true needs psu.high_current_limit")
            return self.psu.high_current_limit
        return self.psu.current_limit

    def _load_current(self, code: int) -> float:
        """Estimated load bank current (A) for `code` at vout_nominal (what LoadBank.current gives)."""
        return self.dut.vout_nominal * self.loadbank.conductance(code)

    def heaviest_allowed_code(self) -> int:
        """The code within dut.i_max that draws the most current (what smoke and load-reg's [] use)."""
        codes = [
            c
            for c in range(self.loadbank.max_code + 1)
            if self.dut.i_max is None or self._load_current(c) <= self.dut.i_max
        ]
        return max(codes, key=self._load_current)

    def supply_loads(self) -> dict[str, tuple[object, float]]:
        """The heaviest load (A) each configured test puts on psu.channel, with that test's section.

        Estimated from the load bank's resistors (at vout_nominal, or at loadbank_check.voltage for the
        bank straight on the supply); the LDO's own few hundred uA aren't included. line-tran and psrr
        aren't here: they power the buffer rail with buffer.rail_current_limit.
        """
        out: dict[str, tuple[object, float]] = {}
        lc = self.loadbank_check
        r_min = min(self.loadbank.resistors) + self.loadbank.switch_resistance
        out["loadbank_check"] = (lc, lc.voltage / r_min)  # one bit at a time; the smallest resistor
        out["smoke"] = (self.smoke, self._load_current(self.heaviest_allowed_code()))
        if self.load_reg:
            codes = self.load_reg.codes or [self.heaviest_allowed_code()]
            out["load_reg"] = (self.load_reg, max(self._load_current(c) for c in codes))
        if self.load_tran:
            lt = self.load_tran
            out["load_tran"] = (lt, max(self._load_current(lt.code_low), self._load_current(lt.code_high)))
        for name in ("line_reg", "temp", "vref_line", "startup"):
            if (sec := getattr(self, name)) is not None:
                out[name] = (sec, self._load_current(sec.load_code))
        if self.iq:
            out["iq"] = (self.iq, self._load_current(self.iq.check_code))
        return out

    def lowest_vin(self) -> dict[str, float]:
        """The lowest VIN (V) each configured test puts on the LDO while it runs, by test name.

        Used for the external reference: VREF must stay within vref.above_vin of VIN throughout.
        (enable-sine and enable-startup aren't run with an external reference.)
        """
        out: dict[str, float] = {}
        if self.load_reg:
            out["load-reg"] = self.load_reg.vin
        if self.line_reg:
            out["line-reg"] = self.line_reg.vin_start  # the sweep goes up
        if self.load_tran:
            out["load-tran"] = self.load_tran.vin
        if self.line_tran:
            out["line-tran"] = self.line_tran.vin_low
        if self.iq:
            out["iq"] = self.iq.vin_start
        if self.psrr:
            out["psrr"] = self.psrr.vin - self.psrr.amplitude
        return out

    @model_validator(mode="after")
    def _cross_checks(self) -> BenchConfig:
        """Limits that span sections, checked before any instrument is opened.

        - dut.vin_max: the DUT's absolute max VIN. Every test level that reaches VIN, from the PSU
          or the wavegen (including a sine's peak), must be at or below it.
        - psu.v_max: the PSU is never programmed above this. When the PSU drives VIN directly, the
          lower of the two ceilings applies; when it powers the BUF634 rail instead (line_tran,
          psrr), only psu.v_max applies, since VIN then comes from the wavegen.
        - dut.i_max: every load code a test uses must draw at most this at vout_nominal
          (estimated from the resistor values, like LoadBank.current), so a config can't ask for a
          load the DUT can't supply.
        - buffer headroom (in LineTranConfig / PsrrConfig): the buffer rail must sit at least
          buffer_headroom above VIN's peak, or the BUF634 clips and the step / sine on VIN is wrong.
        - vref (only if [vref] is present): its own PSU output, v_max within dut.vin_max, and the
          external reference within vref.above_vin of every test's lowest VIN.
        - psu current limit: every test's heaviest load (supply_loads) below the limit it runs with
          (psu_limit_for: psu.high_current_limit with high_current = true), so no test is configured
          to put the supply into current limit. psu_hold / psu_sweep: current_limit at most
          psu.current_limit, and their voltages within the ceiling for where the supply goes (`to`).
        - en_sine / en_current: the wave into EN_AIN within dut.vin_max (EN_AIN runs from VIN).
        - temp VS_BUFF: temp.vs_buff_v within psu.v_max, on the PSU's other output (not psu.channel).
        - enable-pin clashes: tests that drive the enable line (en_sine, startup, iq 'disabled')
          would fight the load bank if an enable DIO pin were also a load bank pin.
        """
        # When the PSU drives VIN directly, both ceilings apply.
        vin_ceiling = min(self.dut.vin_max, self.psu.v_max)
        max_code = self.loadbank.max_code

        lb = self.loadbank
        if self.board.bank_bits and len(self.board.bank_bits) != len(lb.dio_pins):
            raise ValueError(
                f"board.bank_bits has {len(self.board.bank_bits)} entries; loadbank.dio_pins has {len(lb.dio_pins)}"
            )

        def code_ok(name: str, code: int) -> None:
            if not 0 <= code <= max_code:
                raise ValueError(f"{name} = {code} is outside the load bank range 0..{max_code}")
            # the bank alone: the external resistor (--ext-load-ohm) is checked against dut.i_max per
            # run, for the codes that run uses (LoadBank.require), before anything powers up
            if self.dut.i_max is not None:
                i = self.dut.vout_nominal * lb.bank_conductance(code)
                if i > self.dut.i_max:
                    raise ValueError(
                        f"{name} = {code} draws ~{i * 1e3:.1f} mA at {self.dut.vout_nominal} V, above "
                        f"dut.i_max ({self.dut.i_max * 1e3:.1f} mA)"
                    )

        def vin_ok(name: str, v: float) -> None:
            if v > vin_ceiling:
                raise ValueError(f"{name} = {v} V exceeds dut.vin_max / psu.v_max ({vin_ceiling} V)")

        # psu-check has no DUT on the PSU, so only psu.v_max applies. psu_check always exists
        # (defaults), so this runs for every config.
        over = [v for v in self.psu_check.points if v > self.psu.v_max or v < 0]
        if over:
            raise ValueError(f"psu_check.points {over} are outside 0..psu.v_max ({self.psu.v_max} V)")
        if self.load_reg:
            vin_ok("load_reg.vin", self.load_reg.vin)
            for c in self.load_reg.codes:
                code_ok("load_reg.codes", c)
        if self.line_reg:
            lr = self.line_reg
            vin_ok("line_reg sweep", lr.vin_stop)  # vin_stop > vin_start (LineRegConfig)
            code_ok("line_reg.load_code", lr.load_code)
        if self.temp:
            vin_ok("temp.vin", self.temp.vin)
            code_ok("temp.load_code", self.temp.load_code)
            if self.temp.vs_buff_v > self.psu.v_max:
                raise ValueError(
                    f"temp.vs_buff_v = {self.temp.vs_buff_v} V exceeds psu.v_max ({self.psu.v_max} V)"
                )
            if self.temp.vs_buff_channel == self.psu.channel:
                raise ValueError("temp.vs_buff_channel must be the PSU's other output, not psu.channel (VIN)")
        if self.vref_line:
            vl = self.vref_line
            vin_ok("vref_line.vin_high", vl.vin_high)
            code_ok("vref_line.load_code", vl.load_code)
        if self.load_tran:
            lt = self.load_tran
            vin_ok("load_tran.vin", lt.vin)
            code_ok("load_tran.code_low", lt.code_low)
            code_ok("load_tran.code_high", lt.code_high)
            if lt.code_low >= lt.code_high:
                raise ValueError("load_tran.code_low must be below code_high")
        clash = self.enable_pin_clash()
        iq_enable = self.iq is not None and "disabled" in self.iq.states
        if (self.en_sine or self.startup or iq_enable) and clash:
            raise ValueError(
                f"enable DIO pins {sorted(clash)} are also load bank pins {self.loadbank.dio_pins}"
            )
        if self.en_sine:
            es = self.en_sine
            vin_ok("en_sine.vin", es.vin)
            # The sine goes into EN_AIN, which runs from VIN: keep it within the DUT's VIN rating.
            if es.sine_high > self.dut.vin_max:
                raise ValueError(
                    f"en_sine.high ({es.sine_high} V) exceeds dut.vin_max ({self.dut.vin_max} V)"
                )
        if self.en_current:
            ec = self.en_current
            vin_ok("en_current.vin", ec.vin)
            if ec.square_high > self.dut.vin_max:  # into EN_AIN, which runs from VIN
                raise ValueError(
                    f"en_current.high ({ec.square_high} V) exceeds dut.vin_max ({self.dut.vin_max} V)"
                )
        if self.startup:
            vin_ok("startup.vin", self.startup.vin)
            code_ok("startup.load_code", self.startup.load_code)
        if self.iq:
            vin_ok("iq sweep", self.iq.vin_stop)  # vin_stop >= vin_start (IqConfig)
        if self.split_rails:
            sr = self.split_rails
            if sr.v_max > vin_ceiling:
                raise ValueError(
                    f"split_rails.v_max ({sr.v_max} V) exceeds dut.vin_max / psu.v_max ({vin_ceiling} V)"
                )
            if sr.psu_channel == self.psu.channel:
                raise ValueError("split_rails.psu_channel must be the PSU's other output, not psu.channel")
            code_ok("iq.check_code", self.iq.check_code)
        if self.psrr:
            ps = self.psrr
            if ps.meter == "sa" and (self.sa is None or not self.sa.enabled):
                raise ValueError("psrr.meter = 'sa' needs an enabled [sa] section (the HP 8591E)")
            # As in line_tran: the PSU feeds the buffer rail; VIN (dc + sine) comes from the wavegen.
            if ps.psu_voltage > self.psu.v_max:
                raise ValueError(f"psrr.psu_voltage exceeds psu.v_max ({self.psu.v_max} V)")
            if ps.vin + ps.amplitude > self.dut.vin_max:
                raise ValueError(
                    f"psrr.vin + amplitude ({ps.vin + ps.amplitude} V) exceeds dut.vin_max ({self.dut.vin_max} V)"
                )
            peak = ps.vin + ps.amplitude
            if peak + self.ad2.vin_margin(peak) > self.dut.vin_max:
                # The run measures VIN with the AD2; leave room for its error below the limit.
                raise ValueError(
                    f"psrr.vin + amplitude ({peak} V) is within the AD2's measurement error "
                    f"(~{self.ad2.vin_margin(peak) * 1e3:.0f} mV) of dut.vin_max ({self.dut.vin_max} V)"
                )
            code_ok("psrr.load_code", ps.load_code)
        if self.line_tran:
            lt = self.line_tran
            if lt.w1_max is not None and not (
                self.dut.vin_max
                <= lt.w1_max
                <= min(self.dut.vin_max + W1_MAX_ABOVE_VIN_MAX, AD2_WAVEGEN_MAX_V)
            ):
                raise ValueError(
                    f"line_tran.w1_max ({lt.w1_max} V) must be between dut.vin_max ({self.dut.vin_max} V) and "
                    f"dut.vin_max + {W1_MAX_ABOVE_VIN_MAX} V (and within the AD2's {AD2_WAVEGEN_MAX_V} V)"
                )
            # Here the PSU feeds the buffer rail; VIN comes from the wavegen through the buffer.
            if lt.psu_voltage > self.psu.v_max:
                raise ValueError(f"line_tran.psu_voltage exceeds psu.v_max ({self.psu.v_max} V)")
            if lt.vin_high > self.dut.vin_max:
                raise ValueError(f"line_tran.vin_high exceeds dut.vin_max ({self.dut.vin_max} V)")
            if lt.vin_high + self.ad2.vin_margin(lt.vin_high) > self.dut.vin_max:
                raise ValueError(
                    f"line_tran.vin_high ({lt.vin_high} V) is within the AD2's measurement error "
                    f"(~{self.ad2.vin_margin(lt.vin_high) * 1e3:.0f} mV) of dut.vin_max ({self.dut.vin_max} V)"
                )
            code_ok("line_tran.load_code", lt.load_code)
        if self.buffer:
            bu = self.buffer
            if bu.channel == self.psu.channel:
                raise ValueError(
                    f"buffer.channel ({bu.channel}) is psu.channel: U2's negative rail needs the other output"
                )
            for name, sec in (("line_tran", self.line_tran), ("psrr", self.psrr)):
                if sec is not None and bu.v_minus < sec.buffer_headroom:
                    raise ValueError(
                        f"buffer.v_minus ({bu.v_minus} V) is less than {name}.buffer_headroom "
                        f"({sec.buffer_headroom} V): U2 couldn't drive VIN down to 0 V"
                    )
        if self.psu.idle_voltage is not None and self.psu.idle_voltage > vin_ceiling:
            raise ValueError(
                f"psu.idle_voltage ({self.psu.idle_voltage} V) exceeds dut.vin_max / psu.v_max ({vin_ceiling} V): "
                "it is what the supply gives when its output is turned on by hand"
            )
        for name, (sec, amps) in self.supply_loads().items():
            limit = self.psu_limit_for(sec)
            if amps >= limit:
                high = getattr(sec, "high_current", False)
                which = "psu.high_current_limit" if high else "psu.current_limit"
                hint = (
                    f", or set high_current = true in [{name}]"
                    if hasattr(sec, "high_current") and not high
                    else ""
                )
                raise ValueError(
                    f"{name} draws ~{amps * 1e3:.1f} mA, at or above its current limit {which} "
                    f"({limit * 1e3:g} mA): use a lighter load{hint}"
                )
        for name in ("psu_hold", "psu_sweep"):
            sec = getattr(self, name)
            if sec is None:
                continue
            if sec.current_limit is not None and sec.current_limit > self.psu.current_limit:
                raise ValueError(
                    f"{name}.current_limit ({sec.current_limit * 1e3:g} mA) is above psu.current_limit "
                    f"({self.psu.current_limit * 1e3:g} mA)"
                )
            top = sec.voltage if name == "psu_hold" else max(sec.start, sec.stop)
            if sec.to == "vin":
                vin_ok(f"{name} voltage", top)
            elif top > self.psu.v_max:
                raise ValueError(f"{name} voltage {top} V exceeds psu.v_max ({self.psu.v_max} V)")
        if self.vref:
            vr = self.vref
            if vr.psu_channel == self.psu.channel:
                raise ValueError(
                    f"vref.psu_channel ({vr.psu_channel}) is psu.channel: the reference needs the other output"
                )
            if vr.v_max > self.dut.vin_max:
                raise ValueError(f"vref.v_max ({vr.v_max} V) exceeds dut.vin_max ({self.dut.vin_max} V)")
            for test, vin in self.lowest_vin().items():
                if vr.external_v > vin + vr.above_vin:
                    raise ValueError(
                        f"vref.external_v ({vr.external_v} V) is more than vref.above_vin ({vr.above_vin} V) "
                        f"above {test}'s lowest VIN ({vin} V)"
                    )
        return self


def load_config(path: Path) -> BenchConfig:
    """Read and validate a TOML config. Raises pydantic.ValidationError on any problem."""
    with open(path, "rb") as f:
        data = tomllib.load(f)
    return BenchConfig.model_validate(data)
