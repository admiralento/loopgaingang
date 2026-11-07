"""Bench / DUT configuration loaded from a TOML file.

Everything that depends on the board, the wiring or the test conditions lives in
the config file, so the code never hard-codes an instrument address or a pin.
The config is validated before any instrument is powered up.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

# AD2 limits
AD2_DIO_PINS = 16
AD2_WAVEGEN_MAX_V = 5.0


FlowName = Literal["dsr-pacing", "dsrdtr", "xonxoff", "none"]


class _Model(BaseModel):
    # Typos in the TOML file should fail loudly, not be silently ignored.
    model_config = ConfigDict(extra="forbid")


class DutConfig(_Model):
    name: str = "LDO"
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
    stopbits: Literal[1, 2] = 2
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
        if self.ovp is not None and self.ovp <= self.v_max:
            raise ValueError(f"psu.ovp ({self.ovp} V) must be above psu.v_max ({self.v_max} V)")
        return self

    def serial_settings(self):
        """RS-232 settings to use if this instrument is on a serial port."""
        from ldo_bench.instruments.transports import SerialSettings

        return SerialSettings(self.baud, self.parity, self.stopbits, self.flow)


class ScopeChannelConfig(_Model):
    range: float = Field(5.0, gt=0, description="Peak-to-peak input range (V). AD2: 5 or 50")
    offset: float = 0.0


class Ad2Config(_Model):
    serial: str = Field("", description="Empty = first Analog Discovery 2 found")
    vout_channel: Literal[1, 2] = 1
    ref_channel: Literal[1, 2] = 2
    vout: ScopeChannelConfig = ScopeChannelConfig()
    ref: ScopeChannelConfig = ScopeChannelConfig(range=50.0)
    wavegen_channel: Literal[1, 2] = 1

    @model_validator(mode="after")
    def _distinct(self) -> Ad2Config:
        if self.vout_channel == self.ref_channel:
            raise ValueError("ad2.vout_channel and ad2.ref_channel must differ")
        return self


class DmmConfig(_Model):
    """Bench DMM (Agilent 34401A) measuring VOUT in the DC tests."""

    enabled: bool = True
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
    stopbits: Literal[1, 2] = 2
    flow: FlowName = "dsr-pacing"
    nplc: Literal[0.02, 0.2, 1, 10, 100] = Field(10, description="Integration time in power-line cycles")
    range: float | None = Field(None, gt=0, description="Fixed DCV range (V); None = auto range")
    samples: int = Field(1, ge=1, description="Readings averaged per point")

    def serial_settings(self):
        """RS-232 settings to use if this instrument is on a serial port."""
        from ldo_bench.instruments.transports import SerialSettings

        return SerialSettings(self.baud, self.parity, self.stopbits, self.flow)


class XScopeConfig(_Model):
    """Agilent 54621A on RS-232, capturing each transient edge alongside the AD2 as a cross-check."""

    enabled: bool = True
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
    parity: Literal["none"] = "none"
    stopbits: Literal[1] = 1
    flow: FlowName = Field("dsrdtr", description="DTR = 'dsrdtr', XON/XOFF = 'xonxoff'")
    vout_channel: Literal[1, 2] = 1
    ref_channel: Literal[1, 2] = 2
    vout_range: float = Field(
        0.16, gt=0, description="Ch full-scale range (8 div) on VOUT, centred on vout_nominal (V)"
    )
    points: Literal[100, 250, 500, 1000, 2000] = 2000
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
    dio_pins: list[int] = Field(description="AD2 DIO pin for each bank bit, LSB first")
    resistors: list[float] = Field(description="Load resistor for each bit, LSB first (ohm)")
    switch_resistance: float = Field(0.0, ge=0, description="Per-switch on-resistance (ohm)")
    active_high: bool = True

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
        return (1 << len(self.dio_pins)) - 1


class AnalysisConfig(_Model):
    settle_fraction: float = Field(0.1, gt=0, lt=1)
    smooth_time: float = Field(1e-6, gt=0)
    peak_window: float = Field(20e-6, gt=0)
    blank_time: float = Field(0.5e-6, ge=0)
    settle_reference: Literal["pre", "post"] = Field(
        "pre",
        description=(
            "'pre' = settle back to the pre-step level (same as the WaveForms script); "
            "'post' = settle to the final level after the step"
        ),
    )


class PsuCheckConfig(_Model):
    """psu-check: DMM across the PSU output, nothing else connected."""

    points: list[float] = Field(
        default_factory=lambda: [0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0],
        description="Setpoints (V); anything above psu.v_max is refused",
    )
    readings: int = Field(3, ge=1, description="DMM readings averaged per point")
    settle_time: float = Field(0.5, ge=0, description="Extra wait after each setpoint (s)")


class LoadBankCheckConfig(_Model):
    """loadbank-check: the load bank straight across the PSU (no LDO) to verify the DIO wiring."""

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
    vin: float = Field(gt=0)
    codes: list[int] = Field(default_factory=list, description="Empty = every code 0..max")
    settle_time: float = Field(0.05, ge=0)
    dc_window: float = Field(
        0.1, gt=0, description="DC averaging window (s); 0.1 s spans whole 50/60 Hz cycles"
    )
    psu_readback: bool = Field(True, description="Also record the PSU's V/I readback (~0.8 s per point)")


class LineRegConfig(_Model):
    vin_start: float = Field(gt=0)
    vin_stop: float = Field(gt=0)
    vin_step: float = Field(gt=0)
    load_code: int = Field(0, ge=0)
    fit_vin_min: float | None = Field(
        None, description="Only fit the slope above this Vin (V); default = 3 x step above the dropout knee"
    )
    dropout_fraction: float = Field(
        0.01, gt=0, lt=1, description="Dropout = Vout falls this fraction below its max-Vin value"
    )
    dc_window: float = Field(0.1, gt=0)
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
    sample_rate: float = Field(10e6, gt=0)
    n_samples: int = Field(8192, gt=16)
    pre_trigger: float = Field(0.2, gt=0, lt=1, description="Fraction of the capture before the step")
    averages: int = Field(16, ge=1)
    edges: list[Literal["rising", "falling"]] = Field(default_factory=lambda: ["rising", "falling"])
    timeout: float = Field(5.0, gt=0)
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
    vin: float = Field(gt=0)
    code_low: int = Field(ge=0)
    code_high: int = Field(ge=0)
    trigger_pin: int | None = Field(
        None, description="DIO pin wired to the ref channel; default = MSB that changes"
    )
    dio_high_level: float = Field(3.3, gt=0, description="DIO logic-high voltage, for the trigger level")


class EnableConfig(_Model):
    """How the AD2 drives the board's enable path.

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
    """en-sine: a sine into the enable block's analog input, its output watched for the thresholds."""

    vin: float = Field(gt=0, description="PSU on VIN (feeds EN_VDD through J1 9-11) (V)")
    frequency: float = Field(1e3, gt=0, description="Sine frequency (Hz)")
    low: float = Field(0.0, ge=-0.3, description="Sine minimum (V)")
    high: float | None = Field(None, gt=0, description="Sine maximum (V); None = vin (EN_VDD)")
    cycles: float = Field(4.0, gt=0.5, description="Sine cycles per capture")
    n_samples: int = Field(8192, gt=64)
    captures: int = Field(
        8, ge=1, description="Separate captures; thresholds are reported as mean and spread"
    )
    wavegen_channel: Literal[1, 2] = Field(1, description="AD2 W1/W2 -> EN_FG SMA (J2)")
    timeout: float = Field(5.0, gt=0)

    @property
    def sine_high(self) -> float:
        return self.vin if self.high is None else self.high

    @model_validator(mode="after")
    def _check(self) -> EnSineConfig:
        if self.sine_high <= self.low:
            raise ValueError("en_sine.high must be above en_sine.low")
        return self


class StartupConfig(_Model):
    """enable-startup: step the enable line and record VOUT and the BGR (VREF) / supply current."""

    vin: float = Field(gt=0)
    load_code: int = Field(0, ge=0, description="Load during the edges (0 = none)")
    edges: list[Literal["rising", "falling"]] = Field(
        default_factory=lambda: ["rising", "falling"], description="rising = enable, falling = disable"
    )
    window: float = Field(1e-3, gt=0, description="Capture length (s)")
    n_samples: int = Field(8192, gt=64)
    pre_trigger: float = Field(0.1, gt=0, lt=1)
    averages: int = Field(8, ge=1)
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
    timeout: float = Field(5.0, gt=0)


class LineTranConfig(TransientCaptureConfig):
    psu_voltage: float = Field(gt=0, description="PSU voltage on VS_BUFF (buffer supply) (V)")
    vin_low: float = Field(ge=0, description="W1 low level -> VIN through the BUF634 (V)")
    vin_high: float = Field(gt=0)
    load_code: int = Field(0, ge=0)
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

    @model_validator(mode="after")
    def _cross_checks(self) -> BenchConfig:
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
        en_pins = {self.enable.dio_pin} | ({self.enable.dio_pin_n} - {None})
        clash = en_pins & set(self.loadbank.dio_pins)
        if (self.en_sine or self.startup) and clash:
            raise ValueError(
                f"enable DIO pins {sorted(clash)} are also load bank pins {self.loadbank.dio_pins}"
            )
        if self.en_sine:
            es = self.en_sine
            vin_ok("en_sine.vin", es.vin)
            if es.sine_high > self.dut.vin_max:
                raise ValueError(
                    f"en_sine.high ({es.sine_high} V) exceeds dut.vin_max ({self.dut.vin_max} V)"
                )
        if self.startup:
            vin_ok("startup.vin", self.startup.vin)
            code_ok("startup.load_code", self.startup.load_code)
        if self.line_tran:
            lt = self.line_tran
            # Here the PSU feeds the buffer rail; VIN comes from the wavegen through the buffer.
            if lt.psu_voltage > self.psu.v_max:
                raise ValueError(f"line_tran.psu_voltage exceeds psu.v_max ({self.psu.v_max} V)")
            if lt.vin_high > self.dut.vin_max:
                raise ValueError(f"line_tran.vin_high exceeds dut.vin_max ({self.dut.vin_max} V)")
            code_ok("line_tran.load_code", lt.load_code)
        return self


def load_config(path: Path) -> BenchConfig:
    with open(path, "rb") as f:
        data = tomllib.load(f)
    return BenchConfig.model_validate(data)
