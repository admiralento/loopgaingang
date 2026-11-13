"""The bench: instruments plus the safety rules every procedure goes through.

Procedures never talk to the PSU or wavegen directly for voltages that reach
the DUT. They call `vin_from_psu`, `buffer_rail` or `vin_from_wavegen`, which
enforce the configured ceilings and ramp the supply. Leaving the `with Bench`
block for any reason, including an exception or Ctrl-C, runs `shutdown()`.

`open_bench` opens the real instruments (or the simulator with sim=True) and
returns a Bench; the CLI wraps it in `with`.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

import numpy as np

from ldo_bench.config import AD2_WAVEGEN_MAX_V, BenchConfig
from ldo_bench.instruments.base import (
    ChannelSetup,
    DcReading,
    InstrumentError,
    PowerSupply,
    SupplyOutput,
    Voltmeter,
    WaveShape,
)
from ldo_bench.loadbank import LoadBank

log = logging.getLogger(__name__)

SETPOINT_TOLERANCE = 1e-3  # V: the supply's setpoint readback must match what was programmed
VIN_SET_REL, VIN_SET_ABS = 0.02, 0.020  # measured VIN may be off its setpoint by 2 % + 20 mV
# AD2 VOUT may differ from the DMM's by 2 % + 50 mV: well above the AD2's DC error and a few mm of
# probe separation (1-8 mV on the bench), well below a probe that has come off VOUT (~1 V)
VOUT_AGREE_REL, VOUT_AGREE_ABS = 0.02, 0.050

# What the AD2 ref channel is wired to, for the simulator: (signal, DIO pin if signal is "dio" else 0).
RefProbe = tuple[Literal["vin", "dio", "vref", "iin", "iout", "en_ain"], int]
ConfirmFn = Callable[[str], bool]  # shows a prompt, returns True if the user answered yes


@dataclass(frozen=True)
class DcPoint:
    """One DC operating point. VOUT comes from the DMM when there is one, else the AD2.

    vout / vout_std: mean and standard deviation of VOUT from `source` (V).
    vin: VIN at the board (AD2 ref channel) or the PSU's readback, per `vin_source` (V).
    vout_ad2: VOUT on the AD2 as a cross-check (V); NaN when it wasn't measured.
    """

    vout: float
    vout_std: float
    vin: float
    vout_ad2: float
    source: Literal["dmm", "ad2"]
    dmm_range: float = float("nan")  # V: the DMM's range for this point (NaN: no DMM, or not reported)


class Bench:
    """The instruments plus the safety layer every procedure goes through.

    Procedures never program a voltage that reaches the DUT directly. They call:
      - `vin_from_psu`: PSU on VIN; ceiling min(dut.vin_max, psu.v_max).
      - `buffer_rail`: PSU on the BUF634 supply; ceiling psu.v_max.
      - `psu_output`: PSU with only a meter / the load bank on it; ceiling psu.v_max.
      - `vin_from_wavegen` / `vin_sine_from_wavegen`: wavegen on VIN through the BUF634;
        ceiling min(dut.vin_max, 5 V wavegen limit).
    A request above the ceiling raises InstrumentError before anything is programmed. The
    config checks the same limits up front; this is the second line of defence.

    The PSU is set up once (output off, current limit, OVP, range, 0 V) before its first use, and
    every change is ramped in steps of psu.ramp_step with a short pause between them, then
    psu.settle_time. The load bank refuses codes above dut.i_max (see LoadBank).

    External reference (`vref_external=True`, `--vref ext`, needs [vref]): the PSU's other output
    drives VREF_IN in place of the BGR (JP2 removed). It is set up with the supply and held at 0 V
    (OUTP switches both outputs together) until a procedure calls `reference_on()` with VIN up; that
    ramps it to vref.external_v and proves VOUT follows it. While it is on, VIN may not go more than
    vref.above_vin below it, and anything that would take VIN away (`supply_off`, turning off the
    wavegen that drives VIN) ramps the reference to 0 V first. `reference_on()` does nothing in the
    normal (BGR) mode, so procedures call it unconditionally.

    Enable from the AD2 (`enable_dio=True`, `--enable dio`): the DC and transient tests drive the LDO's
    enable through [enable] (DIO -> level shifters -> EN_AIN) instead of having it tied on with jumpers.
    They call `ldo_enable(False)` before VIN comes up, `ldo_enable(True)` once it is up, and
    `ldo_enable(False)` when they finish; with the enable tied on (the default) these do nothing.

    `shutdown()` (also run on leaving `with Bench`) turns things off in this order: external
    reference 0 V -> LDO enable low (--enable dio) -> wavegen (both channels) -> AD2 V+ -> load bank -> DIO released -> PSU output
    off and 0 V -> close the instruments. Signals into the board go first, so nothing drives an
    unpowered DUT.

    In simulation (`sim_model` set) `sleep` doesn't wait and wiring isn't asked about.
    `xscope_issue` holds why the optional cross-check scope is missing, if it was wanted.
    """

    def __init__(
        self,
        cfg: BenchConfig,
        psu: PowerSupply,
        ad2,  # implements Oscilloscope, DigitalOut and Wavegen
        *,
        dmm: Voltmeter | None = None,
        xscope=None,
        sim_model=None,
        assume_yes: bool = False,
        confirm: ConfirmFn | None = None,
        vref_external: bool = False,
        enable_dio: bool = False,
    ) -> None:
        if vref_external and cfg.vref is None:
            raise InstrumentError("an external reference needs a [vref] section in the config")
        if enable_dio:
            _check_enable_pins(cfg)
        self.cfg = cfg
        self.psu = psu
        self.dmm = dmm
        self.xscope = xscope  # optional 54621A cross-check scope for transients
        self.xscope_issue: str | None = None  # why it isn't available, if it was wanted
        # The AD2 plays three roles; separate names keep procedure code readable.
        self.scope = ad2
        self.dio = ad2
        self.awg = ad2
        self.sim_model = sim_model
        self.assume_yes = assume_yes
        self._confirm = confirm
        self.loadbank = LoadBank(cfg.loadbank, ad2, i_max=cfg.dut.i_max, vout=cfg.dut.vout_nominal)
        self._psu_ready = False  # current limit / OVP / range programmed
        self._psu_on = False  # output enabled
        self._awg_on = False
        self.shutdown_errors: list[str] = []  # steps of the last shutdown() that failed
        # external reference (see the class docstring)
        self.vref_external = vref_external
        self.vref_out: SupplyOutput | None = None  # the PSU output on VREF_IN, once set up
        self._vref_on = False  # driving vref.external_v (not held at 0 V)
        self.vref_proof: dict | None = None  # what reference_on() measured, for the run's records
        # what VIN is now (V) and what drives it, so the reference never sits above it
        self._vin_level: float | None = None
        self._vin_from: Literal["psu", "wavegen"] | None = None
        # enable from the AD2 (see the class docstring)
        self.enable_dio = enable_dio
        self._enable_driven = False  # V_33 on and the enable DIO driven by ldo_enable()
        self.last_wiring: list[str] = []  # the wiring lines last shown by confirm_wiring()

    @property
    def is_sim(self) -> bool:
        """True when running on the simulator (--sim)."""
        return self.sim_model is not None

    def __enter__(self) -> Bench:
        return self

    def __exit__(self, *exc) -> None:
        self.shutdown()

    def sleep(self, seconds: float) -> None:
        """Wait `seconds` (s) on real hardware; no-op in simulation so tests run fast."""
        if not self.is_sim and seconds > 0:
            time.sleep(seconds)

    def idns(self) -> dict[str, str]:
        """Identification string of each open instrument, for meta.json."""
        out = {"psu": self.psu.idn, "ad2": self.scope.idn}
        if self.dmm is not None:
            out["dmm"] = self.dmm.idn
        if self.xscope is not None:
            out["xscope"] = self.xscope.idn
        return out

    def vout_wiring(self) -> str:
        """Wiring line for whatever measures VOUT in the DC tests."""
        a = self.cfg.ad2
        line = f"Scope {a.vout_channel}+ -> VOUT at the output pins, {a.vout_channel}- -> GND"
        if self.dmm is not None:
            line += "; DMM V+/COM -> VOUT / GND at the output pins (DMM is the VOUT reference)"
        return line

    # --- Wiring -------------------------------------------------------------
    def confirm_wiring(
        self,
        title: str,
        lines: list[str],
        ref_probe: RefProbe,
        dmm_probe: Literal["vout", "psu", "shunt", "series"] = "vout",
        dmm_rail: str = "total",
        bank_on_psu: bool = False,
        psu_to: Literal["vin", "rail"] = "vin",
        ch1_probe: Literal["vout", "en_out"] = "vout",
        en_pin: int | None = None,
    ) -> None:
        """Show the wiring a test needs and wait for the user to confirm it.

        `title` and `lines` are what the user sees. The keyword arguments (ref_probe onwards)
        only tell the simulator how the bench is wired; on real hardware they do nothing.
        Not asked with --sim, --yes or no confirm function (the wiring is logged instead).
        Raises KeyboardInterrupt if the user answers no, so the run aborts like Ctrl-C.
        """
        if self.enable_dio:
            lines = [*lines, *self.enable_wiring()]
            if en_pin is None:  # the simulated LDO now follows the enable DIO
                en_pin = self.cfg.enable.dio_pin
        if self.sim_model is not None:
            self.sim_model.ref_probe = ref_probe
            self.sim_model.dmm_probe = dmm_probe
            self.sim_model.dmm_rail = dmm_rail
            self.sim_model.bank_on_psu = bank_on_psu
            self.sim_model.psu_to = psu_to
            self.sim_model.ch1_probe = ch1_probe
            self.sim_model.en_pin = en_pin
        if self.vref_external:
            lines = [*lines, *self.vref_wiring()]
            if self.sim_model is not None:  # the simulated operator followed the instructions
                self.sim_model.vref_wired = True
                self.sim_model.jp2_fitted = False
        self.last_wiring = list(lines)
        text = "\n".join([f"Wiring for {title}:", *(f"  - {s}" for s in lines)])
        if self.is_sim or self.assume_yes or self._confirm is None:
            log.info(text)  # not asking: still record what the run assumed
        elif not self._confirm(f"{text}\nIs the bench wired like this? (--yes skips this)"):
            raise KeyboardInterrupt("wiring not confirmed")
        if self.vref_external and not self.is_sim:
            self._confirm_jp2()

    def enable_wiring(self) -> list[str]:
        """Wiring lines for --enable dio (JAFWAK J13 / J6 / J8 labels)."""
        e = self.cfg.enable
        pins = f"AD2 DIO {e.dio_pin} -> J13 pin 12 (EN)"
        if e.dio_pin_n is not None:
            pins += f", DIO {e.dio_pin_n} -> J13 pin 10 (~EN)"
        v33 = "AD2 V+ -> J13 pin 14 (VIO)" if e.v33 is not None else "3.3 V on J13 pin 14 (VIO)"
        return [
            f"Enable from the AD2: {pins}; {v33}",
            'Enable path: J6 7-8 "EN" (EN_CTRL_18 -> EN_AIN), J8 1-2 and 9-10 "LDO" '
            "(EN_OUT -> EN_IN, ~EN_OUT -> ~EN_IN); nothing else on J6",
        ]

    def vref_wiring(self) -> list[str]:
        """Wiring lines for the external reference."""
        n = self.cfg.vref.psu_channel
        return [
            "JP2 (VREF) REMOVED: VREF_OUT (the BGR) must not be joined to VREF_IN",
            f"PSU OUT{n} + -> VREF_IN (JP2's VREF_IN pin), OUT{n} - -> GND "
            f"({self.cfg.vref.external_v} V, set by this run)",
        ]

    def _confirm_jp2(self) -> None:
        """Ask, every run and even with --yes, that JP2 is out: nothing can measure it beforehand."""
        n = self.cfg.vref.psu_channel
        question = (
            f"External reference: is JP2 removed, with PSU OUT{n} on VREF_IN? If JP2 is still fitted, "
            f"OUT{n} back-drives the BGR output. (Asked every run; --yes doesn't skip it.)"
        )
        if self._confirm is None:
            raise InstrumentError(
                "--vref ext needs a person to confirm JP2 is removed, and this run can't ask (no prompt)"
            )
        if not self._confirm(question):
            raise KeyboardInterrupt("JP2 removal not confirmed")

    # --- Supplies -----------------------------------------------------------
    def _ensure_psu(self) -> None:
        """First use only: output off, then current limit / OVP / range, then 0 V."""
        if self._psu_ready:
            return
        p = self.cfg.psu
        self.psu.output(False)
        self.psu.setup(current_limit=p.current_limit, ovp=p.ovp, voltage_range=p.voltage_range)
        self.psu.set_voltage(0.0)
        if self.vref_external:
            vr = self.cfg.vref
            if not hasattr(self.psu, "aux"):
                raise InstrumentError(f"the supply ({self.psu.idn}) has no second output for the reference")
            self.vref_out = self.psu.aux(vr.psu_channel)
            self.vref_out.setup(current_limit=vr.current_limit, ovp=vr.ovp)
        self._psu_ready = True

    def _psu_ramp(self, target: float, limit: float) -> None:
        """Ramp the PSU from its present setpoint to `target` (V), refusing anything above `limit` (V)."""
        if target < 0 or target > limit + 1e-9:
            raise InstrumentError(f"refusing to program the PSU to {target} V (limit {limit} V)")
        self._ensure_psu()
        if not self._psu_on:  # the output always turns on at 0 V and ramps up from there
            self.psu.set_voltage(0.0)
            self.psu.output(True)
            self._psu_on = True
        v = self.psu.get_voltage_setpoint()
        step = self.cfg.psu.ramp_step
        while abs(target - v) > 1e-9:
            v = min(target, v + step) if target > v else max(target, v - step)
            self.psu.set_voltage(v)
            if abs(target - v) > 1e-9:
                self.sleep(0.02)
        # Confirm the supply took it: a lost or rejected command would leave VIN somewhere else
        # while the data records the requested value.
        got = self.psu.get_voltage_setpoint()
        if abs(got - target) > SETPOINT_TOLERANCE:
            raise InstrumentError(
                f"the PSU setpoint reads {got:.4f} V after programming {target:.4f} V: the supply didn't "
                "take the command (check the connection)"
            )
        self.sleep(self.cfg.psu.settle_time)

    def supply_off(self) -> None:
        """Make sure the PSU output is off now (setting the supply up first if this run hasn't).

        For readings that must have nothing powered, e.g. iq's zero: the supply may have been left
        on from its front panel.
        """
        self._ensure_psu()
        self.reference_off()
        self.psu.output(False)
        self._psu_on = False
        self._vin_level, self._vin_from = None, None

    def vin_from_psu(self, volts: float) -> None:
        """PSU drives the LDO's VIN directly: ramp to `volts` (V), at most min(dut.vin_max, psu.v_max)."""
        self._vref_allows_vin(volts)
        self._psu_ramp(volts, min(self.cfg.dut.vin_max, self.cfg.psu.v_max))
        self._vin_level, self._vin_from = volts, "psu"

    def psu_output(self, volts: float) -> None:
        """PSU with nothing but a meter on it (psu-check): only psu.v_max applies."""
        self._no_reference("psu_output")
        self._psu_ramp(volts, self.cfg.psu.v_max)

    def buffer_rail(self, volts: float) -> None:
        """PSU drives the BUF634 supply (VS_BUFF / VDD_BUF) during line transient and PSRR.

        Only psu.v_max applies: the buffer rail doesn't reach VIN (the wavegen sets VIN). Refused
        while the external reference is on (lowering the rail could pull VIN below it).
        """
        self._no_reference("buffer_rail")
        self._psu_ramp(volts, self.cfg.psu.v_max)

    def vin_from_wavegen(self, volts: float) -> None:
        """Wavegen drives VIN through the BUF634: a DC level of `volts` (V), at most
        min(dut.vin_max, 5 V). The buffer rail must already be up (buffer_rail)."""
        limit = min(self.cfg.dut.vin_max, AD2_WAVEGEN_MAX_V)
        if not 0 <= volts <= limit:
            raise InstrumentError(f"refusing to drive VIN to {volts} V from the wavegen (limit {limit} V)")
        self._vref_allows_vin(volts)
        self.awg.set_dc(self.cfg.ad2.wavegen_channel, volts)
        self._awg_on = True
        self._vin_level, self._vin_from = volts, "wavegen"

    def vin_sine_from_wavegen(self, *, dc: float, amplitude: float, frequency: float) -> None:
        """Wavegen sine (dc +/- amplitude) onto VIN through the BUF634 (PSRR). Its peak obeys dut.vin_max.

        dc, amplitude in V (amplitude = peak, not peak-to-peak); frequency in Hz.
        """
        limit = min(self.cfg.dut.vin_max, AD2_WAVEGEN_MAX_V)
        lo, hi = dc - amplitude, dc + amplitude
        if amplitude <= 0 or lo < 0 or hi > limit + 1e-9:
            raise InstrumentError(
                f"refusing to drive VIN with a {lo:.3f}..{hi:.3f} V sine from the wavegen (limit 0..{limit} V)"
            )
        self._vref_allows_vin(lo)
        self.awg.set_sine(self.cfg.ad2.wavegen_channel, frequency=frequency, low=lo, high=hi)
        self._awg_on = True
        self._vin_level, self._vin_from = lo, "wavegen"

    def enable_logic(self, on: bool) -> None:
        """AD2 V+ on V_33 (the enable level shifters' 3.3 V side), if the config says the AD2 supplies it.

        on=True sets V+ to enable.v33; on=False turns V+ off. No-op if enable.v33 is None.
        """
        v = self.cfg.enable.v33
        if v is not None and hasattr(self.scope, "set_vplus"):
            self.scope.set_vplus(v if on else None)

    def set_enable(self, on: bool) -> None:
        """Drive EN_CTRL_33 (and ~EN_CTRL_33 inverted, if wired) from the AD2."""
        e = self.cfg.enable
        levels = {e.dio_pin: on}
        if e.dio_pin_n is not None:
            levels[e.dio_pin_n] = not on
        self.dio.write_pins(levels)

    def ldo_enable(self, on: bool) -> None:
        """--enable dio: drive the LDO's enable through [enable]; does nothing when EN is tied on.

        The first call turns on V_33 (AD2 V+) for the level shifters. on=True then waits
        enable.settle_time (s) for the LDO to start. Call with on=False before VIN comes up, so EN
        is held low (not floating) while it rises.
        """
        if not self.enable_dio:
            return
        if not self._enable_driven:
            self.enable_logic(True)
            self._enable_driven = True
        self.set_enable(on)
        if on:
            self.sleep(self.cfg.enable.settle_time)

    def en_ain_sine(
        self, channel: int, *, frequency: float, low: float, high: float, shape: WaveShape = "sine"
    ) -> None:
        """Wavegen sine or triangle (`shape`, low..high V, Hz) into the enable block's EN_AIN (enable-sine).

        EN_AIN is powered from VIN (EN_VDD), so the wave's top is held to dut.vin_max (and the
        wavegen's 5 V); its bottom may go to -0.3 V, the usual input clamp limit. Both shapes
        peak exactly at low and high, so the same limits apply to each.
        """
        limit = min(self.cfg.dut.vin_max, AD2_WAVEGEN_MAX_V)
        if not -0.3 <= low < high <= limit + 1e-9:
            raise InstrumentError(
                f"refusing to drive EN_AIN with a {low:.3f}..{high:.3f} V {shape} (limit -0.3..{limit} V)"
            )
        self.awg.set_wave(channel, shape=shape, frequency=frequency, low=low, high=high)
        self._awg_on = True

    def wavegen_off(self, channel: int | None = None) -> None:
        """Turn off a wavegen channel (default ad2.wavegen_channel).

        If that channel drives VIN (through the buffer), the external reference goes to 0 V first.
        """
        ch = self.cfg.ad2.wavegen_channel if channel is None else channel
        drives_vin = ch == self.cfg.ad2.wavegen_channel and self._vin_from == "wavegen"
        if drives_vin:
            self.reference_off()
        self.awg.off(ch)
        self._awg_on = False
        if drives_vin:
            self._vin_level, self._vin_from = 0.0, None

    # --- External reference (--vref ext) -----------------------------------
    def _no_reference(self, what: str) -> None:
        if self._vref_on:
            raise InstrumentError(f"refusing {what} while the external reference is on (reference_off first)")

    def _vref_allows_vin(self, vin: float) -> None:
        """Refuse a VIN (V) more than vref.above_vin below the external reference while it's on."""
        if not self._vref_on:
            return
        vr = self.cfg.vref
        if vin < vr.external_v - vr.above_vin - 1e-9:
            raise InstrumentError(
                f"refusing VIN = {vin:.3f} V with the external reference at {vr.external_v} V on VREF_IN "
                f"(VIN may be at most {vr.above_vin} V below it)"
            )

    def _vref_ramp(self, target: float) -> None:
        """Ramp the reference output to `target` (V) in psu.ramp_step steps, at most vref.v_max."""
        vr = self.cfg.vref
        if not 0 <= target <= vr.v_max + 1e-9:
            raise InstrumentError(f"refusing to program VREF_IN to {target} V (limit {vr.v_max} V)")
        out = self.vref_out
        v = out.get_voltage_setpoint()
        step = self.cfg.psu.ramp_step
        while abs(target - v) > 1e-9:
            v = min(target, v + step) if target > v else max(target, v - step)
            out.set_voltage(v)
        got = out.get_voltage_setpoint()
        if abs(got - target) > SETPOINT_TOLERANCE:
            raise InstrumentError(
                f"the reference output (OUT{out.channel}) reads {got:.4f} V after programming {target:.4f} V"
            )
        self.sleep(vr.settle_time)

    def reference_on(self) -> None:
        """External reference mode: ramp VREF_IN to vref.external_v and prove VOUT follows it.

        Call with VIN up and the LDO enabled. Does nothing in BGR mode, or if it's already on.
        The proof steps the reference down by vref.proof_step and back; VOUT (AD2 scope on VOUT)
        must move by at least vref.proof_follow of it. If it doesn't, the reference goes back to
        0 V and the run stops: JP2 is probably still fitted (the BGR holding VREF_IN), or the
        reference isn't wired to VREF_IN.
        """
        if not self.vref_external or self._vref_on:
            return
        vr = self.cfg.vref
        if self._vin_level is None or self.vref_out is None or not self._psu_on:
            raise InstrumentError(
                "the external reference needs VIN up first (it would drive an unpowered chip)"
            )
        if vr.external_v > self._vin_level + vr.above_vin + 1e-9:
            raise InstrumentError(
                f"refusing the external reference at {vr.external_v} V with VIN at {self._vin_level:.3f} V "
                f"(at most vref.above_vin = {vr.above_vin} V above VIN)"
            )
        log.info("External reference: OUT%d -> %.4f V on VREF_IN ...", self.vref_out.channel, vr.external_v)
        self._vref_ramp(vr.external_v)
        self._vref_on = True
        v_hi = self._vout_now()
        self._vref_ramp(vr.external_v - vr.proof_step)
        v_lo = self._vout_now()
        self._vref_ramp(vr.external_v)
        moved = v_hi - v_lo
        self.vref_proof = {
            "vout_at_external_v": v_hi,
            "vout_at_step_v": v_lo,
            "step_v": vr.proof_step,
            "vout_moved_v": moved,
        }
        if moved < vr.proof_follow * vr.proof_step:
            self.reference_off()
            raise InstrumentError(
                f"VOUT moved {moved * 1e3:.1f} mV when the external reference stepped {vr.proof_step * 1e3:.0f} mV "
                f"(needs {vr.proof_follow:.0%}): the reference isn't setting VOUT. Stopping: is JP2 still "
                f"fitted (then OUT{self.vref_out.channel} is back-driving the BGR), is OUT{self.vref_out.channel} "
                "on VREF_IN, and is the LDO enabled?"
            )
        log.info(
            "External reference proven: VOUT moved %.1f mV for a %.0f mV step",
            moved * 1e3,
            vr.proof_step * 1e3,
        )

    def reference_off(self) -> None:
        """Ramp the external reference to 0 V (it stays connected, held at 0 V). Safe in any mode."""
        if self.vref_out is None:
            return
        if self._psu_on:
            self._vref_ramp(0.0)
        else:
            self.vref_out.set_voltage(0.0)
        self._vref_on = False

    def _vout_now(self) -> float:
        """VOUT (V) on the AD2's VOUT channel, averaged over 0.1 s."""
        vout, _ = self.measure_dc(0.1)
        return vout.mean

    def vref_info(self) -> dict:
        """What drove VREF_IN in this run, for summary.json / meta.json."""
        vr = self.cfg.vref
        info: dict = {"source": "external" if self.vref_external else "bgr"}
        if vr is not None and vr.bgr_measured is not None:
            info["bgr_measured_v"] = vr.bgr_measured
        if self.vref_external:
            info["external_v"] = vr.external_v
            info["psu_channel"] = vr.psu_channel
            if self.vref_proof is not None:
                info["proof"] = self.vref_proof
        return info

    # --- Measurements -------------------------------------------------------
    def scope_setups(self) -> dict[int, ChannelSetup]:
        """AD2 range / offset per scope channel, from [ad2.vout] and [ad2.ref]."""
        a = self.cfg.ad2
        return {
            a.vout_channel: ChannelSetup(a.vout.range, a.vout.offset),
            a.ref_channel: ChannelSetup(a.ref.range, a.ref.offset),
        }

    def measure_dc(self, duration: float) -> tuple[DcReading, DcReading]:
        """Return (vout, ref) from the AD2, averaged over `duration` (s)."""
        r = self.scope.measure_dc(duration, self.scope_setups())
        return r[self.cfg.ad2.vout_channel], r[self.cfg.ad2.ref_channel]

    def vout_issue(self, vout: float, *, source: str) -> str | None:
        """A setup problem if VOUT isn't near vout_nominal (probe not on VOUT, LDO off, wrong board).

        Tests keep running and record the real data; the issue goes on the plot and in the report so
        a wrong setup is obvious, instead of the data being analysed as if it were VOUT.
        """
        nom, tol = self.cfg.dut.vout_nominal, self.cfg.dut.vout_tolerance
        if abs(vout - nom) <= tol * nom:
            return None
        return (
            f"VOUT reads {vout:.4f} V on the {source}, but {self.cfg.dut.name} should be at {nom} V "
            f"(+/-{tol:.0%}): is the {source} on VOUT / GND, the LDO enabled and VIN applied?"
        )

    def vout_agreement_issue(self, p: DcPoint) -> str | None:
        """A setup problem if the AD2's VOUT and the DMM's disagree by more than 2 % + 50 mV.

        Only when VOUT came from the DMM and the AD2 also read it (p.vout_ad2 finite). The DMM's own
        reading is checked by vout_issue; this catches AD2 scope 1 off VOUT while the DMM is on it
        (bench, 2026-11-10), which would leave the scope wiring the transient tests rely on unchecked.
        """
        if p.source != "dmm" or not np.isfinite(p.vout_ad2):
            return None
        if abs(p.vout_ad2 - p.vout) <= VOUT_AGREE_REL * abs(p.vout) + VOUT_AGREE_ABS:
            return None
        ch = self.cfg.ad2.vout_channel
        return (
            f"VOUT on AD2 scope {ch} ({p.vout_ad2:.4f} V) disagrees with the DMM ({p.vout:.4f} V) by more "
            f"than {VOUT_AGREE_REL:.0%} + {VOUT_AGREE_ABS * 1e3:.0f} mV: is scope {ch} on VOUT / GND?"
        )

    def vin_issue(self, vin: float, setpoint: float, *, source: str) -> str | None:
        """A setup problem if VIN measured at the board is off its setpoint by more than
        VIN_SET_TOLERANCE (2 % + 20 mV, which allows for cable drop under load).

        Catches a VIN probe on the wrong node, a supply in current limit, or a lost command: the
        data would otherwise be analysed against a VIN axis that isn't the LDO's input.
        """
        tol = VIN_SET_REL * setpoint + VIN_SET_ABS
        if abs(vin - setpoint) <= tol:
            return None
        return (
            f"VIN at the board reads {vin:.4f} V on the {source} with the PSU set to {setpoint:.4f} V "
            f"(more than {tol * 1e3:.0f} mV off): is the {source} on VIN at the LDO input, and is the "
            "supply in current limit?"
        )

    def measure_point(self, duration: float, vin_source: Literal["ad2", "psu"] = "ad2") -> DcPoint:
        """VOUT from the DMM if present (more accurate), else the AD2.

        VIN from the AD2 at the board (`vin_source="ad2"`), or from the PSU's own
        readback (`"psu"`), which needs no scope wiring but includes cable drop
        and the PSU's readback error (~1-3 mV on the bench E3648A).
        `duration` (s) is the AD2 averaging window; with the DMM, dmm.samples readings are averaged.
        """
        if vin_source == "psu" and self.dmm is not None:  # the AD2 isn't needed at all
            vin_psu = self.psu.measure_voltage()
            n = self.cfg.dmm.samples if self.cfg.dmm else 1
            x = np.array([self.dmm.read_dcv() for _ in range(n)])
            return DcPoint(float(x.mean()), float(x.std()), vin_psu, float("nan"), "dmm", self._dmm_range())
        vout_ad2, vin = self.measure_dc(duration)
        if vin_source == "psu":
            vin = DcReading(self.psu.measure_voltage(), 0.0, 1)
        if self.dmm is None:
            return DcPoint(vout_ad2.mean, vout_ad2.std, vin.mean, vout_ad2.mean, "ad2")
        n = self.cfg.dmm.samples if self.cfg.dmm else 1
        x = np.array([self.dmm.read_dcv() for _ in range(n)])
        return DcPoint(float(x.mean()), float(x.std()), vin.mean, vout_ad2.mean, "dmm", self._dmm_range())

    def _dmm_range(self) -> float:
        """The DMM's DC volts range (V) after a reading, so a sweep shows where autorange switched."""
        return float(self.dmm.dcv_range()) if hasattr(self.dmm, "dcv_range") else float("nan")

    # --- Shutdown -----------------------------------------------------------
    def shutdown(self) -> None:
        """Reference 0 V, wavegen off, AD2 V+ off, load off, DIO released, PSU off, close. Each step runs even if an
        earlier one fails (failures are logged). Safe to call more than once."""
        steps: list[tuple[str, Callable[[], None]]] = [
            ("external reference 0 V", lambda: self.vref_out.set_voltage(0.0) if self.vref_out else None),
            ("LDO enable low", lambda: self.set_enable(False) if self._enable_driven else None),
            ("wavegen off", lambda: self.awg.off(self.cfg.ad2.wavegen_channel)),
            ("wavegen 2 off", lambda: self.awg.off(2 if self.cfg.ad2.wavegen_channel == 1 else 1)),
            ("AD2 V+ off", lambda: self.scope.set_vplus(None) if hasattr(self.scope, "set_vplus") else None),
            ("load bank off", self.loadbank.off),
            ("DIO release", self.dio.release_all),
            ("PSU output off", lambda: self.psu.output(False)),
            ("PSU 0 V", lambda: self.psu.set_voltage(0.0)),
            ("close PSU", self.psu.close),
            ("close DMM", lambda: self.dmm.close() if self.dmm is not None else None),
            ("close cross-check scope", lambda: self.xscope.close() if self.xscope is not None else None),
            ("close AD2", self.scope.close),
        ]
        for name, fn in steps:
            try:
                fn()
            except Exception as e:  # keep going: the remaining steps matter more
                log.error("shutdown: %s failed: %s", name, e)
                self.shutdown_errors.append(f"{name}: {e}")
        self._psu_on = self._awg_on = self._psu_ready = self._vref_on = self._enable_driven = False
        self._vin_level, self._vin_from = None, None


def _check_enable_pins(cfg: BenchConfig) -> None:
    """--enable dio: the enable DIO pins mustn't also switch the load bank."""
    e = cfg.enable
    clash = ({e.dio_pin} | ({e.dio_pin_n} - {None})) & set(cfg.loadbank.dio_pins)
    if clash:
        raise InstrumentError(
            f"--enable dio: enable DIO pins {sorted(clash)} are also load bank pins {cfg.loadbank.dio_pins}"
        )


def open_bench(
    cfg: BenchConfig,
    *,
    sim: bool = False,
    psu_resource: str | None = None,
    ad_serial: str | None = None,
    assume_yes: bool = False,
    confirm: ConfirmFn | None = None,
    use_dmm: bool = True,
    use_xscope: bool = False,
    vref_external: bool = False,
    enable_dio: bool = False,
) -> Bench:
    """Open every instrument the config asks for and return a Bench (use it in a `with` block).

    sim: use the simulator instead of hardware (wiring is never asked about).
    psu_resource / ad_serial: override psu.resource / ad2.serial from the command line.
    use_dmm: open the [dmm] if it's enabled (False for --no-dmm and tests that don't use it).
    use_xscope: also open the [xscope] cross-check scope (only the transient tests use it). If it
    can't be opened, the run continues without it and Bench.xscope_issue says why.
    vref_external: drive VREF_IN from the PSU's other output ([vref]) instead of the BGR.
    enable_dio: the tests drive the LDO's enable from the AD2 ([enable]) instead of it being tied on.
    If any required instrument fails to open, the ones already opened are closed and the error
    is re-raised.
    """
    if vref_external and cfg.vref is None:  # before anything is opened
        raise InstrumentError("an external reference needs a [vref] section in the config")
    if enable_dio:
        _check_enable_pins(cfg)
    want_dmm = use_dmm and cfg.dmm is not None and cfg.dmm.enabled
    want_xscope = use_xscope and cfg.xscope is not None and cfg.xscope.enabled
    if sim:
        from ldo_bench.instruments.sim import LdoModel, SimAnalogDiscovery, SimPowerSupply

        lb = cfg.loadbank
        model = LdoModel(cfg.dut.vout_nominal, lb.resistors, lb.dio_pins, lb.active_high)
        from ldo_bench.instruments.sim import SimDmm, SimXScope

        sim_ad2 = SimAnalogDiscovery(model, cfg.ad2.vout_channel)
        return Bench(
            cfg,
            SimPowerSupply(model),
            sim_ad2,
            dmm=SimDmm(model) if want_dmm else None,
            xscope=SimXScope(sim_ad2) if want_xscope else None,
            sim_model=model,
            assume_yes=True,
            vref_external=vref_external,
            enable_dio=enable_dio,
        )

    from ldo_bench.instruments import discovery
    from ldo_bench.instruments.discovery import Finder, open_ad2, open_dmm, open_psu, open_xscope

    t0 = time.monotonic()
    log.info("Opening instruments (Ctrl-C to abort) ...")
    # One search shared by every instrument: each scan runs once, and nothing claimed is re-probed.
    finder = Finder(cfg.psu.gpib_address)
    psu = open_psu(
        psu_resource or cfg.psu.resource,
        gpib_address=cfg.psu.gpib_address,
        channel=cfg.psu.channel,
        serial=cfg.psu.serial_settings(),
        finder=finder,
    )
    opened: list = [psu]
    try:
        ad2 = open_ad2(ad_serial or cfg.ad2.serial)
        opened.append(ad2)
        dmm = None
        if want_dmm:
            dmm = open_dmm(cfg.dmm.resource, serial=cfg.dmm.serial_settings(), finder=finder)
            opened.append(dmm)
            log.info("Configuring the DMM ...")
            dmm.configure_dcv(range=cfg.dmm.range, nplc=cfg.dmm.nplc)
        xscope = None
        xscope_issue = None
        if want_xscope:
            xs = cfg.xscope
            if dmm is None and cfg.dmm is not None and not xs.resource:
                # The scope search probes every free serial port at 8N1; a 34401A set to 7E2 hears
                # garbage, logs an error and beeps. Find its port first, with its own settings.
                discovery.reserve_dmm_port(cfg.dmm.resource, serial=cfg.dmm.serial_settings(), finder=finder)
            try:
                xscope = open_xscope(xs.resource, serial=xs.serial_settings(), finder=finder)
                opened.append(xscope)
            except InstrumentError as e:  # optional: the AD2 measurement goes ahead without it
                xscope_issue = f"cross-check scope not available: {e}"
                log.warning("%s (continuing with the AD2 only; --no-xscope hides this)", xscope_issue)
    except BaseException:
        for inst in reversed(opened):
            try:
                inst.close()
            except Exception as e:
                log.debug("close after failed open: %s", e)
        raise
    log.info("Instruments ready (%.1f s)", time.monotonic() - t0)
    bench = Bench(
        cfg,
        psu,
        ad2,
        dmm=dmm,
        xscope=xscope,
        assume_yes=assume_yes,
        confirm=confirm,
        vref_external=vref_external,
        enable_dio=enable_dio,
    )
    bench.xscope_issue = xscope_issue
    return bench
