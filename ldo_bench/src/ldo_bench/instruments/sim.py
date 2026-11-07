"""Simulated bench: an LDO model plus fake PSU / AD2 that all share its state.

Used by `--sim` and by the test suite, so every procedure can run end to end
with no hardware attached. The model is deliberately simple but has the
features the analysis has to handle: output resistance, finite line
regulation, dropout, noise, an underdamped transient and a switching glitch.

What VIN is connected to follows the real board: if the wavegen is driving
(line transient, VIN through the BUF634) VIN follows W1; otherwise VIN follows
the PSU output.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field

import numpy as np

from ldo_bench.instruments.base import Capture, ChannelSetup, DcReading, Edge, InstrumentError


@dataclass
class LdoModel:
    vout_nominal: float
    resistors: list[float]
    dio_pins: list[int]
    active_high: bool = True
    r_out: float = 0.05  # ohm, DC load regulation
    line_reg: float = 0.5e-3  # V/V, DC line regulation
    dropout: float = 0.10  # V at zero load
    r_dropout: float = 1.0  # ohm, extra dropout per amp
    iq: float = 50e-6  # A
    noise_rms: float = 0.4e-3  # V
    # Transient: v(t) = A * exp(-t / tau) * cos(2 pi f t)
    z_peak: float = 0.4  # ohm, peak dip per amp of load step
    k_line: float = 0.05  # V/V, peak output kick per volt of line step
    tau: float = 4e-6
    f_ring: float = 150e3
    glitch: float = 0.05  # V, one-sample spike at the switching edge
    seed: int = 1234

    # bench state
    psu_v: float = 0.0
    psu_on: bool = False
    awg_v: dict[int, float] = field(default_factory=dict)  # enabled wavegen channels
    dio: dict[int, bool] = field(default_factory=dict)
    ref_probe: tuple[str, int] = ("vin", 0)  # what scope ch2 is wired to: ("vin", 0) or ("dio", pin)
    dmm_probe: str = "vout"  # what the DMM is across: "vout" or "psu" (the supply terminals)
    bank_on_psu: bool = False  # loadbank-check: the load bank straight across the PSU, no LDO
    psu_i_offset: float = 2.0e-3  # A, current readback offset (cancelled by zero-load subtraction)
    psu_offset: float = -1.0e-3  # V, sim supply output error: offset + gain, like the bench E3648A
    psu_gain: float = 4.5e-4
    psu_readback_gain: float = 5.5e-4  # readback reads high, ~2.7 mV at 5 V
    # enable block (EN_AIN -> Schmitt comparator -> EN_OUT) and turn-on / turn-off dynamics
    en_pin: int | None = None  # AD2 DIO driving EN_CTRL_33 -> enable path; None = always enabled
    en_vt_rise: float = 0.58  # fraction of EN_VDD (= VIN) where EN_OUT goes high
    en_vt_fall: float = 0.42
    en_t_rise: float = 30e-9  # EN_OUT edge time (s)
    vref_nominal: float = 1.2  # BGR output (V)
    vref_delay: float = 4e-6
    vref_tau: float = 8e-6
    vout_delay: float = 15e-6  # after VREF starts
    vout_tau: float = 35e-6
    vout_off_tau: float = 300e-6  # discharge with no load
    c_out: float = 10e-6  # output capacitance: inrush = C dV/dt
    iq_on: float = 150e-6  # quiescent current when enabled (A)
    sense_ohm: float = 0.1  # R13 / R1
    ch1_probe: str = "vout"  # what the VOUT channel is on: "vout" or "en_out"
    awg_sine: dict[int, tuple[float, float, float]] = field(default_factory=dict)  # ch -> (f, low, high)

    def __post_init__(self) -> None:
        self.rng = np.random.default_rng(self.seed)

    # --- DC model -----------------------------------------------------------
    buffer_offset: float = -0.015  # V, BUF634 output minus input (line tran calibrates it out)
    psu_to: str = "vin"  # where the PSU is wired: "vin" (DC tests) or "rail" (BUF634 supply, line tran)
    buffer_dropout: float = 1.5  # V, the BUF634 output can't get closer than this to its rail

    @property
    def vin(self) -> float:
        if self.psu_to == "rail":  # VIN = buffer output: W1 + offset, only while the rail is powered
            if not (self.psu_on and self.awg_v):
                return 0.0
            out = next(iter(self.awg_v.values())) + self.buffer_offset
            return max(0.0, min(out, self.psu_v - self.buffer_dropout))
        return self.psu_v if self.psu_on else 0.0

    def conductance(self) -> float:
        return sum(
            1.0 / r
            for pin, r in zip(self.dio_pins, self.resistors, strict=True)
            if self.dio.get(pin, not self.active_high) == self.active_high
        )

    @property
    def enabled(self) -> bool:
        return self.en_pin is None or bool(self.dio.get(self.en_pin, False))

    def vout_dc(self, vin: float | None = None, g: float | None = None) -> float:
        vin = self.vin if vin is None else vin
        g = self.conductance() if g is None else g
        if vin <= 0 or not self.enabled:
            return 0.0
        v = self.vout_nominal
        for _ in range(4):  # load current depends on vout; iterate to a fixed point
            i = v * g
            vreg = self.vout_nominal + self.line_reg * (vin - self.vout_nominal - 0.5) - self.r_out * i
            vdrop = vin - self.dropout - self.r_dropout * i
            v = max(0.0, min(vreg, vdrop))
        return v

    def psu_actual(self) -> float:
        """What is really on the supply terminals (sim includes a small output error)."""
        if not self.psu_on or self.psu_v == 0:
            return 0.0
        return self.psu_v * (1 + self.psu_gain) + self.psu_offset

    def load_current(self) -> float:
        return self.vout_dc() * self.conductance()

    def ref_value(self) -> float:
        kind, pin = self.ref_probe
        if kind == "dio":
            return 3.3 if self.dio.get(pin, False) else 0.0
        if kind == "vref":
            return self.vref_nominal if self.enabled and self.vin > 0 else 0.0
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
        saved = self.en_pin
        try:
            if enabled:
                self.en_pin = None
            return self.vout_dc() if enabled else 0.0
        finally:
            self.en_pin = saved

    def noise(self, n: int) -> np.ndarray:
        return self.rng.normal(0.0, self.noise_rms, n)


class SimPowerSupply:
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
        buffer_iq = 1.5e-3 if self.m.awg_v else 0.0
        return self.m.load_current() + self.m.iq + buffer_iq

    def close(self) -> None:
        pass


class SimAnalogDiscovery:
    """Implements Oscilloscope, DigitalOut and Wavegen, like the real AD2 driver."""

    def __init__(self, model: LdoModel, vout_channel: int = 1) -> None:
        self.m = model
        self.vout_channel = vout_channel
        self.idn = "SIM Analog Discovery 2 (SN:SIM000)"
        self._touched: set[int] = set()

    # --- Oscilloscope -------------------------------------------------------
    def _value(self, ch: int, vout: float, ref: float) -> float:
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
    ) -> Capture:
        m = self.m
        if m.awg_sine:
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
        di = v0 * g1 - v0 * g0  # load step (A), + = more load
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
        ch1, ch2 = (vout_side, ref_side) if self.vout_channel == 1 else (ref_side, vout_side)
        self.last_capture = Capture(rate, ch1, ch2, i0, edge_found=found)
        return self.last_capture

    def _sine_capture(self, rate, n, level, edge, pre, stimulus) -> Capture:
        """W1 sine on EN_AIN (scope ref channel), the enable comparator's EN_OUT on the other."""
        m = self.m
        stimulus()
        f, lo, hi = next(iter(m.awg_sine.values()))
        mid, amp = (hi + lo) / 2, (hi - lo) / 2
        i0 = round(pre * n)
        t = (np.arange(n) - i0) / rate
        ratio = float(np.clip((level - mid) / amp, -1, 1))
        phase = math.asin(ratio) if edge == "rising" else math.pi - math.asin(ratio)
        ain = mid + amp * np.sin(2 * math.pi * f * t + phase)
        vdd = m.vin
        hi_th, lo_th = m.en_vt_rise * vdd, m.en_vt_fall * vdd
        out = np.zeros(n)
        state = ain[0] > (hi_th + lo_th) / 2
        for k, a in enumerate(ain):
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
        if not (-5.0 <= low < high <= 5.0):
            raise InstrumentError("AD2 wavegen is limited to +/-5 V")
        self.m.awg_v.pop(channel, None)
        self.m.awg_sine[channel] = (frequency, low, high)

    def set_vplus(self, volts: float | None) -> None:
        self.vplus = volts

    def off(self, channel: int) -> None:
        self.m.awg_v.pop(channel, None)
        self.m.awg_sine.pop(channel, None)

    def close(self) -> None:
        pass


class SimDmm:
    """34401A stand-in: VOUT with ~20 uV of noise."""

    def __init__(self, model: LdoModel) -> None:
        self.m = model
        self.idn = "SIM,34401A,0,sim"

    def configure_dcv(self, *, range: float | None, nplc: float) -> None:
        pass

    def read_dcv(self) -> float:
        m = self.m
        v = m.psu_actual() if m.dmm_probe == "psu" else m.vout_dc()
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
