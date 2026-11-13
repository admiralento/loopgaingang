"""Simulated bench: an LDO model plus fake PSU / AD2 that all share its state.

Used by `--sim` and by the test suite, so every procedure can run end to end
with no hardware attached. The model is deliberately simple but has the
features the analysis has to handle: output resistance, finite line
regulation, dropout, noise, an underdamped transient and a switching glitch.

What VIN is connected to follows the real board: if the wavegen is driving
(line transient, VIN through the BUF634) VIN follows W1; otherwise VIN follows
the PSU output.

LdoModel holds both the device model and the bench wiring. Procedures tell the
operator to move a probe or cable; in the sim the tests (and --sim) set the
matching field instead:
  psu_to     where the PSU output goes: "vin" (straight to VIN) or "rail" (the BUF634's
             supply, VIN = buffered W1; line transient and PSRR)
  ref_probe  what the AD2 reference channel (ch2) probes: ("vin", 0), ("dio", pin),
             ("vref", 0) BGR output, ("iin", 0) across R13, ("iout", 0) across R1,
             ("tmp", 0) the TMP235 output (J13 pin 5)
  ch1_probe  what the VOUT channel probes: "vout" or "en_out" (enable-sine test)
  dmm_probe  what the DMM is on: "vout", "vref" (TP3, the BGR output), "psu" (supply terminals),
             "shunt" (across R13), "series" (in series with a rail, current mode) or "open"
  en_pin     the AD2 DIO driving the enable path, None if EN is tied on
  vref_wired the PSU's second output is wired to VREF_IN (external reference run)
  jp2_fitted JP2 joins the BGR to VREF_IN; with it fitted the BGR sets the reference (and a
             wired second output back-drives it: bgr_backdriven records that it happened)
  buffer_split  the reworked U2 on a split supply ([buffer]): its output can't get within
             buffer_dropout of either rail, and a floating input sends it to the top rail
  vminus_wired  the PSU's second output is wired reversed onto U2's V- (its setpoint is the
             negative rail's magnitude)
Fake instruments (SimPowerSupply, SimAnalogDiscovery, SimDmm, SimXScope) read and
write this shared state, so e.g. a DIO write changes the load the PSU sees.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field

import numpy as np

from ldo_bench.instruments.base import Capture, ChannelSetup, DcReading, Edge, InstrumentError, WaveShape

# Fraction of the quiescent current drawn by each of the board's J1 supply rails (iq series test).
RAIL_SHARE = {"total": 1.0, "bgr": 0.15, "vdd": 0.30, "en": 0.10, "ota": 0.45}


@dataclass
class LdoModel:
    """Behavioural LDO + bench model.

    Device parameters (resistances, time constants, currents) and bench state (setpoints, DIO
    levels, where probes are) share this one object; the bench-state fields are the ones the
    fake instruments and the tests write.
    """

    vout_nominal: float  # (V) regulated output at zero load with VIN = VOUT + 0.5 V
    resistors: list[float]  # (ohm) load bank resistors, one per DIO pin below
    dio_pins: list[int]  # AD2 DIO switching each load resistor
    active_high: bool = True  # True: DIO high switches the load on
    r_out: float = 0.05  # ohm, DC load regulation
    line_reg: float = 0.5e-3  # V/V, DC line regulation
    dropout: float = 0.10  # V, VIN - VOUT in dropout at zero load
    r_dropout: float = 1.0  # ohm, extra dropout per amp
    # A, quiescent current seen by the PSU readback, and the "off" current in the iin probe and
    # startup_signals(); the iq test itself uses iq_on / iq_off
    iq: float = 50e-6
    noise_rms: float = 0.4e-3  # V, white noise on every scope sample (VOUT; ref channel gets a fraction)
    # Transient: v(t) = A * exp(-t / tau) * sin(2 pi f t) after the step, A = -z_peak * dI + k_line * dVIN.
    # (sin, so the ring starts from zero; the actual peak is somewhat below A.)
    z_peak: float = 0.4  # ohm, ring amplitude per amp of load step (more load -> dip)
    k_line: float = 0.05  # V/V, ring amplitude per volt of line step
    tau: float = 4e-6  # s, ring decay time constant (loop damping)
    f_ring: float = 150e3  # Hz, ring frequency (roughly the loop's crossover)
    glitch: float = 0.05  # V, one-sample spike at the switching edge
    seed: int = 1234  # noise RNG seed, so test results are repeatable

    # bench state
    psu_v: float = 0.0  # V, programmed setpoint
    psu_on: bool = False
    awg_v: dict[int, float] = field(default_factory=dict)  # wavegen channels outputting DC: ch -> V
    dio: dict[int, bool] = field(default_factory=dict)  # DIO levels being driven (absent = released)
    ref_probe: tuple[str, int] = ("vin", 0)  # what scope ch2 is wired to (see module docstring)
    dmm_probe: str = "vout"  # what the DMM is across (see module docstring)
    bank_on_psu: bool = False  # loadbank-check: the load bank straight across the PSU, no LDO
    psu_i_offset: float = 2.0e-3  # A, current readback offset (cancelled by zero-load subtraction)
    psu_offset: float = -1.0e-3  # V, sim supply output error: offset + gain, like the bench E3648A
    psu_gain: float = 4.5e-4
    psu_readback_gain: float = 5.5e-4  # readback reads high, ~2.7 mV at 5 V
    # enable block (EN_AIN -> Schmitt comparator -> EN_OUT) and turn-on / turn-off dynamics
    en_pin: int | None = None  # AD2 DIO driving EN_CTRL_33 -> enable path; None = always enabled
    en_vt_rise: float = 0.58  # fraction of EN_VDD (= VIN) where EN_OUT goes high
    en_vt_fall: float = 0.42  # fraction of EN_VDD where EN_OUT goes low (Schmitt hysteresis)
    en_t_rise: float = 30e-9  # EN_OUT edge time (s)
    en_delay: float = 30e-9  # s, EN_AIN crossing a threshold -> EN_OUT switching (propagation delay)
    vref_nominal: float = 1.2  # BGR output (V) at VIN 1.8 V and 25 C
    bgr_line: float = 0.5e-3  # V/V, BGR line sensitivity while it has headroom
    bgr_min_vin: float = 1.56  # V, below this VIN the BGR runs out of headroom: VREF falls 1 V/V
    bgr_tc: float = 58.9e-6  # 1/C, VREF temperature coefficient (VOUT follows it)
    # temperature (temp logger): the TMP235 on VS_BUFF, read by the AD2 on J13 pin 5
    temp_profile: Callable[[float], float] | None = None  # clock (s) -> board temperature (C); None = 25 C
    clock: float = 0.0  # s, simulated time: Bench.sleep advances it
    vs_buff_on: bool = True  # VS_BUFF powered (by hand), so the TMP235 runs
    vref_delay: float = 4e-6  # s from the enable edge until VREF starts rising
    vref_tau: float = 8e-6  # s, VREF rise time constant (turn-off falls 4x faster)
    vout_delay: float = 15e-6  # s, after VREF starts
    vout_tau: float = 35e-6  # s, VOUT soft-start time constant
    vout_off_tau: float = 300e-6  # s, VOUT discharge with no load (with load: C_out / G if faster)
    c_out: float = 10e-6  # output capacitance: inrush = C dV/dt
    iq_on: float = 150e-6  # quiescent current when enabled (A)
    sense_ohm: float = 0.1  # ohm, current-sense resistors R13 (input) / R1 (output)
    ch1_probe: str = "vout"  # what the VOUT channel is on: "vout" or "en_out"
    awg_wave: dict[int, tuple[str, float, float, float]] = field(
        default_factory=dict
    )  # ch -> (shape, f, low, high)
    # quiescent / shutdown current and the DMM measuring it
    iq_off: float = 0.3e-6  # A, shutdown current (below the shunt method's floor, like the real board)
    iq_vin_slope: float = 0.05  # fractional change in Iq per volt of VIN (relative to 1.8 V)
    dmm_rail: str = "total"  # iq series method: which rail the DMM is in (a RAIL_SHARE key)
    dmm_offset_v: float = 2e-6  # V, thermal EMF / offset on the 100 mV range (the iq zero removes it)
    dmm_noise_v: float = 0.15e-6  # V rms, DMM noise on the shunt / open readings
    dmm_noise_a: float = 5e-9  # A rms, DMM noise in current mode
    # external reference (JAFWAK JP2 / VREF_IN)
    vref_out_v: float = 0.0  # V, setpoint of the PSU's second output
    vref_wired: bool = False
    jp2_fitted: bool = True
    bgr_backdriven: bool = False
    # PSRR: VOUT/VIN = h_dc (1 + j f/fz) / (1 + j f/fp)
    psrr_h_dc: float = 1e-3  # -60 dB at low frequency
    psrr_fz: float = 5e3  # Hz, zero: rejection worsens above here as loop gain falls
    psrr_fp: float = 2e6
    vout_hum: tuple[float, float] | None = (
        None  # (amplitude V, frequency Hz) of a tone on VOUT, e.g. mains  # Hz, pole: |H| levels off again
    )

    def __post_init__(self) -> None:
        self.rng = np.random.default_rng(self.seed)

    # --- DC model -----------------------------------------------------------
    buffer_offset: float = -0.015  # V, BUF634 output minus input (line tran calibrates it out)
    psu_to: str = "vin"  # where the PSU is wired: "vin" (DC tests) or "rail" (BUF634 supply, line tran)
    buffer_dropout: float = 1.5  # V, the BUF634 output can't get closer than this to its rail
    buffer_split: bool = False  # U2 reworked: V- from the second output (see the module docstring)
    vminus_wired: bool = False

    @property
    def vin(self) -> float:
        """VIN on the board (V): PSU setpoint, or the buffered W1 level when psu_to == "rail"."""
        if self.psu_to == "rail":  # VIN = buffer output: W1 + offset, only while the rail is powered
            src = self.awg_level()
            if self.buffer_split:
                return self._split_buffer_out(src)
            if not self.psu_on or src is None:
                return 0.0
            out = src + self.buffer_offset
            return max(0.0, min(out, self.psu_v - self.buffer_dropout))
        return self.psu_v if self.psu_on else 0.0

    def _split_buffer_out(self, src: float | None) -> float:
        """U2's output (V) on its split supply, for input `src` (V; None = floating).

        It stays buffer_dropout inside both rails. With the rails too low for any output range it
        sits midway between them; with a floating input it goes to the top of its range.
        """
        vp = self.psu_v if self.psu_on else 0.0
        vm = self.vref_out_v if (self.psu_on and self.vminus_wired) else 0.0  # second output = |V-|
        if vp <= 0 and vm <= 0:
            return 0.0
        hi, lo = vp - self.buffer_dropout, -vm + self.buffer_dropout
        if lo > hi:
            return (vp - vm) / 2
        if src is None:
            return hi
        return min(max(src + self.buffer_offset, lo), hi)

    def awg_level(self) -> float | None:
        """DC level the wavegen puts on the buffer input (a sine's mid-level), None if it's off."""
        if self.awg_v:
            return next(iter(self.awg_v.values()))
        if self.awg_wave:
            _, _, lo, hi = next(iter(self.awg_wave.values()))
            return (lo + hi) / 2
        return None

    def supply_current(self, rail: str = "total") -> float:
        """Current drawn from VIN (rail 'total') or by one J1 rail; the load flows through VDD."""
        vin = self.vin
        if vin <= 0:
            return 0.0
        q = (self.iq_on if self.enabled else self.iq_off) * (1 + self.iq_vin_slope * (vin - 1.8))
        i = q * RAIL_SHARE[rail]
        if rail in ("total", "vdd"):
            i += self.load_current()
        return i

    def psrr_h(self, f: float) -> complex:
        """VOUT/VIN transfer function at `f` (Hz); 0 when the LDO is off."""
        if not self.enabled or self.vin <= 0:
            return 0j
        return self.psrr_h_dc * (1 + 1j * f / self.psrr_fz) / (1 + 1j * f / self.psrr_fp)

    def conductance(self) -> float:
        """Total load conductance (S) of the load resistors switched on by the DIO pins."""
        return sum(
            1.0 / r
            for pin, r in zip(self.dio_pins, self.resistors, strict=True)
            if self.dio.get(pin, not self.active_high) == self.active_high  # released pin = load off
        )

    @property
    def reference(self) -> float:
        """What the regulator follows (V): the BGR (vout_nominal), or the external reference with JP2 out."""
        if self.vref_wired and not self.jp2_fitted:
            return self.vref_out_v if self.psu_on else 0.0
        return self.vout_nominal * (1 + self.bgr_tc * (self.temperature - 25.0))

    @property
    def temperature(self) -> float:
        """Board temperature (C) now, from temp_profile (25 C when unset)."""
        return 25.0 if self.temp_profile is None else float(self.temp_profile(self.clock))

    def bgr_vref(self) -> float:
        """BGR output at TP3 (V): vref_nominal with its line and temperature terms, falling 1 V/V
        below bgr_min_vin; 0 when the LDO is disabled or VIN is off."""
        vin = self.vin
        if vin <= 0 or not self.enabled:
            return 0.0
        v = self.vref_nominal * (1 + self.bgr_tc * (self.temperature - 25.0)) + self.bgr_line * (vin - 1.8)
        if vin < self.bgr_min_vin:
            v -= self.bgr_min_vin - vin
        return max(0.0, v)

    @property
    def enabled(self) -> bool:
        """LDO enabled: EN tied on (en_pin None) or its DIO driven high."""
        return self.en_pin is None or bool(self.dio.get(self.en_pin, False))

    def vout_dc(self, vin: float | None = None, g: float | None = None) -> float:
        """Steady-state VOUT (V) for `vin` (V) and load conductance `g` (S), default the present ones.

        The lower of the regulated value (line + load regulation) and the dropout limit.
        """
        vin = self.vin if vin is None else vin
        g = self.conductance() if g is None else g
        if vin <= 0 or not self.enabled:
            return 0.0
        ref = self.reference
        v = ref
        for _ in range(4):  # load current depends on vout; iterate to a fixed point
            i = v * g
            # line regulation referenced to VIN = VOUT + 0.5 V
            vreg = ref + self.line_reg * (vin - ref - 0.5) - self.r_out * i
            vdrop = vin - self.dropout - self.r_dropout * i
            v = max(0.0, min(vreg, vdrop))
        return v

    def psu_actual(self) -> float:
        """What is really on the supply terminals (sim includes a small output error)."""
        if not self.psu_on or self.psu_v == 0:
            return 0.0
        return self.psu_v * (1 + self.psu_gain) + self.psu_offset

    def load_current(self) -> float:
        """Current into the load bank (A)."""
        return self.vout_dc() * self.conductance()

    def ref_value(self) -> float:
        """DC level (V) at whatever ref_probe is on."""
        kind, pin = self.ref_probe
        if kind == "dio":
            return 3.3 if self.dio.get(pin, False) else 0.0
        if kind == "vref":
            return self.bgr_vref()
        if kind == "tmp":  # TMP235: 500 mV at 0 C, 10 mV/C, powered from VS_BUFF
            return 0.5 + 0.01 * self.temperature if self.vs_buff_on else 0.0
        if kind == "iin":  # across R13: everything the board draws from VIN
            return self.sense_ohm * (self.load_current() + (self.iq_on if self.enabled else self.iq))
        if kind == "iout":  # across R1: the LDO's output current
            return self.sense_ohm * self.load_current()
        return self.vin

    def startup_signals(self, t: np.ndarray, on_before: bool, on_after: bool) -> dict[str, np.ndarray]:
        """VOUT, VREF, input and output current around an enable / disable edge at t = 0."""
        v_on = self.vout_dc() if on_after else self.vout_dc_if(on_before)
        g = self.conductance()
        ts = np.clip(t, 0, None)
        if on_after and not on_before:  # turn-on
            vref = np.where(
                t >= self.vref_delay,
                self.vref_nominal * (1 - np.exp(-(ts - self.vref_delay) / self.vref_tau)),
                0,
            )
            vref = np.clip(vref, 0, None)
            d = self.vref_delay + self.vout_delay
            vout = np.where(t >= d, v_on * (1 - np.exp(-(ts - d) / self.vout_tau)), 0.0)
            vout = np.clip(vout, 0, None)
            dvdt = np.where(t >= d, v_on / self.vout_tau * np.exp(-(ts - d) / self.vout_tau), 0.0)
            iq = np.where(t >= 0, self.iq_on, self.iq)
        elif on_before and not on_after:  # turn-off: VREF drops fast, VOUT discharges through load
            tau = self.vout_off_tau if g == 0 else min(self.vout_off_tau, self.c_out / g)
            vref = np.where(t >= 0, self.vref_nominal * np.exp(-ts / (self.vref_tau / 4)), self.vref_nominal)
            vout = np.where(t >= 0, v_on * np.exp(-ts / tau), v_on)
            dvdt = np.zeros_like(t)
            iq = np.where(t >= 0, self.iq, self.iq_on)
        else:
            level = v_on if on_after else 0.0
            vref = np.full_like(t, self.vref_nominal if on_after else 0.0)
            vout = np.full_like(t, level)
            dvdt = np.zeros_like(t)
            iq = np.full_like(t, self.iq_on if on_after else self.iq)
        iout = vout * g + self.c_out * dvdt
        return {"vout": vout, "vref": vref, "iout": iout, "iin": iout + iq}

    def vout_dc_if(self, enabled: bool) -> float:
        """vout_dc() as if the LDO were (or weren't) enabled, whatever the EN pin says now."""
        saved = self.en_pin
        try:
            if enabled:
                self.en_pin = None
            return self.vout_dc() if enabled else 0.0
        finally:
            self.en_pin = saved

    def noise(self, n: int) -> np.ndarray:
        """n samples of white scope noise (V), noise_rms each."""
        return self.rng.normal(0.0, self.noise_rms, n)


class SimPowerSupply:
    """PowerSupply stand-in (an E3648A with a realistic output and readback error)."""

    def __init__(self, model: LdoModel) -> None:
        self.m = model
        self.idn = "SIM,E3648A,0,sim"
        self.current_limit = 0.0

    def setup(self, *, current_limit: float, ovp: float | None, voltage_range: str | None) -> None:
        self.current_limit = current_limit

    def set_voltage(self, volts: float) -> None:
        self.m.psu_v = volts

    def get_voltage_setpoint(self) -> float:
        return self.m.psu_v

    def output(self, on: bool) -> None:
        self.m.psu_on = on

    def measure_voltage(self) -> float:
        return self.m.psu_actual() * (1 + self.m.psu_readback_gain) if self.m.psu_on else 0.0

    def measure_current(self) -> float:
        if not self.m.psu_on:
            return 0.0
        if self.m.bank_on_psu:
            noise = float(self.m.rng.normal(0.0, 0.2e-3))
            return self.m.psu_actual() * self.m.conductance() + self.m.psu_i_offset + noise
        buffer_iq = 1.5e-3 if self.m.awg_v else 0.0  # A, the BUF634's own supply current
        return self.m.load_current() + self.m.iq + buffer_iq

    def aux(self, channel: int) -> SimSupplyOutput:
        return SimSupplyOutput(self.m, channel)

    def close(self) -> None:
        pass


class SimSupplyOutput:
    """SupplyOutput stand-in: the sim E3648A's second output, wired to VREF_IN when m.vref_wired."""

    def __init__(self, model: LdoModel, channel: int) -> None:
        self.m = model
        self.channel = channel
        self.current_limit = 0.0

    def setup(self, *, current_limit: float, ovp: float | None) -> None:
        self.current_limit = current_limit
        self.set_voltage(0.0)

    def set_voltage(self, volts: float) -> None:
        self.m.vref_out_v = volts
        if self.m.vref_wired and self.m.jp2_fitted and volts > 0:
            self.m.bgr_backdriven = True

    def get_voltage_setpoint(self) -> float:
        return self.m.vref_out_v

    def measure_voltage(self) -> float:
        return self.m.vref_out_v * (1 + self.m.psu_readback_gain) if self.m.psu_on else 0.0

    def measure_current(self) -> float:
        return 0.0


class SimAnalogDiscovery:
    """Implements Oscilloscope, DigitalOut and Wavegen, like the real AD2 driver."""

    def __init__(self, model: LdoModel, vout_channel: int = 1) -> None:
        self.m = model
        self.vout_channel = vout_channel
        self.idn = "SIM Analog Discovery 2 (SN:SIM000)"
        self._touched: set[int] = set()

    # --- Oscilloscope -------------------------------------------------------
    def _value(self, ch: int, vout: float, ref: float) -> float:
        """Pick the VOUT or the ref-probe value for scope channel `ch`."""
        return vout if ch == self.vout_channel else ref

    def measure_dc(self, duration: float, setups: dict[int, ChannelSetup]) -> dict[int, DcReading]:
        n = 8192
        vout, ref = self.m.vout_dc(), self.m.ref_value()
        out = {}
        for ch in setups:
            x = self._value(ch, vout, ref) + self.m.noise(n)
            out[ch] = DcReading(float(np.mean(x)), float(np.std(x)), n)
        return out

    def capture(
        self,
        *,
        sample_rate: float,
        n_samples: int,
        setups: dict[int, ChannelSetup],
        trigger_channel: int,
        trigger_level: float,
        edge: Edge,
        pre_trigger: float,
        stimulus: Callable[[], None],
        timeout: float,
        force: bool = False,
        dio_trigger: tuple[int, Edge] | None = None,
        free_running: bool = False,  # accepted for the protocol; the model has no arm race
    ) -> Capture:
        """Synthesise the capture by evaluating the model before and after stimulus().

        Dispatches to the PSRR, enable-sine or DIO-triggered models when those are set up;
        otherwise a load / line step with a damped ring. Raises like a trigger timeout if the
        step doesn't cross trigger_level.
        """
        m = self.m
        if m.awg_wave and m.psu_to == "rail":
            return self._psrr_capture(sample_rate, n_samples, stimulus)
        if m.awg_wave:
            return self._sine_capture(sample_rate, n_samples, trigger_level, edge, pre_trigger, stimulus)
        if dio_trigger is not None:
            return self._dio_capture(sample_rate, n_samples, dio_trigger, pre_trigger, stimulus, force)
        vin0, g0, ref0 = m.vin, m.conductance(), m.ref_value()
        v0 = m.vout_dc(vin0, g0)
        stimulus()
        vin1, g1, ref1 = m.vin, m.conductance(), m.ref_value()
        v1 = m.vout_dc(vin1, g1)

        trig_before = self._value(trigger_channel, v0, ref0)
        trig_after = self._value(trigger_channel, v1, ref1)
        crossed = (
            trig_before < trigger_level <= trig_after
            if edge == "rising"
            else trig_before > trigger_level >= trig_after
        )
        if not crossed and not force:
            raise InstrumentError(
                f"trigger timeout: ch{trigger_channel} went {trig_before:.3f} -> {trig_after:.3f} V, "
                f"no {edge} crossing of {trigger_level:.3f} V"
            )

        i_trig = round(pre_trigger * n_samples)
        t = (np.arange(n_samples) - i_trig) / sample_rate
        after = t >= 0
        di = v0 * g1 - v0 * g0  # load step (A) at the pre-step VOUT, + = more load
        dvin = vin1 - vin0
        amp = -m.z_peak * di + m.k_line * dvin
        ts = np.where(after, t, 0.0)
        # starts at zero and builds up, like a real loop (peak ~ a quarter ring period after the step)
        ring = amp * np.exp(-ts / m.tau) * np.sin(2 * math.pi * m.f_ring * ts)
        vout = np.where(after, v1 + ring, v0) + m.noise(n_samples)
        if 0 <= i_trig < n_samples:
            vout[i_trig] += m.glitch * (1 if (di < 0 or dvin > 0) else -1)
        ref = np.where(after, ref1, ref0) + m.noise(n_samples) * 0.25
        ch1, ch2 = (vout, ref) if self.vout_channel == 1 else (ref, vout)
        self.last_capture = Capture(sample_rate, ch1, ch2, i_trig, edge_found=not force)
        return self.last_capture

    def _put(self, vout_side: np.ndarray, ref_side: np.ndarray, rate: float, i0: int, found: bool) -> Capture:
        """Build a Capture with VOUT / ref on the right channels; kept as last_capture for SimXScope."""
        ch1, ch2 = (vout_side, ref_side) if self.vout_channel == 1 else (ref_side, vout_side)
        self.last_capture = Capture(rate, ch1, ch2, i0, edge_found=found)
        return self.last_capture

    def _sine_capture(self, rate, n, level, edge, pre, stimulus) -> Capture:
        """W1 sine or triangle on EN_AIN (scope ref channel), the enable comparator's EN_OUT on the other."""
        m = self.m
        stimulus()
        shape, f, lo, hi = next(iter(m.awg_wave.values()))
        mid, amp = (hi + lo) / 2, (hi - lo) / 2
        i0 = round(pre * n)
        t = (np.arange(n) - i0) / rate
        ratio = float(np.clip((level - mid) / amp, -1, 1))
        # phase so the wave crosses `level` with the right slope at t = 0 (the trigger).
        # The triangle is (2/pi) asin(sin(theta)): same phase as the sine, linear between its peaks.
        # The square jumps at theta = 0 / pi whatever the level.
        start = {"sine": math.asin(ratio), "triangle": math.pi / 2 * ratio}.get(shape, 0.0)
        phase = start if edge == "rising" else math.pi - start

        def wave(tt: np.ndarray) -> np.ndarray:
            s = np.sin(2 * math.pi * f * tt + phase)
            if shape == "sine":
                return mid + amp * s
            if shape == "triangle":
                return mid + amp * 2 / math.pi * np.arcsin(s)
            return mid + amp * np.where(s >= 0, 1.0, -1.0)

        ain = wave(t)
        ain_late = wave(t - m.en_delay)  # what the comparator acts on, en_delay later
        vdd = m.vin
        hi_th, lo_th = m.en_vt_rise * vdd, m.en_vt_fall * vdd
        out = np.zeros(n)
        state = (
            ain_late[0] > (hi_th + lo_th) / 2
        )  # initial comparator state (unknown inside the hysteresis band)
        for k, a in enumerate(ain_late):
            if not state and a >= hi_th:
                state = True
            elif state and a <= lo_th:
                state = False
            out[k] = vdd if state else 0.0
        # finite edge time: a short moving average over en_t_rise
        w = max(1, round(m.en_t_rise * rate))
        if w > 1:
            out = np.convolve(out, np.ones(w) / w, mode="same")
        out = out + m.noise(n) * 0.5
        en_out = out if m.ch1_probe == "en_out" else np.full(n, m.vout_dc()) + m.noise(n)
        return self._put(en_out, ain + m.noise(n) * 0.25, rate, i0, True)

    def _psrr_capture(self, rate, n, stimulus) -> Capture:
        """W1 sine through the buffer onto VIN; VOUT carries it attenuated by the PSRR (free-running)."""
        m = self.m
        stimulus()
        _, f, lo, hi = next(iter(m.awg_wave.values()))
        t = np.arange(n) / rate
        ph = float(m.rng.uniform(0, 2 * math.pi))
        vin_dc = m.vin
        amp = (hi - lo) / 2 if vin_dc > 0 else 0.0
        vin = vin_dc + amp * np.sin(2 * math.pi * f * t + ph)
        if vin_dc > 0:
            vin = np.minimum(vin, m.psu_v - m.buffer_dropout)
        h = m.psrr_h(f)
        vout = m.vout_dc(vin_dc) + abs(h) * amp * np.sin(2 * math.pi * f * t + ph + np.angle(h))
        if m.vout_hum is not None:  # not locked to the injected tone: its phase differs each capture
            a_hum, f_hum = m.vout_hum
            vout = vout + a_hum * np.sin(2 * math.pi * f_hum * t + float(m.rng.uniform(0, 2 * math.pi)))
        return self._put(vout + m.noise(n), vin + m.noise(n), rate, 0, False)

    def _dio_capture(self, rate, n, dio_trigger, pre, stimulus, force) -> Capture:
        """Enable / disable edge from the AD2's own DIO: VOUT and whatever the ref channel is on."""
        m = self.m
        pin, dedge = dio_trigger
        before = bool(m.dio.get(pin, False))
        on_before = m.enabled and m.vin > 0
        stimulus()
        after = bool(m.dio.get(pin, False))
        on_after = m.enabled and m.vin > 0
        moved = (not before and after) if dedge == "rising" else (before and not after)
        if not moved and not force:
            raise InstrumentError(f"trigger timeout: DIO {pin} had no {dedge} edge")
        i0 = round(pre * n)
        t = (np.arange(n) - i0) / rate
        sig = m.startup_signals(t, on_before, on_after)
        kind = m.ref_probe[0]
        # current probes read the sense-resistor voltage, I * R
        ref = (
            sig[kind] * (m.sense_ohm if kind in ("iin", "iout") else 1.0)
            if kind in sig
            else np.full(n, m.ref_value())
        )
        vout = sig["vout"] + m.noise(n)
        return self._put(vout, ref + m.noise(n) * (0.05 if kind in ("iin", "iout") else 0.25), rate, i0, True)

    # --- DigitalOut ---------------------------------------------------------
    def write_pins(self, levels: dict[int, bool]) -> None:
        self.m.dio.update(levels)
        self._touched.update(levels)

    def release_all(self) -> None:
        for p in self._touched:
            self.m.dio[p] = False  # floating gate pulled down by its 10k
        self._touched.clear()

    # --- Wavegen ------------------------------------------------------------
    def set_dc(self, channel: int, volts: float) -> None:
        if abs(volts) > 5.0:
            raise InstrumentError("AD2 wavegen is limited to +/-5 V")
        self.m.awg_v[channel] = volts

    def set_sine(self, channel: int, *, frequency: float, low: float, high: float) -> None:
        self.set_wave(channel, shape="sine", frequency=frequency, low=low, high=high)

    def set_wave(self, channel: int, *, shape: WaveShape, frequency: float, low: float, high: float) -> None:
        if shape not in ("sine", "triangle", "square"):
            raise InstrumentError(f"unknown wavegen shape {shape!r}")
        if not (-5.0 <= low < high <= 5.0):
            raise InstrumentError("AD2 wavegen is limited to +/-5 V")
        self.m.awg_v.pop(channel, None)
        self.m.awg_wave[channel] = (shape, frequency, low, high)

    def set_vplus(self, volts: float | None) -> None:
        self.vplus = volts  # recorded only; nothing in the model depends on it

    def off(self, channel: int) -> None:
        self.m.awg_v.pop(channel, None)
        self.m.awg_wave.pop(channel, None)

    def close(self) -> None:
        pass


class SimDmm:
    """34401A stand-in: reads whatever dmm_probe says it's connected to.

    DC volts on VOUT / PSU get ~20 uV of noise; shunt and open readings use dmm_noise_v, and
    current mode (dmm_probe "series") reads the dmm_rail current with dmm_noise_a.
    """

    def __init__(self, model: LdoModel) -> None:
        self.m = model
        self.idn = "SIM,34401A,0,sim"
        self.mode = "dcv"
        self.range: float | None = None  # None = autorange
        self.last = 0.0

    def configure_dcv(self, *, range: float | None, nplc: float) -> None:
        self.mode = "dcv"
        self.range = range

    def dcv_range(self) -> float:
        """Range (V) for the last reading: the configured one, or the smallest that holds it (120 %)."""
        if self.range is not None:
            return self.range
        return next(r for r in (0.1, 1.0, 10.0, 100.0, 1000.0) if abs(self.last) <= 1.2 * r)

    def configure_dci(self, *, range: float | None, nplc: float) -> None:
        self.mode = "dci"

    def read(self) -> float:
        m = self.m
        if self.mode == "dci":  # only reads a current if it is in series with something
            i = m.supply_current(m.dmm_rail) if m.dmm_probe == "series" else 0.0
            return i + float(m.rng.normal(0.0, m.dmm_noise_a))
        return self.read_dcv()

    def read_dcv(self) -> float:
        self.last = self._read_dcv()
        return self.last

    def _read_dcv(self) -> float:
        m = self.m
        if m.dmm_probe == "open":  # leads not on anything
            return float(m.rng.normal(0.0, m.dmm_noise_v))
        if m.dmm_probe == "shunt":  # across R13
            v = m.sense_ohm * m.supply_current() + m.dmm_offset_v
            return v + float(m.rng.normal(0.0, m.dmm_noise_v))
        sources = {"vref": m.bgr_vref, "psu": m.psu_actual}  # anything else: VOUT
        v = sources.get(m.dmm_probe, m.vout_dc)()
        return v + float(m.rng.normal(0.0, 20e-6))

    def close(self) -> None:
        pass


class SimXScope:
    """54621A stand-in: a 2000-point resample of the sim AD2's last capture, on its own time axis."""

    def __init__(self, ad2: SimAnalogDiscovery) -> None:
        self.ad2 = ad2
        self.idn = "SIM,54621A,0,sim"
        self.model = "54621A"
        self._arm: dict | None = None
        self.busy = False

    def arm(self, **kw) -> None:
        self._arm = kw

    def collect(self, step_again=None, max_extra: int = 0, timeout: float = 60.0) -> dict:
        from ldo_bench.instruments.agilent546xx import ScopeTrace

        a, cap = self._arm, getattr(self.ad2, "last_capture", None)
        if a is None or cap is None:
            raise InstrumentError("sim xscope: nothing armed / captured")
        w, n = a["window"], a["points"]
        t = -a["pre_trigger"] * w + np.arange(n) * (w / n)
        t_src = cap.time
        vout_src = cap.channel(self.ad2.vout_channel)
        ref_src = cap.ch2 if self.ad2.vout_channel == 1 else cap.ch1
        lsb = a["vout_range"] / 256 / 16  # 8-bit, averaged: finer effective resolution
        v = np.round(np.interp(t, t_src, vout_src) / lsb) * lsb
        r = np.interp(t, t_src, ref_src)
        self._arm = None
        return {
            a["vout_channel"]: ScopeTrace(t, v, a["averages"]),
            a["ref_channel"]: ScopeTrace(t, r, a["averages"]),
        }

    def close(self) -> None:
        pass
