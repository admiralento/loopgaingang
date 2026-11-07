"""The bench: instruments plus the safety rules every procedure goes through.

Procedures never talk to the PSU or wavegen directly for voltages that reach
the DUT. They call `vin_from_psu`, `buffer_rail` or `vin_from_wavegen`, which
enforce the configured ceilings and ramp the supply. Leaving the `with Bench`
block for any reason, including an exception or Ctrl-C, runs `shutdown()`.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

import numpy as np

from ldo_bench.config import AD2_WAVEGEN_MAX_V, BenchConfig
from ldo_bench.instruments.base import ChannelSetup, DcReading, InstrumentError, PowerSupply, Voltmeter
from ldo_bench.loadbank import LoadBank

log = logging.getLogger(__name__)

RefProbe = tuple[Literal["vin", "dio"], int]
ConfirmFn = Callable[[str], bool]


@dataclass(frozen=True)
class DcPoint:
    """One DC operating point. VOUT comes from the DMM when there is one, else the AD2."""

    vout: float
    vout_std: float
    vin: float
    vout_ad2: float
    source: Literal["dmm", "ad2"]


class Bench:
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
    ) -> None:
        self.cfg = cfg
        self.psu = psu
        self.dmm = dmm
        self.xscope = xscope  # optional 54621A cross-check scope for transients
        self.xscope_issue: str | None = None  # why it isn't available, if it was wanted
        self.scope = ad2
        self.dio = ad2
        self.awg = ad2
        self.sim_model = sim_model
        self.assume_yes = assume_yes
        self._confirm = confirm
        self.loadbank = LoadBank(cfg.loadbank, ad2, i_max=cfg.dut.i_max, vout=cfg.dut.vout_nominal)
        self._psu_ready = False
        self._psu_on = False
        self._awg_on = False

    @property
    def is_sim(self) -> bool:
        return self.sim_model is not None

    def __enter__(self) -> Bench:
        return self

    def __exit__(self, *exc) -> None:
        self.shutdown()

    def sleep(self, seconds: float) -> None:
        if not self.is_sim and seconds > 0:
            time.sleep(seconds)

    def idns(self) -> dict[str, str]:
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
        dmm_probe: Literal["vout", "psu"] = "vout",
        bank_on_psu: bool = False,
        psu_to: Literal["vin", "rail"] = "vin",
    ) -> None:
        """Show the wiring a test needs and wait for the user to confirm it.

        The keyword arguments only tell the simulator how the bench is wired.
        """
        if self.sim_model is not None:
            self.sim_model.ref_probe = ref_probe
            self.sim_model.dmm_probe = dmm_probe
            self.sim_model.bank_on_psu = bank_on_psu
            self.sim_model.psu_to = psu_to
        text = "\n".join([f"Wiring for {title}:", *(f"  - {s}" for s in lines)])
        if self.is_sim or self.assume_yes or self._confirm is None:
            log.info(text)  # not asking: still record what the run assumed
            return
        if not self._confirm(f"{text}\nIs the bench wired like this? (--yes skips this)"):
            raise KeyboardInterrupt("wiring not confirmed")

    # --- Supplies -----------------------------------------------------------
    def _ensure_psu(self) -> None:
        if self._psu_ready:
            return
        p = self.cfg.psu
        self.psu.output(False)
        self.psu.setup(current_limit=p.current_limit, ovp=p.ovp, voltage_range=p.voltage_range)
        self.psu.set_voltage(0.0)
        self._psu_ready = True

    def _psu_ramp(self, target: float, limit: float) -> None:
        if target < 0 or target > limit + 1e-9:
            raise InstrumentError(f"refusing to program the PSU to {target} V (limit {limit} V)")
        self._ensure_psu()
        if not self._psu_on:
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
        self.sleep(self.cfg.psu.settle_time)

    def vin_from_psu(self, volts: float) -> None:
        """PSU drives the LDO's VIN directly."""
        self._psu_ramp(volts, min(self.cfg.dut.vin_max, self.cfg.psu.v_max))

    def psu_output(self, volts: float) -> None:
        """PSU with nothing but a meter on it (psu-check): only psu.v_max applies."""
        self._psu_ramp(volts, self.cfg.psu.v_max)

    def buffer_rail(self, volts: float) -> None:
        """PSU drives the BUF634 supply (VS_BUFF / VDD_BUF) during line transient."""
        self._psu_ramp(volts, self.cfg.psu.v_max)

    def vin_from_wavegen(self, volts: float) -> None:
        """Wavegen drives VIN through the BUF634."""
        limit = min(self.cfg.dut.vin_max, AD2_WAVEGEN_MAX_V)
        if not 0 <= volts <= limit:
            raise InstrumentError(f"refusing to drive VIN to {volts} V from the wavegen (limit {limit} V)")
        self.awg.set_dc(self.cfg.ad2.wavegen_channel, volts)
        self._awg_on = True

    def wavegen_off(self) -> None:
        self.awg.off(self.cfg.ad2.wavegen_channel)
        self._awg_on = False

    # --- Measurements -------------------------------------------------------
    def scope_setups(self) -> dict[int, ChannelSetup]:
        a = self.cfg.ad2
        return {
            a.vout_channel: ChannelSetup(a.vout.range, a.vout.offset),
            a.ref_channel: ChannelSetup(a.ref.range, a.ref.offset),
        }

    def measure_dc(self, duration: float) -> tuple[DcReading, DcReading]:
        """Return (vout, ref) from the AD2, averaged over `duration`."""
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

    def measure_point(self, duration: float, vin_source: Literal["ad2", "psu"] = "ad2") -> DcPoint:
        """VOUT from the DMM if present (more accurate), else the AD2.

        VIN from the AD2 at the board (`vin_source="ad2"`), or from the PSU's own
        readback (`"psu"`), which needs no scope wiring but includes cable drop
        and the PSU's readback error (~1-3 mV on the bench E3648A).
        """
        if vin_source == "psu" and self.dmm is not None:
            vin_psu = self.psu.measure_voltage()
            n = self.cfg.dmm.samples if self.cfg.dmm else 1
            x = np.array([self.dmm.read_dcv() for _ in range(n)])
            return DcPoint(float(x.mean()), float(x.std()), vin_psu, float("nan"), "dmm")
        vout_ad2, vin = self.measure_dc(duration)
        if vin_source == "psu":
            vin = DcReading(self.psu.measure_voltage(), 0.0, 1)
        if self.dmm is None:
            return DcPoint(vout_ad2.mean, vout_ad2.std, vin.mean, vout_ad2.mean, "ad2")
        n = self.cfg.dmm.samples if self.cfg.dmm else 1
        x = np.array([self.dmm.read_dcv() for _ in range(n)])
        return DcPoint(float(x.mean()), float(x.std()), vin.mean, vout_ad2.mean, "dmm")

    # --- Shutdown -----------------------------------------------------------
    def shutdown(self) -> None:
        """Wavegen off, load off, PSU off, close. Each step runs even if an earlier one fails."""
        steps: list[tuple[str, Callable[[], None]]] = [
            ("wavegen off", lambda: self.awg.off(self.cfg.ad2.wavegen_channel)),
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
        self._psu_on = self._awg_on = self._psu_ready = False


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
) -> Bench:
    """use_xscope: also open the [xscope] cross-check scope (only the transient tests use it)."""
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
        )

    from ldo_bench.instruments.discovery import open_ad2, open_dmm, open_psu, open_xscope

    t0 = time.monotonic()
    log.info("Opening instruments (Ctrl-C to abort) ...")
    psu = open_psu(
        psu_resource or cfg.psu.resource, gpib_address=cfg.psu.gpib_address, channel=cfg.psu.channel
    )
    opened: list = [psu]
    try:
        ad2 = open_ad2(ad_serial or cfg.ad2.serial)
        opened.append(ad2)
        dmm = None
        if want_dmm:
            dmm = open_dmm(cfg.dmm.resource, baud=cfg.dmm.baud, parity=cfg.dmm.parity)
            opened.append(dmm)
            log.info("Configuring the DMM ...")
            dmm.configure_dcv(range=cfg.dmm.range, nplc=cfg.dmm.nplc)
        xscope = None
        xscope_issue = None
        if want_xscope:
            xs = cfg.xscope
            used = frozenset(
                inst.t.name.removeprefix("serial:")
                for inst in opened
                if hasattr(getattr(inst, "t", None), "name")
            )
            try:
                xscope = open_xscope(xs.resource, baud=xs.baud, flow=xs.flow, skip=used)
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
    bench = Bench(cfg, psu, ad2, dmm=dmm, xscope=xscope, assume_yes=assume_yes, confirm=confirm)
    bench.xscope_issue = xscope_issue
    return bench
