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

import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

# AD2 limits
AD2_DIO_PINS = 16  # DIO 0..15
AD2_WAVEGEN_MAX_V = 5.0  # highest wavegen output used for VIN (V)


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


class PsuConfig(_Model):
    """[psu]: the bench supply (Agilent E3648A / E36xxA): address, RS-232 settings and safety limits.

    current_limit and ovp are programmed before the output is turned on; v_max is the software
    ceiling the Bench checks before every voltage change.
    """

    resource: str = Field(
        "",
        description=(
            "'' = auto-discover over VISA (GPIB / USB), RS-232 ports and Prologix adapters. Or any of: "
            "'GPIB0::5::INSTR', 'USB0::...::INSTR' (UsbGpib), 'prologix:COM5:5', 'serial:COM3' / "
            "'serial:/dev/cu.usbserial-XXXX'"
        ),
    )
    gpib_address: int = Field(5, ge=0, le=30, description="Tried first when scanning a Prologix adapter")
    # RS-232 only (E36xxA: I/O config on the front panel); ignored on GPIB / USB
    baud: int = Field(9600, description="RS-232 baud rate (must match the supply)")
    parity: Literal["none", "even", "odd"] = Field("none", description="none = 8 data bits; even/odd = 7")
    stopbits: Literal[1, 2] = Field(2, description="RS-232 stop bits (must match the supply)")
    flow: FlowName = Field("dsr-pacing", description="HP DTR/DSR handshake: wait for DSR before each command")
    channel: int = Field(1, ge=1, le=2, description="Output to use on a dual-output supply (E3648A)")
    voltage_range: Literal["LOW", "HIGH"] | None = Field(None, description="Output range (LOW/HIGH)")
    current_limit: float = Field(gt=0, description="Applied before the output is enabled (A)")
    ovp: float | None = Field(None, gt=0, description="Over-voltage protection level (V)")
    v_max: float = Field(
        gt=0,
        description=(
            "The PSU is never programmed above this (V). Usually equals dut.vin_max; it is higher only "
            "when line_tran powers the buffer rail from the PSU."
        ),
    )
    ramp_step: float = Field(0.25, gt=0, description="Max voltage step when ramping (V)")
    settle_time: float = Field(0.3, ge=0, description="Wait after each PSU voltage change (s)")

    @model_validator(mode="after")
    def _ovp_above_v_max(self) -> PsuConfig:
        # OVP is the hardware backstop: if it sat at or below v_max, a legal setpoint could trip it.
        if self.ovp is not None and self.ovp <= self.v_max:
            raise ValueError(f"psu.ovp ({self.ovp} V) must be above psu.v_max ({self.v_max} V)")
        return self

    def serial_settings(self):
        """RS-232 settings to use if this instrument is on a serial port."""
        from ldo_bench.instruments.transports import SerialSettings

        return SerialSettings(self.baud, self.parity, self.stopbits, self.flow)


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


class DmmConfig(_Model):
    """[dmm]: bench DMM (Agilent 34401A) measuring VOUT in the DC tests (and current in iq / psu-check).

    Leave the section out, set enabled = false, or pass --no-dmm to measure VOUT with the AD2.
    """

    enabled: bool = Field(True, description="false = don't open the DMM (VOUT from the AD2)")
    resource: str = Field(
        "",
        description=(
            "'' = auto-discover over VISA (GPIB / USB), RS-232 ports and Prologix adapters. Or any of: "
            "'GPIB0::5::INSTR', 'USB0::...::INSTR' (UsbGpib), 'prologix:COM5:5', 'serial:COM3' / "
            "'serial:/dev/cu.usbserial-XXXX'"
        ),
    )
    # RS-232 only (34401A: Shift > I/O MENU); ignored on GPIB
    baud: int = Field(9600, description="Must match the 34401A's RS-232 baud rate setting")
    parity: Literal["none", "even", "odd"] = Field(
        "none", description="34401A PARITY setting: NONE: 8 BITS = 'none', EVEN: 7 BITS = 'even'"
    )
    stopbits: Literal[1, 2] = Field(2, description="RS-232 stop bits (must match the meter)")
    flow: FlowName = Field("dsr-pacing", description="RS-232 handshake; the 34401A paces with DSR")
    nplc: Literal[0.02, 0.2, 1, 10, 100] = Field(10, description="Integration time in power-line cycles")
    range: float | None = Field(None, gt=0, description="Fixed DCV range (V); None = auto range")
    samples: int = Field(1, ge=1, description="Readings averaged per point")

    def serial_settings(self):
        """RS-232 settings to use if this instrument is on a serial port."""
        from ldo_bench.instruments.transports import SerialSettings

        return SerialSettings(self.baud, self.parity, self.stopbits, self.flow)


class XScopeConfig(_Model):
    """[xscope]: Agilent 54621A on RS-232, capturing each transient edge alongside the AD2 as a cross-check.

    Optional: only load-tran and line-tran use it, and if it can't be opened the run goes ahead with
    the AD2 alone. --no-xscope skips it for one run.
    """

    enabled: bool = Field(True, description="false = don't use the cross-check scope")
    resource: str = Field(
        "",
        description=(
            "'' = auto-discover over VISA (GPIB / USB), RS-232 ports and Prologix adapters. Or any of: "
            "'GPIB0::5::INSTR', 'USB0::...::INSTR' (UsbGpib), 'prologix:COM5:5', 'serial:COM3' / "
            "'serial:/dev/cu.usbserial-XXXX'"
        ),
    )
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

    def serial_settings(self):
        """RS-232 settings to use if this instrument is on a serial port."""
        from ldo_bench.instruments.transports import SerialSettings

        return SerialSettings(self.baud, self.parity, self.stopbits, self.flow)


class LoadBankConfig(_Model):
    """[loadbank]: the binary-weighted load bank: which AD2 DIO drives each bit and its resistor.

    A load "code" is the binary number on the pins (bit 0 = first entry). The resistor values are
    used to estimate the load current (VOUT / R per set bit); nothing measures it directly.
    """

    dio_pins: list[int] = Field(description="AD2 DIO pin for each bank bit, LSB first")
    resistors: list[float] = Field(description="Load resistor for each bit, LSB first (ohm)")
    switch_resistance: float = Field(0.0, ge=0, description="Per-switch on-resistance (ohm)")
    active_high: bool = Field(True, description="true = DIO high switches the bit's resistor in")

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

    Each bit is switched in on its own and the PSU's current readback is compared with V / R.
    """

    voltage: float = Field(
        1.0,
        gt=0,
        le=1.5,
        description="PSU voltage on the bank (V). Capped at 1.5 V: 15.4 ohm at 1.5 V is already 0.15 W",
    )
    readings: int = Field(3, ge=1, description="PSU current readings averaged per state")
    settle_time: float = Field(0.2, ge=0, description="Wait after switching a bit (s)")
    tolerance: float = Field(0.10, gt=0, lt=1, description="Allowed relative current error per bit")
    abs_tolerance: float = Field(1.5e-3, ge=0, description="Plus this absolute error (A): readback noise")


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

    @model_validator(mode="after")
    def _check(self) -> LineRegConfig:
        if self.vin_stop <= self.vin_start:
            raise ValueError("line_reg.vin_stop must be above vin_start (the sweep goes up)")
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


class EnableConfig(_Model):
    """[enable]: how the AD2 drives the board's enable path (enable-startup, iq 'disabled', en-sine).

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

    @model_validator(mode="after")
    def _distinct(self) -> EnableConfig:
        if self.dio_pin_n is not None and self.dio_pin_n == self.dio_pin:
            raise ValueError("enable.dio_pin and enable.dio_pin_n must differ")
        return self


class EnSineConfig(_Model):
    """[en_sine] (enable-sine): a sine into the enable block's analog input, its output watched for the thresholds."""

    vin: float = Field(gt=0, description="PSU on VIN (feeds EN_VDD through J1 9-11) (V)")
    frequency: float = Field(1e3, gt=0, description="Sine frequency (Hz)")
    low: float = Field(0.0, ge=-0.3, description="Sine minimum (V)")
    high: float | None = Field(None, gt=0, description="Sine maximum (V); None = vin (EN_VDD)")
    cycles: float = Field(4.0, gt=0.5, description="Sine cycles per capture")
    n_samples: int = Field(8192, gt=64, description="Samples per capture (sample rate follows from cycles)")
    captures: int = Field(
        8, ge=1, description="Separate captures; thresholds are reported as mean and spread"
    )
    wavegen_channel: Literal[1, 2] = Field(1, description="AD2 W1/W2 -> EN_FG SMA (J2)")
    timeout: float = Field(5.0, gt=0, description="Max wait for one AD2 trigger (s)")

    @property
    def sine_high(self) -> float:
        """Sine maximum actually used (V): `high`, or `vin` if not set."""
        return self.vin if self.high is None else self.high

    @model_validator(mode="after")
    def _check(self) -> EnSineConfig:
        if self.sine_high <= self.low:
            raise ValueError("en_sine.high must be above en_sine.low")
        return self


class StartupConfig(_Model):
    """[startup] (enable-startup): step the enable line and record VOUT and the BGR (VREF) / supply current."""

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
    ch2: Literal["vref", "iin", "iout"] = Field(
        "vref", description="Scope 2: VREF at TP3 (BGR), or across R13 (input current) / R1 (output current)"
    )
    ch2_range: float = Field(5.0, gt=0, description="Scope 2 peak-to-peak range (V); 5 is the AD2's finest")
    sense_ohm: float = Field(0.1, gt=0, description="R13 / R1 value (ohm)")
    settle_fraction: float = Field(
        0.02, gt=0, lt=1, description="Settled = within this fraction of the final level"
    )
    vref_nominal: float | None = Field(
        None, gt=0, description="Expected BGR output (V) for the setup check; None = no check"
    )
    timeout: float = Field(5.0, gt=0, description="Max wait for one AD2 trigger (s)")


# Supply rail the DMM measures in iq 'series' mode: 'total' = between PSU and VIN_DC; the others are
# JAFWAK J1 jumpers (bgr 1-3, vdd 5-7, en 9-11, ota 13-15).
IqRail = Literal["total", "bgr", "vdd", "en", "ota"]


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
    readings: int = Field(5, ge=1, description="DMM readings averaged per point (and for the zero)")
    settle_time: float = Field(0.5, ge=0, description="Wait after each VIN / enable change (s)")
    dmm_range: float | None = Field(
        None,
        gt=0,
        description="DMM range: V for shunt (default 0.1 = 100 mV), A for series (default auto)",
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

    @model_validator(mode="after")
    def _check(self) -> PsrrConfig:
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

    @model_validator(mode="after")
    def _check(self) -> LineTranConfig:
        if self.vin_high <= self.vin_low:
            raise ValueError("line_tran.vin_high must be above vin_low")
        if self.vin_high > AD2_WAVEGEN_MAX_V:
            raise ValueError(f"line_tran.vin_high exceeds the AD2 wavegen limit ({AD2_WAVEGEN_MAX_V} V)")
        if self.psu_voltage < self.vin_high + self.buffer_headroom:
            raise ValueError(
                f"line_tran.psu_voltage ({self.psu_voltage} V) must be at least vin_high + buffer_headroom "
                f"({self.vin_high + self.buffer_headroom} V) or the BUF634 will clip"
            )
        return self


class BenchConfig(_Model):
    """The whole TOML file. [dut], [psu] and [loadbank] are required; a test's section is required
    only to run that test (the CLI says so if it's missing)."""

    dut: DutConfig
    psu: PsuConfig
    ad2: Ad2Config = Ad2Config()
    dmm: DmmConfig | None = None
    xscope: XScopeConfig | None = None
    loadbank: LoadBankConfig
    analysis: AnalysisConfig = AnalysisConfig()
    psu_check: PsuCheckConfig = PsuCheckConfig()
    loadbank_check: LoadBankCheckConfig = LoadBankCheckConfig()
    load_reg: LoadRegConfig | None = None
    line_reg: LineRegConfig | None = None
    load_tran: LoadTranConfig | None = None
    line_tran: LineTranConfig | None = None
    enable: EnableConfig = EnableConfig()
    en_sine: EnSineConfig | None = None
    startup: StartupConfig | None = None
    iq: IqConfig | None = None
    psrr: PsrrConfig | None = None

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
        - enable-pin clashes: tests that drive the enable line (en_sine, startup, iq 'disabled')
          would fight the load bank if an enable DIO pin were also a load bank pin.
        """
        # When the PSU drives VIN directly, both ceilings apply.
        vin_ceiling = min(self.dut.vin_max, self.psu.v_max)
        max_code = self.loadbank.max_code

        lb = self.loadbank

        def code_ok(name: str, code: int) -> None:
            if not 0 <= code <= max_code:
                raise ValueError(f"{name} = {code} is outside the load bank range 0..{max_code}")
            if self.dut.i_max is not None:
                g = sum(1 / (r + lb.switch_resistance) for b, r in enumerate(lb.resistors) if code >> b & 1)
                i = self.dut.vout_nominal * g
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
            vin_ok("line_reg sweep", max(lr.vin_start, lr.vin_stop))
            code_ok("line_reg.load_code", lr.load_code)
        if self.load_tran:
            lt = self.load_tran
            vin_ok("load_tran.vin", lt.vin)
            code_ok("load_tran.code_low", lt.code_low)
            code_ok("load_tran.code_high", lt.code_high)
            if lt.code_low >= lt.code_high:
                raise ValueError("load_tran.code_low must be below code_high")
        en_pins = {self.enable.dio_pin} | ({self.enable.dio_pin_n} - {None})  # dio_pin_n may be None
        clash = en_pins & set(self.loadbank.dio_pins)
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
        if self.startup:
            vin_ok("startup.vin", self.startup.vin)
            code_ok("startup.load_code", self.startup.load_code)
        if self.iq:
            vin_ok("iq sweep", self.iq.vin_stop)  # vin_stop >= vin_start (IqConfig)
            code_ok("iq.check_code", self.iq.check_code)
        if self.psrr:
            ps = self.psrr
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
        return self


def load_config(path: Path) -> BenchConfig:
    """Read and validate a TOML config. Raises pydantic.ValidationError on any problem."""
    with open(path, "rb") as f:
        data = tomllib.load(f)
    return BenchConfig.model_validate(data)
