"""iq: quiescent (enabled) and shutdown (disabled) supply current vs VIN, no load, measured by the DMM.

method "shunt": DMM DC volts across R13 (0.1 ohm between VIN and INB, J13.1 / J13.2), 100 mV range.
Everything the board draws from VIN goes through it: the LDO, the BGR, the enable block, the level
shifters' low side and the ~VOUT / 1 Mohm into the AD2's scope 1. At 10 NPLC (6.5 digits) that's
0.1 uV, i.e. 1 uA per count: fine for Iq, not for shutdown current (those points come out as "below
the floor"). Shorter integration times have coarser counts (RESOLUTION).

method "series": DMM DC amps (I / LO terminals) in place of one J1 jumper, which isolates that rail,
or between the PSU and VIN_DC (rail "total"). On a fixed range (10 mA unless dmm_range says otherwise,
never autorange, so the zero and the points share a range): 10 nA per count at 10 NPLC. The meter's
burden (~5 ohm on 10 mA) drops a little of that rail's voltage.

With the supply output switched off (it may have been left on), the DMM reads the zero (offset and thermal EMF), which is subtracted from
every point; the spread of those readings sets the floor. In the shunt method a known load is then
switched in once to prove the meter sees the current through R13.

run() steps:
1. Check the DMM driver can do the chosen method; confirm the wiring; configure the DMM (DC volts
   on 100 mV for shunt, DC amps for series).
2. Load bank and wavegen off, supply output off; read the zero. floor = max(3 x the zero's spread,
   one DMM count). The floor uses one reading's spread, though each point is a mean of `readings`
   minus a mean of `readings`, so it's conservative by sqrt(readings / 2).
3. If any "disabled" points are wanted: AD2 V+ on (V_33) and the LDO enabled.
4. Shunt load check (shunt method, check_code set, "enabled" in states): at vin_stop, read the
   current, switch in check_code, read again; the difference should be the bank's current at the
   measured VOUT.
5. For each state, for each VIN from vin_start to vin_stop: PSU to VIN, wait settle_time, read
   VOUT (AD2), the DMM current (zero subtracted), and the PSU's voltage and current readback.
6. Enable low, load bank off, then the checks below.

Checks (the data is never corrected):
- Setup issues (run invalid): reversed leads (the load check, or else the median enabled current,
  negative beyond the floor); the load check off by more than check_tolerance, or overloaded; a
  DMM overload (at the zero, every current is then NaN);
  enabled points with VOUT not near nominal or a current under the floor (meter shorted out or
  not in the path); disabled points with VOUT above 20 % of nominal (enable path not working).
- Notes (run still valid): the ~1 uA the AD2 scope draws from VOUT is included for the "total"
  and "vdd" rails; shutdown current under the floor.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np

from ldo_bench import plotting
from ldo_bench import wiring as w
from ldo_bench.analysis import sweep_points
from ldo_bench.bench import Bench
from ldo_bench.config import IqConfig
from ldo_bench.instruments.base import InstrumentError
from ldo_bench.procedures import INVALID

log = logging.getLogger(__name__)

NAME = "iq"
J1_RAIL = w.J1_RAILS  # rail -> (name, J1 pins)
# 34401A resolution (one count, x range) by integration time, from its manual's resolution table
# (recalled, not yet checked against the manual): 6.5 digits only at 10 NPLC and above.
RESOLUTION = {0.02: 1e-4, 0.2: 1e-5, 1: 3e-6, 10: 1e-6, 100: 3e-7}
SERIES_RANGE = 0.01  # A: the DCI range used unless iq.dmm_range says otherwise (fixed, never auto)
SCOPE_INPUT_OHM = 1e6  # AD2 scope input, loading VOUT


@dataclass
class IqPoint:
    """One VIN point in one enable state."""

    state: str  # "enabled" / "disabled"
    vin: float  # PSU readback (V)
    current: float  # A, zero subtracted (nan if the meter overloaded)
    std: float  # A, standard deviation of the DMM readings at this point
    vout: float  # VOUT on the AD2 (V)
    psu_current: float  # PSU readback (A): coarse, a cross-check only
    below_floor: bool  # |current| < the result's floor: not resolved, shown as "< floor"
    issues: list[str] = field(default_factory=list)  # setup problems at this point


@dataclass
class IqResult:
    """The whole iq run. `issues` = setup issues plus every point's issues (each listed once)."""

    method: str  # "shunt" or "series"
    rail: str  # series method: "total" or a J1 rail key ("bgr", "vdd", "en", "ota")
    sense_ohm: float  # ohm, R13 (shunt method)
    zero: float  # A, the DMM reading with the PSU off (subtracted)
    floor: float  # A, smallest current the setup resolves
    points: list[IqPoint]
    check: dict | None  # shunt load check: code, expected, measured (A)
    vout_nominal: float  # V
    setup_issues: list[str] = field(default_factory=list)  # problems not tied to one point
    notes: list[str] = field(default_factory=list)  # findings that don't invalidate the run

    @property
    def issues(self) -> list[str]:
        out = list(self.setup_issues)
        for p in self.points:
            out += [i for i in p.issues if i not in out]
        return out

    @property
    def states(self) -> list[str]:
        """The enable states measured, in run order."""
        return list(dict.fromkeys(p.state for p in self.points))

    @property
    def what(self) -> str:
        """What the current is, in words, for the report and plot title."""
        if self.method == "shunt":
            return f"input current across R13 ({self.sense_ohm:g} ohm)"
        if self.rail == "total":
            return "input current (DMM in series with VIN)"
        name, pins = J1_RAIL[self.rail]
        return f"{name} rail current (DMM in place of J1 {pins})"

    @property
    def rows(self) -> list[dict]:
        return [
            {
                "state": p.state,
                "vin_v": p.vin,
                "current_a": p.current,
                "current_std_a": p.std,
                "below_floor": p.below_floor,
                "floor_a": self.floor,
                "vout_v": p.vout,
                "psu_current_a": p.psu_current,
                "valid": not p.issues,
                "issues": " | ".join(p.issues),
            }
            for p in self.points
        ]

    def summary(self) -> dict:
        per_state = {}
        for s in self.states:
            pts = [p for p in self.points if p.state == s]
            cur = np.array([p.current for p in pts], float)
            ok = np.isfinite(cur)
            per_state[s] = {
                "vin": [p.vin for p in pts],
                "current_a": [p.current for p in pts],
                "below_floor": [p.below_floor for p in pts],
                "min_a": float(cur[ok].min()) if ok.any() else None,
                "max_a": float(cur[ok].max()) if ok.any() else None,
            }
        return {
            "valid": not self.issues,
            "issues": self.issues,
            "notes": self.notes,
            "method": self.method,
            "rail": self.rail,
            "measures": self.what,
            "zero_a": self.zero,
            "floor_a": self.floor,
            "load_check": self.check,
            "states": per_state,
        }

    def report(self) -> str:
        lines = [
            f"Quiescent current: {self.what}; zero {fmt_a(self.zero)} subtracted, floor {fmt_a(self.floor)}"
        ]
        if self.check:
            c = self.check
            lines.append(
                f"  load check: code {c['code']} should add {fmt_a(c['expected'])}, measured {fmt_a(c['measured'])}"
            )
        if self.issues:
            lines.append(f"  {INVALID}")
        for s in self.states:
            lines.append(f"  {s}:")
            lines.append("      VIN (V)      current      VOUT (V)")
            for p in (p for p in self.points if p.state == s):
                cur = f"< {fmt_a(self.floor)}" if p.below_floor else fmt_a(p.current)
                lines.append(f"      {p.vin:7.3f}  {cur:>12s}    {p.vout:8.4f}")
        lines += [f"  note: {n}" for n in self.notes]
        return "\n".join(lines)


def fmt_a(i: float) -> str:
    """A current in A as mA / uA / nA text; NaN (a DMM overload) prints as "overload"."""
    if not np.isfinite(i):
        return "overload"
    a = abs(i)
    if a >= 1e-3:
        return f"{i * 1e3:.3f} mA"
    if a >= 1e-6:
        return f"{i * 1e6:.2f} uA"
    return f"{i * 1e9:.1f} nA"


def wiring(bench: Bench, iq: IqConfig) -> list[str]:
    """Wiring lines for the chosen method / rail, one connection per line; the enable lines only if
    "disabled" is measured."""
    cfg = bench.cfg
    if iq.method == "shunt":
        lines = [
            *w.psu_on_vin(cfg),
            w.dmm("J13 pin 1 (INA)", "J13 pin 2 (INB)", "across R13; the DMM floats, so no ground lead"),
            *w.j1_rails(),
        ]
    elif iq.rail == "total":
        lines = [
            f"PSU OUT{cfg.psu.channel} + -> DMM I (3A/mA jack)",
            f"DMM LO (COM) -> {cfg.board.vin_dc} (the DMM in series with the supply)",
            f"PSU OUT{cfg.psu.channel} - -> {cfg.board.supply_gnd}",
            *w.fit(cfg.board.dc_jumpers),
            *w.j1_rails(),
        ]
    else:
        name, pins = w.J1_RAILS[iq.rail]
        a, b = pins.split("-")
        lines = [
            *w.psu_on_vin(cfg),
            f"Remove the J1 {pins} jumper ({name})",
            # current flows from INB (the higher pin) into the rail: the I jack goes on INB to read positive
            f"DMM I (3A/mA jack) -> J1 pin {b} (INB), DMM LO (COM) -> J1 pin {a} ({name}) (in place of the jumper)",
            *w.j1_rails(skip=iq.rail),
        ]
    lines.append(w.scope_vout(cfg, "VOUT check"))
    if iq.method == "shunt" and iq.check_code and "enabled" in iq.states:
        lines += w.load_bank(cfg)  # the load check switches in check_code once
    if "disabled" in iq.states:
        lines += w.enable_dio(cfg)
    return [*lines, w.ad2_gnd(cfg)]


def run(bench: Bench, iq: IqConfig) -> IqResult:
    """Measure supply current vs VIN in each state (see the module docstring for steps and checks).

    iq: the [iq] section. Raises InstrumentError if there is no DMM or its driver can't measure
    this way; setup problems found in the data are recorded, never raised.
    """
    if bench.dmm is None:
        raise InstrumentError("iq needs the DMM: enable [dmm] in the config and don't pass --no-dmm")
    dmm = bench.dmm
    if not hasattr(dmm, "read") or (iq.method == "series" and not hasattr(dmm, "configure_dci")):
        raise InstrumentError(f"the DMM ({dmm.idn}) driver can't do {iq.method} current measurements")
    cfg = bench.cfg
    nom = cfg.dut.vout_nominal
    shunt = iq.method == "shunt"
    use_enable = "disabled" in iq.states
    bench.confirm_wiring(
        "quiescent current",
        wiring(bench, iq),
        ref_probe=("vin", 0),
        dmm_probe="shunt" if shunt else "series",
        dmm_rail=iq.rail,
        en_pin=cfg.enable.dio_pin if use_enable else None,
    )
    nplc = iq.nplc or (cfg.dmm.nplc if cfg.dmm else 10)
    count = RESOLUTION[nplc]
    if shunt:
        rng = iq.dmm_range or 0.1
        dmm.configure_dcv(range=rng, nplc=nplc)
        # volts across R13 -> amps; one count of the range, in amps, is the best case floor
        scale, resolution = 1 / iq.sense_ohm, rng * count / iq.sense_ohm
    else:
        # A fixed range: with autorange the zero and the points could be read on different ranges.
        rng = iq.dmm_range or SERIES_RANGE
        dmm.configure_dci(range=rng, nplc=nplc)
        scale, resolution = 1.0, rng * count

    def read() -> tuple[float, float]:
        """(mean, std) of iq.readings DMM readings in A; (nan, nan) if any overloaded."""
        x = np.array([dmm.read() for _ in range(iq.readings)], float) * scale
        if not np.all(np.isfinite(x)):
            return float("nan"), float("nan")
        return float(x.mean()), float(x.std(ddof=1)) if len(x) > 1 else 0.0

    bench.loadbank.off()
    bench.wavegen_off()
    # The meter's offset / thermal EMF, with nothing flowing: the supply is switched off first, as
    # it may have been left on from its front panel.
    bench.supply_off()
    bench.sleep(iq.settle_time)
    log.info("Reading the DMM zero (PSU output off) ...")
    zero, zero_std = read()
    setup_issues: list[str] = []
    if not np.isfinite(zero):
        # no zero, so no current can be worked out: every point stays NaN (no stand-in value)
        setup_issues.append("the DMM overloads with the PSU off: check its function / leads")
    # 3 sigma of the zero's noise, never below one count
    floor = max(3 * zero_std, resolution) if np.isfinite(zero_std) else resolution
    log.info("zero %s, floor %s", fmt_a(zero), fmt_a(floor))

    if use_enable:
        # V_33 and EN high before VIN comes up, so the LDO starts enabled for the load check
        bench.enable_logic(True)
        bench.set_enable(True)

    if bench.vref_external:
        # --vref ext: the reference comes on (and is proven) with VIN up and the LDO enabled, before
        # the load check and the sweep
        bench.vin_from_psu(iq.vin_stop)
        bench.reference_on()

    # shunt: prove the meter sees the current through R13 by adding a known load once
    check = None
    if shunt and iq.check_code and "enabled" in iq.states:
        bench.vin_from_psu(iq.vin_stop)
        bench.sleep(iq.settle_time)
        i0, _ = read()
        bench.loadbank.set(iq.check_code)
        bench.sleep(iq.settle_time)
        i1, _ = read()
        vout, _ = bench.measure_dc(0.05)
        bench.loadbank.off()
        expected = bench.loadbank.current(iq.check_code, vout.mean)
        # the difference cancels the zero and the LDO's own current
        check = {
            "code": iq.check_code,
            "expected": expected,
            "measured": i1 - i0,
            "before": i0 - zero,  # the LDO's own current, zero subtracted
            "vout": vout.mean,
        }
        log.info("load check: expected +%s, measured %s", fmt_a(expected), fmt_a(i1 - i0))

    vins = sweep_points(iq.vin_start, iq.vin_stop, iq.vin_step)  # ends exactly on vin_stop
    points: list[IqPoint] = []
    for state in iq.states:
        if use_enable:
            bench.set_enable(state == "enabled")
        for v in vins:
            bench.vin_from_psu(float(v))
            bench.sleep(iq.settle_time)
            vout, _ = bench.measure_dc(0.05)
            i, s = read()
            vin_rb = bench.psu.measure_voltage()
            psu_i = bench.psu.measure_current()
            cur = i - zero
            points.append(IqPoint(state, vin_rb, cur, s, vout.mean, psu_i, False))
            log.info("%s VIN %.3f V: %s", state, vin_rb, fmt_a(cur))
    if use_enable:
        bench.set_enable(False)
    bench.loadbank.off()

    # Leads the other way round read negative. The data stays as measured (no sign flip): it's a
    # setup problem to fix, then run again.
    notes: list[str] = []
    if bench.vref_external:
        notes.append(
            "external reference: the current into VREF_IN comes from the reference output, not VIN, so "
            "it isn't included; the BGR is still powered from VIN and is"
        )
    ref = (
        check["measured"]
        if check
        else np.nanmedian([p.current for p in points if p.state == "enabled"] or [0])
    )
    reversed_leads = bool(np.isfinite(ref) and ref < -floor)
    if reversed_leads:
        setup_issues.append(
            "the DMM reads the current negative: its leads are reversed (swap HI / COM, or I / LO). "
            "Currents are saved as measured"
        )

    if check and not reversed_leads:
        c = check
        if not np.isfinite(c["measured"]):
            setup_issues.append(
                f"the DMM overloaded during the load check (code {c['code']}), so nothing proves it sees the "
                "current through R13: check its range and that it's on the V terminals"
            )
        elif abs(c["measured"] - c["expected"]) > iq.check_tolerance * c["expected"]:
            head = (
                f"switching in load code {c['code']} should add {fmt_a(c['expected'])} through R13, the DMM saw "
                f"{fmt_a(c['measured'])}"
            )
            if abs(c["measured"]) < 0.1 * c["expected"] and c["before"] > 3 * floor:
                # the DMM reads the LDO's own current, so it is across R13: the load never switched in
                bits = [
                    f"AD2 DIO {d} on {bit}"
                    for k, (d, bit) in enumerate(zip(cfg.loadbank.dio_pins, w.bank_bits(cfg), strict=True))
                    if c["code"] >> k & 1
                ]
                setup_issues.append(
                    f"{head}, though it reads {fmt_a(c['before'])} before it, so it is across R13 and the load "
                    f'didn\'t switch in: is J10 "LOAD" fitted, VS_BUFF (J4) at 5 V, and {" and ".join(bits)}?'
                )
            else:
                setup_issues.append(
                    f"{head}: are the DMM V / COM leads on J13.1 / J13.2, and is it on the V terminals (not I)? "
                    "If so, is the load switching in (J10 fitted, VS_BUFF at 5 V)?"
                )
    for p in points:
        if not np.isfinite(p.current):
            p.issues.append(f"the DMM overloaded at VIN {p.vin:.2f} V ({p.state}): raise iq.dmm_range")
            continue
        p.below_floor = abs(p.current) < floor
        if p.state == "enabled":
            up = bench.vout_issue(p.vout, source=f"scope {cfg.ad2.vout_channel}")
            if up:
                p.issues.append(f"enabled: {up}")
            if p.below_floor:
                hint = (
                    "are the DMM leads across R13?"
                    if shunt
                    else "is the DMM in series (jumper removed, I and LO terminals), not shorted by the jumper?"
                )
                p.issues.append(f"enabled current is below the DMM floor ({fmt_a(floor)}): {hint}")
        elif p.vout > 0.2 * nom:
            p.issues.append(
                f"VOUT is {p.vout:.3f} V while disabled: the enable path isn't turning the LDO off "
                f"(J6 7-8, J8 1-2 / 9-10, DIO {cfg.enable.dio_pin} on J13.12, V_33)"
            )
    if iq.rail in ("total", "vdd"):
        notes.append(
            f"includes ~{fmt_a(nom / SCOPE_INPUT_OHM)} into the AD2 scope on VOUT (1 Mohm); "
            "unplug it for the last microamp"
        )
    if any(p.below_floor for p in points if p.state == "disabled"):
        notes.append(
            f"shutdown current is below what this method resolves ({fmt_a(floor)})"
            + ("; the series method (DCI, 10 nA counts) resolves it" if shunt else "")
        )
    return IqResult(
        iq.method,
        iq.rail,
        iq.sense_ohm,
        zero,
        floor,
        points,
        check,
        nom,
        list(dict.fromkeys(setup_issues)),
        notes,
    )


def _unit(values: list[float]) -> tuple[float, str]:
    """(scale, unit) for plotting currents in A, from the largest finite magnitude."""
    v = [abs(x) for x in values if np.isfinite(x)]
    top = max(v) if v else 1e-6
    if top >= 1e-3:
        return 1e3, "mA"
    if top >= 1e-6:
        return 1e6, "µA"
    return 1e9, "nA"


def _vout_panel(ax, pts: list[IqPoint], state: str, nominal: float) -> None:
    """VOUT on the scope (OUTA) at each point of one state, against VIN.

    Enabled, VOUT near nominal shows the LDO regulated, so the rail under test was powered (through
    the meter, in the series method). Disabled, VOUT under 20 % of nominal shows the enable turned
    it off. A current that is small but real looks like this; an open meter or a dead enable doesn't.
    """
    color = plotting.SERIES[0] if state == "enabled" else plotting.SERIES[1]
    ax.plot([p.vin for p in pts], [p.vout for p in pts], "o-", color=color, ms=4, lw=1.2)
    level, label = (
        (nominal, f"nominal {nominal:g} V") if state == "enabled" else (0.2 * nominal, "20 % of nominal")
    )
    ax.axhline(level, color=plotting.INK_2, lw=0.9, ls=(0, (3, 2)))
    ax.annotate(
        label,
        (0.99, level),
        xycoords=("axes fraction", "data"),
        xytext=(0, 3),
        textcoords="offset points",
        ha="right",
        fontsize=7.5,
        color=plotting.INK_2,
    )
    ax.set_ylim(-0.05 * nominal, 1.25 * nominal)
    ax.set_ylabel("VOUT at OUTA (V)")
    ax.set_title(f"{state.capitalize()}: VOUT", fontsize=10, pad=6)


def plot(res: IqResult, dut: str):
    """Current vs VIN (left) and VOUT at OUTA vs VIN (right), one row per state. The shaded band is
    +/- the floor; points under it are hollow (not resolved). Issues go in a red box on the top panel.
    """
    states = res.states or ["enabled"]
    fig, axes = plotting.figure(
        len(states),
        2,
        size=(11.0, 3.0 * len(states) + 0.6),
        squeeze=False,
        sharex=True,
        gridspec_kw={"width_ratios": [1.6, 1]},
    )
    valid = not res.issues
    for row, state in enumerate(states):
        ax = axes[row][0]
        pts = [p for p in res.points if p.state == state]
        _vout_panel(axes[row][1], pts, state, res.vout_nominal)
        scale, unit = _unit([p.current for p in pts] + [res.floor])
        x = np.array([p.vin for p in pts])
        y = np.array([p.current for p in pts]) * scale
        e = np.array([p.std for p in pts]) * scale
        below = np.array([p.below_floor for p in pts], bool)
        color = plotting.SERIES[0] if state == "enabled" else plotting.SERIES[1]
        ax.axhspan(-res.floor * scale, res.floor * scale, color=plotting.GRID, alpha=0.6, lw=0, zorder=1)
        ax.annotate(
            f"below the DMM floor (±{fmt_a(res.floor).replace('uA', 'µA')})",
            (0.99, res.floor * scale),
            xycoords=("axes fraction", "data"),
            xytext=(0, 3),
            textcoords="offset points",
            ha="right",
            fontsize=7.5,
            color=plotting.INK_2,
        )
        ax.plot(x, y, color=color, lw=1.4, zorder=3)
        ax.errorbar(x[~below], y[~below], e[~below], fmt="o", ms=5, color=color, capsize=2, zorder=4)
        ax.plot(x[below], y[below], "o", ms=5, mfc=plotting.SURFACE, mec=color, zorder=4, label="below floor")
        if below.any():
            ax.legend(loc="lower right")
        ax.set_ylabel(f"{state.capitalize()} current ({unit})")
        ax.set_title(state.capitalize(), fontsize=10, pad=6)
        if row == 0 and not valid:
            plotting.issue_box(ax, res.issues)
    for ax in axes[-1]:
        ax.set_xlabel("VIN (V)")
    title = f"Quiescent current: {dut}  ·  {res.what}"
    plotting.suptitle(fig, title, invalid=not valid)
    return fig
