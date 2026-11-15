"""enable-current: the enable block's EN_VDD current against the frequency of a square on EN_AIN.

The block's static current (~0.3 nA in the PEX) is under the 34401A's 10 nA count, and the ~1.6 mA
spike at each threshold crossing is far too short for it. But every input cycle moves about the
same charge Q through the block (its nodes charging, shoot-through at each crossing), so its
average current is I_static + Q f: a DC current the DMM reads. Sweeping a square from 10 Hz to
1 MHz and fitting that line gives Q (pC per cycle) and the equivalent capacitance Q / VIN.

JAFWAK: AD2 W1 -> EN_FG SMA (J2) -> J6 5-6 -> R23 (1k) -> EN_AIN. The DMM (DC amps, fixed 10 mA
range: 10 nA counts at 10 NPLC) replaces the J1 9-11 jumper, so it carries only EN_VDD. J8 holds the
LDO off (EN_IN on GND, ~EN_IN on VIN), so EN_OUT drives nothing. Nothing may probe EN_OUT: an AD2
scope input (~24 pF) charged to 1.8 V a million times a second is ~43 uA from EN_VDD, more than the
block. The scope's ref channel watches EN_AIN (TP6) only, to prove the square arrives; that load is
driven by W1 through R23, not by EN_VDD.

run() steps:
1. Confirm the wiring; DMM to DC amps on the fixed range.
2. Load bank and wavegen off, supply output off: the DMM zero (subtracted from every point) and
   the floor (max of 3 x the zero's spread and one count).
3. W1 to DC `low` (EN_AIN held, never floating), VIN up: the static point.
4. For each frequency (1, 2, 5 per decade): W1 square low..high, wait settle_time, read the DMM,
   capture EN_AIN (4 cycles) for its swing.
5. W1 back to DC `low`. Fit I = I0 + Q f to the points above the floor.

Checks (the data is never corrected):
- Setup issues (run invalid): a DMM overload (at the zero every current is NaN); EN_AIN not
  swinging like the square (scope not on TP6, J6 not at 5-6, W1 not on EN_FG); the current at the
  top frequency not above the static point by more than the floor (the DMM not in the path, or the
  J1 9-11 jumper left fitted beside it).
- Notes (run still valid): points under the floor (reported as "< floor").
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np

from ldo_bench import plotting
from ldo_bench import wiring as w
from ldo_bench.bench import Bench
from ldo_bench.config import AD2_MAX_SAMPLE_RATE, EnCurrentConfig
from ldo_bench.instruments.base import ChannelSetup, InstrumentError
from ldo_bench.procedures import INVALID
from ldo_bench.procedures.iq import RESOLUTION, fmt_a

log = logging.getLogger(__name__)

NAME = "enable-current"
SWING_SAMPLES = 2048  # samples per EN_AIN capture (4 cycles)


def fmt_hz(f: float) -> str:
    """A frequency (Hz) as Hz / kHz / MHz text."""
    for scale, unit in ((1e6, "MHz"), (1e3, "kHz")):
        if f >= scale:
            return f"{f / scale:g} {unit}"
    return f"{f:g} Hz"


@dataclass
class EnCurrentPoint:
    """One point: the square at `frequency` (0 = EN_AIN held at DC `low`, the static point)."""

    frequency: float  # Hz
    current: float  # A, zero subtracted (nan if the meter overloaded)
    std: float  # A, standard deviation of the DMM readings
    swing: float  # V, EN_AIN's measured swing (2nd-98th percentile); nan for the static point
    below_floor: bool = False


@dataclass
class EnCurrentResult:
    """The sweep, the static point and the fit."""

    vin: float  # V, PSU setpoint (EN_VDD)
    low: float  # V, square levels
    high: float
    zero: float  # A, DMM reading with the supply off (subtracted)
    floor: float  # A, smallest current resolved
    static: EnCurrentPoint
    points: list[EnCurrentPoint]
    charge: float  # C per input cycle, from the fit (nan if under two points resolved)
    static_fit: float  # A, the fit's intercept
    issues: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def frequencies(self) -> list[float]:
        return [p.frequency for p in self.points]

    @property
    def static_current(self) -> float:
        return self.static.current

    def _cur(self, p: EnCurrentPoint) -> str:
        return f"< {fmt_a(self.floor)}" if p.below_floor else fmt_a(p.current)

    @property
    def rows(self) -> list[dict]:
        return [
            {
                "frequency_hz": p.frequency,
                "current_a": p.current,
                "current_std_a": p.std,
                "below_floor": p.below_floor,
                "floor_a": self.floor,
                "en_ain_swing_v": p.swing,
            }
            for p in (self.static, *self.points)
        ]

    def summary(self) -> dict:
        ok = np.isfinite(self.charge)
        return {
            "valid": not self.issues,
            "issues": self.issues,
            "notes": self.notes,
            "vin": self.vin,
            "square_low": self.low,
            "square_high": self.high,
            "zero_a": self.zero,
            "floor_a": self.floor,
            "static_a": self.static.current,
            "static_below_floor": self.static.below_floor,
            "charge_per_cycle_c": float(self.charge) if ok else None,
            "equivalent_capacitance_f": float(self.charge / self.vin) if ok else None,
            "frequency_hz": self.frequencies,
            "current_a": [p.current for p in self.points],
            "below_floor": [p.below_floor for p in self.points],
        }

    def report(self) -> str:
        lines = [
            f"Enable block switching current: EN_VDD (DMM in place of J1 9-11), {self.low:.2f}-{self.high:.2f} V "
            f"square on EN_AIN, VIN {self.vin:.2f} V; zero {fmt_a(self.zero)} subtracted, floor {fmt_a(self.floor)}",
        ]
        if self.issues:
            lines.append(f"  {INVALID}")
        lines.append(f"  static (EN_AIN at {self.low:g} V DC): {self._cur(self.static)}")
        lines.append("      frequency       current    EN_AIN swing")
        for p in self.points:
            lines.append(f"      {fmt_hz(p.frequency):>9s}  {self._cur(p):>12s}    {p.swing:6.3f} V")
        if np.isfinite(self.charge):
            lines.append(
                f"  charge per cycle Q = {self.charge * 1e12:.2f} pC per cycle (equivalent C = Q / VIN = "
                f"{self.charge / self.vin * 1e12:.2f} pF); fit I = {fmt_a(self.static_fit)} + Q f"
            )
        else:
            lines.append("  charge per cycle: not fitted (fewer than two points above the floor)")
        lines += [f"  note: {n}" for n in self.notes]
        return "\n".join(lines)


def wiring(bench: Bench, ec: EnCurrentConfig) -> list[str]:
    """Wiring lines shown before the run, one connection per line (JAFWAK)."""
    cfg = bench.cfg
    return [
        *w.psu_on_vin(cfg),
        "Remove the J1 9-11 jumper (EN_VDD)",
        "DMM I (3A/mA jack) -> J1 pin 11 (INB), DMM LO (COM) -> J1 pin 9 (EN_VDD) (in place of the jumper)",
        *w.j1_rails(skip="en"),
        'Fit J8 5-6 (EN_IN = GND; misprinted "HI"): LDO held off',
        'Fit J8 13-14 "HI" (~EN_IN = VIN)',
        'Fit J6 5-6 "FG" (EN_FG -> EN_AIN; nothing else on J6)',
        f"AD2 W{ec.wavegen_channel} -> EN_FG (J2)",
        w.scope(cfg, "ref", "EN_AIN (TP6)", note="checks the square arrives"),
        "Nothing on EN_OUT (J8 pin 2) or ~EN_OUT (J8 pin 10): a scope's ~24 pF there adds ~43 uA at 1 MHz",
        w.ad2_gnd(cfg),
    ]


def run(bench: Bench, ec: EnCurrentConfig) -> EnCurrentResult:
    """Sweep the square's frequency and read EN_VDD's current (see the module docstring).

    ec: the [en_current] section. Raises InstrumentError if there is no DMM or it can't read
    current; setup problems found in the data are recorded, never raised.
    """
    if bench.dmm is None:
        raise InstrumentError("enable-current needs the DMM: enable [dmm] in the config")
    dmm = bench.dmm
    if not hasattr(dmm, "read") or not hasattr(dmm, "configure_dci"):
        raise InstrumentError(f"the DMM ({dmm.idn}) driver can't measure current")
    cfg, a = bench.cfg, bench.cfg.ad2
    lo, hi, ch = ec.low, ec.square_high, ec.wavegen_channel
    bench.confirm_wiring(
        "enable block switching current",
        wiring(bench, ec),
        ref_probe=("en_ain", 0),
        dmm_probe="series",
        dmm_rail="en",
    )
    nplc = ec.nplc or (cfg.dmm.nplc if cfg.dmm else 10)
    dmm.configure_dci(range=ec.dmm_range, nplc=nplc)

    def read() -> tuple[float, float]:
        """(mean, std) of ec.readings DMM readings (A); (nan, nan) if any overloaded."""
        x = np.array([dmm.read() for _ in range(ec.readings)], float)
        if not np.all(np.isfinite(x)):
            return float("nan"), float("nan")
        return float(x.mean()), float(x.std(ddof=1))

    bench.loadbank.off()
    bench.wavegen_off(ch)
    bench.supply_off()
    bench.sleep(ec.settle_time)
    zero, zero_std = read()
    issues: list[str] = []
    if not np.isfinite(zero):
        issues.append("the DMM overloads with the supply off: check its function (DC amps) and leads")
    resolution = ec.dmm_range * RESOLUTION[nplc]
    floor = max(3 * zero_std, resolution) if np.isfinite(zero_std) else resolution
    log.info("zero %s, floor %s", fmt_a(zero), fmt_a(floor))

    # EN_AIN held at a defined level before VIN comes up, and between points: never floating
    bench.en_ain_dc(ch, lo)
    bench.vin_from_psu(ec.vin)
    bench.sleep(ec.settle_time)
    i, s = read()
    static = EnCurrentPoint(0.0, i - zero, s, float("nan"))
    log.info("static: %s", fmt_a(static.current))

    fine = ChannelSetup(bench.FINE_RANGE, 0.0)
    points: list[EnCurrentPoint] = []
    for f in ec.frequencies:
        bench.en_ain_sine(ch, frequency=f, low=lo, high=hi, shape="square")
        bench.sleep(ec.settle_time)
        i, s = read()
        rate = min(SWING_SAMPLES / (4 / f), AD2_MAX_SAMPLE_RATE)
        cap = bench.scope.capture(
            sample_rate=rate,
            n_samples=SWING_SAMPLES,
            setups={a.ref_channel: fine},
            trigger_channel=a.ref_channel,
            trigger_level=(lo + hi) / 2,
            edge="rising",
            pre_trigger=0.1,
            stimulus=lambda: None,
            timeout=1.0,
            force=True,  # only the swing matters: no trigger wait
        )
        x = cap.channel(a.ref_channel)
        swing = float(np.percentile(x, 98) - np.percentile(x, 2))
        points.append(EnCurrentPoint(f, i - zero, s, swing))
        log.info("%s: %s, EN_AIN swing %.3f V", fmt_hz(f), fmt_a(i - zero), swing)
    bench.en_ain_dc(ch, lo)

    for p in (static, *points):
        p.below_floor = bool(np.isfinite(p.current) and abs(p.current) < floor)
    if np.isfinite(zero) and any(not np.isfinite(p.current) for p in (static, *points)):
        issues.append("the DMM overloaded at some points (their current is NaN): check the range and leads")
    want = hi - lo
    bad = [p for p in points if abs(p.swing - want) > ec.swing_tolerance * want]
    if bad:
        issues.append(
            f"EN_AIN swings {bad[0].swing:.2f} V at {fmt_hz(bad[0].frequency)} but the square is {want:.2f} V "
            f'({len(bad)} of {len(points)} points): is scope {a.ref_channel} on EN_AIN (TP6), J6 at 5-6 "FG", '
            f"and W{ch} on EN_FG (J2)?"
        )
    top = points[-1]
    if np.isfinite(top.current) and np.isfinite(static.current) and top.current - static.current <= floor:
        issues.append(
            f"the EN_VDD current doesn't rise with frequency ({fmt_a(top.current)} at {fmt_hz(top.frequency)}, "
            f"{fmt_a(static.current)} static): is the DMM in place of J1 9-11 (jumper removed, I and LO jacks), "
            "and is the square reaching EN_AIN?"
        )

    ok = [p for p in points if np.isfinite(p.current) and not p.below_floor]
    charge, intercept = float("nan"), float("nan")
    if len(ok) >= 2:
        q, i0 = np.polyfit([p.frequency for p in ok], [p.current for p in ok], 1)
        charge, intercept = float(q), float(i0)
    notes = []
    under = [p for p in (static, *points) if p.below_floor]
    if under:
        notes.append(
            f"{len(under)} point(s) under the DMM floor ({fmt_a(floor)}): shown as bounds, not fitted"
        )
    return EnCurrentResult(
        ec.vin, lo, hi, zero, floor, static, points, charge, intercept, list(dict.fromkeys(issues)), notes
    )


def plot(res: EnCurrentResult, dut: str):
    """EN_VDD current against frequency, log-log, with the fit (left), and EN_AIN's swing (right).

    Points under the floor are drawn at the floor as hollow downward triangles (bounds). Issues go in
    a red box.
    """
    fig, (ax, ax_sw) = plotting.figure(1, 2, size=(11.0, 4.6), gridspec_kw={"width_ratios": [1.6, 1]})
    valid = not res.issues
    f = np.array(res.frequencies)
    cur = np.array([p.current for p in res.points])
    below = np.array([p.below_floor for p in res.points], bool)
    good = np.isfinite(cur) & ~below & (cur > 0)
    c = plotting.SERIES[0]
    ax.axhspan(1e-15, res.floor, color=plotting.GRID, alpha=0.6, lw=0, zorder=1)
    ax.annotate(
        f"below the DMM floor ({fmt_a(res.floor)})",
        (0.99, res.floor),
        xycoords=("axes fraction", "data"),
        xytext=(0, -10),
        textcoords="offset points",
        ha="right",
        fontsize=7.5,
        color=plotting.INK_2,
    )
    ax.plot(f[good], cur[good], "o", ms=5, color=c, zorder=4, label="measured")
    if below.any():
        ax.plot(
            f[below],
            np.full(below.sum(), res.floor),
            "v",
            ms=6,
            mfc=plotting.SURFACE,
            mec=c,
            zorder=4,
            label="under the floor (bound)",
        )
    if np.isfinite(res.charge):
        ff = np.geomspace(f.min(), f.max(), 200)
        fit = res.static_fit + res.charge * ff
        ax.plot(
            ff[fit > 0],
            fit[fit > 0],
            color=plotting.INK_2,
            lw=1.0,
            ls=(0, (3, 2)),
            zorder=3,
            label=f"fit: Q = {res.charge * 1e12:.2f} pC per cycle",
        )
    st = res.static
    if np.isfinite(st.current) and not st.below_floor and st.current > 0:
        ax.axhline(
            st.current, color=plotting.SERIES[1], lw=0.9, ls=(0, (1, 2)), label="static (EN_AIN at DC)"
        )
    ax.set_xscale("log")
    ax.set_yscale("log")
    top = (
        np.nanmax(np.r_[cur[np.isfinite(cur)], res.floor * 10]) if np.isfinite(cur).any() else res.floor * 10
    )
    ax.set_ylim(res.floor / 10, top * 3)
    ax.set_xlabel("Square frequency on EN_AIN (Hz)")
    ax.set_ylabel("EN_VDD current (A)")
    ax.set_title("EN_VDD current against frequency", fontsize=10, pad=6)
    ax.legend(loc="upper left", fontsize=8)
    if not valid:
        plotting.issue_box(ax, res.issues)

    sw = np.array([p.swing for p in res.points])
    ax_sw.plot(f, sw, "o-", color=plotting.SERIES[1], ms=4, lw=1.2)
    ax_sw.axhline(res.high - res.low, color=plotting.INK_2, lw=0.9, ls=(0, (3, 2)))
    ax_sw.set_xscale("log")
    ax_sw.set_ylim(0, 1.25 * (res.high - res.low))
    ax_sw.set_xlabel("Square frequency (Hz)")
    ax_sw.set_ylabel("EN_AIN swing (V)")
    ax_sw.set_title(f"EN_AIN swing (dashed: the square's {res.high - res.low:.2f} V)", fontsize=10, pad=6)
    title = f"Enable block switching current: {dut}  ·  {res.low:.2f}-{res.high:.2f} V square  ·  VIN {res.vin:.2f} V"
    plotting.suptitle(fig, title, invalid=not valid)
    return fig
