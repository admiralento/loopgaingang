"""temp: the board temperature, VREF and VOUT over time, their tempco and VREF's curve (bring-up Test 3.10).

TMP235 on J13 pin 5 (AD2 ref channel, T = (V - tmp_offset) / tmp_slope), VREF on the DMM, VOUT on
the AD2. Two runs make the curve: a cold one (ice, by hand) and a hot one (`--heater`: a power
resistor on the PSU's other output, ramped by the run). VS_BUFF, which powers the TMP235, comes from
a separate supply (temp.vs_buff_source = "external") or the PSU's other output ("psu", not with
--heater). docs/tests.md has the setup.

Results:
- Plateaus: where T holds within stable_delta for stable_window; two plateaus min_spread apart give
  a straight-line tempco from their means (temperature differences only, so offsets cancel).
- The curve (procedures/temp_curve.py): VREF against T, binned per direction, heating and cooling
  averaged, misreads and fast samples left out, and a parabola fit (curve.csv, bins.csv).

run(): EN low (--enable dio), VIN up, VS_BUFF up (with "psu"), the heater checked (--heater), EN high;
sample every `interval` until `duration` (2 h at most), the end of the heater's profile, or Ctrl-C,
which ends the log and keeps the data; heater off, EN low, VS_BUFF 0 V, load off; then the analysis.
With --heater each sample also steps a PI loop that follows the target ramp (HeaterRamp); above
heater.t_cutoff, a TMP235 reading outside temp_min..temp_max, or no rise (heater not on the board)
the heater goes to 0 and the log stops.
Issues: a temperature outside temp_min..temp_max, VOUT off nominal, VREF off vref_nominal (misreads
aside), too many misreads, a heater interlock. Notes: Ctrl-C, no tempco, misreads, the supply
current rising more than 20 % (condensation), the heater's summary.
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, field

import numpy as np

from ldo_bench import plotting
from ldo_bench import wiring as w
from ldo_bench.bench import Bench
from ldo_bench.config import HeaterConfig, TempConfig
from ldo_bench.instruments.base import ChannelSetup, InstrumentError
from ldo_bench.procedures import INVALID
from ldo_bench.procedures import temp_curve as tcur

log = logging.getLogger(__name__)

NAME = "temp"
CURRENT_RISE_NOTE = 0.2  # a supply-current rise above this fraction gets a note (leakage)
ROOM_C = 22.0  # C, the start temperature assumed for the profile length in the wiring prompt
PROFILE_MARGIN = 120.0  # s: the duration must leave this beyond the heater's profile
MIN_HEADROOM = 5.0  # C: the board must start at least this far below heater.t_high


def plateaus(t: np.ndarray, temp_c: np.ndarray, *, window: float, delta: float) -> list[tuple[int, int]]:
    """Index ranges [start, end] (inclusive) of stable stretches (t in s, temp_c in C).

    Sample k is stable when the samples from t[k] - window to t[k] cover at least 90 % of `window`
    and their temperatures differ by less than `delta`. Consecutive stable samples form a plateau.
    """
    t, x = np.asarray(t, float), np.asarray(temp_c, float)
    stable = np.zeros(len(t), bool)
    for k in range(len(t)):
        m = (t >= t[k] - window - 1e-9) & (t <= t[k])
        if t[k] - t[m][0] >= 0.9 * window and np.ptp(x[m]) < delta:
            stable[k] = True
    out, start = [], None
    for k, s in enumerate(stable):
        if s and start is None:
            start = k
        if not s and start is not None:
            out.append((start, k - 1))
            start = None
    if start is not None:
        out.append((start, len(t) - 1))
    return out


def _tc_ppm(temps: list[float], values: list[float]) -> float | None:
    """Slope of values against temperature, in ppm of their mean per C; None with under 2 points."""
    if len(temps) < 2:
        return None
    slope = np.polyfit(temps, values, 1)[0]
    return float(slope / np.mean(values) * 1e6)


@dataclass(frozen=True)
class HeaterRamp:
    """The heater's target temperature (C) against the time since heating started (s).

    Up from `start_c` at heater.rate to t_high, `hold` there, then down at the same rate to
    start_c + end_above_start; then done (None).
    """

    start_c: float
    hc: HeaterConfig

    @property
    def _rate(self) -> float:
        return self.hc.rate / 60.0  # C/s

    @property
    def t_up(self) -> float:
        return (self.hc.t_high - self.start_c) / self._rate

    @property
    def end_c(self) -> float:
        return self.start_c + self.hc.end_above_start

    @property
    def length(self) -> float:
        """Seconds from the start of heating to the end of the down ramp."""
        return self.t_up + self.hc.hold + (self.hc.t_high - self.end_c) / self._rate

    def target(self, t: float) -> float | None:
        """Target (C) at `t` s into the profile; None once it is done."""
        if t < self.t_up:
            return self.start_c + self._rate * t
        if t < self.t_up + self.hc.hold:
            return self.hc.t_high
        if t <= self.length:
            return self.hc.t_high - self._rate * (t - self.t_up - self.hc.hold)
        return None

    def phase(self, t: float) -> str:
        if t < self.t_up:
            return "up"
        if t < self.t_up + self.hc.hold:
            return "hold"
        return "down" if t <= self.length else "done"


class _Pi:
    """PI loop: heater power (W) from the error (target - T, C), with anti-windup at 0 and max_power."""

    def __init__(self, hc: HeaterConfig) -> None:
        self.hc = hc
        self.integral = 0.0  # C s

    def step(self, error: float, dt: float) -> float:
        hc = self.hc
        trial = hc.kp * error + hc.ki * (self.integral + error * dt)
        saturated_high = trial >= hc.max_power and error > 0
        saturated_low = trial <= 0 and error < 0
        if not (saturated_high or saturated_low):
            self.integral += error * dt
        return min(max(hc.kp * error + hc.ki * self.integral, 0.0), hc.max_power)


@dataclass
class TempResult:
    """The log (`rows`, data.csv), its plateaus and its curve (curve.csv, bins.csv)."""

    rows: list[dict]
    points: list[dict]  # one per plateau: mean temp_c, vref_v, vout_v, t_start_s, t_end_s, samples
    vref_tc: float | None  # ppm/C, or None without two plateaus min_spread apart (or no DMM)
    vout_tc: float | None
    stopped_early: bool
    issues: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    curve: tcur.CurveResult | None = None
    heater: dict | None = None  # the heater's summary (--heater)

    def summary(self) -> dict:
        return {
            "valid": not self.issues,
            "issues": self.issues,
            "notes": self.notes,
            "samples": len(self.rows),
            "duration_s": self.rows[-1]["t_s"] if self.rows else 0.0,
            "stopped_early": self.stopped_early,
            "plateaus": len(self.points),
            "plateau_points": self.points,
            "vref_tc_ppm_per_c": self.vref_tc,
            "vout_tc_ppm_per_c": self.vout_tc,
            "curve": self.curve.summary() if self.curve is not None else None,
            "heater": self.heater,
        }

    def extra_csvs(self) -> dict[str, list[dict]]:
        """curve.csv (VREF against T, the directions averaged) and bins.csv (per direction)."""
        if self.curve is None:
            return {}
        return {"curve.csv": self.curve.curve, "bins.csv": self.curve.bins}

    def report(self) -> str:
        head = f"Temperature log: {len(self.rows)} samples over {self.summary()['duration_s'] / 60:.1f} min"
        if self.issues:
            return f"{head}\n  {INVALID}"
        lines = [head, f"  plateaus      {len(self.points)}"]
        for p in self.points:
            vref = f"VREF {p['vref_v']:.5f} V" if np.isfinite(p["vref_v"]) else "VREF not read"
            lines.append(
                f"    {p['temp_c']:6.2f} C   {vref}   VOUT {p['vout_v']:.4f} V   "
                f"({p['samples']} samples, {p['t_start_s'] / 60:.1f}-{p['t_end_s'] / 60:.1f} min)"
            )
        if self.vref_tc is not None:
            lines.append(f"  VREF tempco   {self.vref_tc:+.1f} ppm/C (DMM, plateaus)")
        if self.vout_tc is not None:
            lines.append(f"  VOUT tempco   {self.vout_tc:+.1f} ppm/C (AD2: its resolution limits this)")
        f = self.curve.fit if self.curve is not None else None
        if f:
            lines += [
                f"  curve         {len(self.curve.curve)} bins, {f['t_min_c']:.1f}-{f['t_max_c']:.1f} C",
                f"  {f['shape']:<12}  {f['peak_c']:.1f} C, {f['peak_v']:.5f} V"
                + ("" if f["peak_in_range"] else " (outside the measured range: extrapolated)"),
                f"  curvature     {f['curvature_ppm_per_c2']:+.4f} ppm/C^2",
                f"  box tempco    {f['box_tc_ppm_per_c']:.1f} ppm/C",
            ]
        lines += [f"  note: {n}" for n in self.notes]
        return "\n".join(lines)


def _profile_minutes(hc: HeaterConfig, start_c: float = ROOM_C) -> float:
    return HeaterRamp(start_c, hc).length / 60


def wiring(bench: Bench, tc: TempConfig, heater: HeaterConfig | None = None) -> list[str]:
    """Wiring and setup lines shown before the run (JAFWAK): the hot run's with `heater`, else the cold run's."""
    cfg = bench.cfg
    b = cfg.board
    lines = [*w.psu_on_vin(cfg), "Fit JP2 (the BGR drives VREF_IN)"]
    if tc.vs_buff_source == "psu":
        lines += [
            f"PSU OUT{tc.vs_buff_channel} + -> {b.vs_buff} ({tc.vs_buff_v:g} V, set by this run: "
            "powers the TMP235 on TEMP (TP7))",
            f"PSU OUT{tc.vs_buff_channel} - -> {b.supply_gnd}",
        ]
    else:
        lines += [
            f"Separate supply + ({tc.vs_buff_v:.1f} V) -> {b.vs_buff} (powers the TMP235 on TEMP (TP7)): "
            "on before you confirm",
            f"Separate supply - -> {b.supply_gnd}",
        ]
    lines += [
        "U2's V- wire (the split-supply rework) on GND, not on the PSU",
        w.scope(cfg, "ref", "J13 pin 5 (TMP)"),
        w.scope_vout(cfg),
    ]
    if bench.dmm is not None:
        lines.append(w.dmm("VREF (TP3)", "GND (TP2)", "the run sets its input to 10 GOhm"))
    if not bench.enable_dio:
        lines += w.enable_static(cfg)
    if tc.load_code:
        lines += w.load_bank(cfg, supply_given=True)
    lines.append(w.ad2_gnd(cfg))
    cover = "Opaque box over the board (the same one for the cold and hot runs), the leads out under its edge"
    if heater is not None:
        ch = heater.channel
        lines += [
            f"HOT RUN: PSU OUT{ch} + -> heater ({heater.resistance:g} Ohm power resistor) lead 1, "
            f"PSU OUT{ch} - -> its other lead: nothing else on OUT{ch}, neither lead touching the board",
            "Heater: on U1 and the TMP235 (TEMP, TP7) together, a couple of layers of paper between it and the board",
            "No ice on the board",
            cover,
            f"The run checks the heater at {heater.check_v:g} V, then ramps {heater.rate:g} C/min up to "
            f"{heater.t_high:g} C, holds {heater.hold / 60:g} min and ramps back down to the start + "
            f"{heater.end_above_start:g} C (about {_profile_minutes(heater):.0f} min from {ROOM_C:g} C). Above "
            f"{heater.t_cutoff:g} C the heater goes off and the run stops; Ctrl-C stops it too (data kept)",
        ]
    else:
        if tc.vs_buff_source != "psu":
            lines.append("COLD RUN: heater off the board (it may stay wired to PSU OUT2: held at 0 V)")
        lines += [
            "Ice: a sealed bag with a dry paper towel under it, on U1 and the TMP235 (TEMP, TP7) together",
            cover,
            "Start at room temperature; put the ice on about 3 min in, leave it until T stops falling, then take "
            "it off and let the board warm back to room temperature under the box. The fast drop when the ice "
            f"goes on stays out of the curve (over {tc.max_rate:g} C/min). Ctrl-C ends the run (data kept)",
        ]
    return lines


def run(bench: Bench, tc: TempConfig, heater: HeaterConfig | None = None) -> TempResult:
    """Log until tc.duration, the heater profile's end or Ctrl-C (see the module docstring).

    heater: the hot run (--heater): the PSU's other output drives the heater through a PI loop. It
    needs temp.vs_buff_source = "external" when VS_BUFF would use the same output. Setup problems
    found in the data go in `issues`; a refused setup raises InstrumentError before anything heats.
    """
    a = bench.cfg.ad2
    if heater is not None and tc.vs_buff_source == "psu" and tc.vs_buff_channel == heater.channel:
        raise InstrumentError(
            f"--heater needs PSU OUT{heater.channel}, which temp.vs_buff_source = 'psu' gives VS_BUFF: "
            "set temp.vs_buff_source = 'external' (VS_BUFF from a separate supply)"
        )
    bench.loadbank.require([tc.load_code])  # dut.i_max with any external load, before power-up
    aux = "heater" if heater is not None else ("vs_buff" if tc.vs_buff_source == "psu" else "vref")
    bench.confirm_wiring(
        "temperature log (hot run: heater)" if heater is not None else "temperature log (cold run: ice)",
        wiring(bench, tc, heater),
        ref_probe=("tmp", 0),
        dmm_probe="vref",
        aux_to=aux,
        vs_buff_by_hand=tc.vs_buff_source == "external",
    )
    setups = {
        a.vout_channel: ChannelSetup(a.vout.range, a.vout.offset),
        a.ref_channel: ChannelSetup(bench.FINE_RANGE, 0.0),  # TMP235: 0.3-1.3 V, on the fine 5 V range
    }
    bench.ldo_enable(False)  # --enable dio only: EN held low while VIN comes up
    bench.loadbank.set(tc.load_code)
    bench.vin_from_psu(tc.vin)
    if tc.vs_buff_source == "psu":
        bench.vs_buff_on(tc.vs_buff_v)
    if heater is not None:
        bench.heater_on()  # resistance check at check_v, back to 0 V
    bench.ldo_enable(True)
    rows: list[dict] = []
    issues: list[str] = []
    t0 = bench.now()
    stopped_early = False
    ctl = _HeaterControl(bench, tc, heater) if heater is not None else None
    try:
        while (t := bench.now() - t0) <= tc.duration:
            r = bench.scope.measure_dc(tc.dc_window, setups)
            tmp_v, vout_v = r[a.ref_channel].mean, r[a.vout_channel].mean
            row = {
                "t_s": t,
                "temp_c": (tmp_v - tc.tmp_offset) / tc.tmp_slope,
                "tmp_v": tmp_v,
                "vout_v": vout_v,
                "vref_v": bench.dmm_mean()[0]
                if bench.dmm is not None
                else float("nan"),  # dmm.samples averaged
            }
            if tc.psu_readback:
                row["i_psu_a"] = bench.psu.measure_current()
                if bench.vs_buff_out is not None:
                    row["i_vs_buff_a"] = bench.vs_buff_out.measure_current()
            stop = ctl.step(t, row) if ctl is not None else None
            rows.append(row)
            if ctl is not None:
                log.info(
                    "%6.0f s  T %6.2f C (target %5.1f)  heater %4.2f W  VREF %.5f V  VOUT %.4f V",
                    t,
                    row["temp_c"],
                    row["target_c"],
                    row["heater_w"],
                    row["vref_v"],
                    vout_v,
                )
            else:
                log.info(
                    "%6.0f s  T %6.2f C  VREF %.5f V  VOUT %.4f V", t, row["temp_c"], row["vref_v"], vout_v
                )
            if stop is not None:
                if stop:  # an interlock: the heater is already off
                    issues.append(stop)
                    log.warning("temp: %s", stop)
                break
            bench.sleep(tc.interval)
    except KeyboardInterrupt:
        stopped_early = True  # Ctrl-C ends the log; the data so far is kept and the bench still shuts down
        log.warning("temp: stopped by Ctrl-C after %d samples", len(rows))
    bench.heater_off()
    bench.ldo_enable(False)
    bench.vs_buff_off()
    bench.loadbank.off()
    res = _analyse(bench, tc, rows, stopped_early, issues)
    if ctl is not None:
        res.heater = ctl.summary(rows)
        res.notes.append(ctl.note(rows))
    return res


class _HeaterControl:
    """One PI step per sample, and the interlocks (see the module docstring)."""

    def __init__(self, bench: Bench, tc: TempConfig, hc: HeaterConfig) -> None:
        self.bench, self.tc, self.hc = bench, tc, hc
        self.pi = _Pi(hc)
        self.ramp: HeaterRamp | None = None
        self.t_heat0 = 0.0  # s (log time) when heating started
        self.last_t: float | None = None
        self.history: deque[tuple[float, float, float]] = deque()  # (t, T, error) for the stall check
        self.done = False
        self.stop_reason: str | None = None

    def step(self, t: float, row: dict) -> str | None:
        """Set the heater from this sample; add target_c / heater_w / heater_i_a / phase to `row`.

        Returns None to go on, "" when the profile is done, or an issue when an interlock tripped
        (the heater is at 0 W by then)."""
        hc, temp_c = self.hc, row["temp_c"]
        if self.ramp is None:  # first sample: the profile starts here
            if temp_c > hc.t_high - MIN_HEADROOM:
                raise InstrumentError(
                    f"the board reads {temp_c:.1f} C, within {MIN_HEADROOM:g} C of heater.t_high ({hc.t_high:g} C): "
                    "let it cool first (or check the TMP235 on J13 pin 5)"
                )
            self.ramp = HeaterRamp(temp_c, hc)
            need = t + self.ramp.length + PROFILE_MARGIN
            if need > self.tc.duration:
                raise InstrumentError(
                    f"the heater's profile from {temp_c:.1f} C takes {self.ramp.length / 60:.0f} min: run with --minutes "
                    f"{int(np.ceil(need / 60))} or more (at most {self.tc.duration / 60:.0f} min now)"
                )
            self.t_heat0 = t
            log.info(
                "Heater profile: %.1f -> %.0f C -> %.1f C, %.0f min",
                temp_c,
                hc.t_high,
                self.ramp.end_c,
                need / 60,
            )
        th = t - self.t_heat0
        target = self.ramp.target(th)
        row["target_c"] = target if target is not None else float("nan")
        row["phase"] = self.ramp.phase(th)
        reason = self._interlock(t, temp_c, target)
        if reason is not None or target is None:
            self.bench.heater_off()
            row["heater_w"] = 0.0
            row["heater_i_a"] = self.bench.heater_out.measure_current()
            self.done = target is None and reason is None
            self.stop_reason = reason
            return reason if reason is not None else ""
        dt = 0.0 if self.last_t is None else t - self.last_t
        self.last_t = t
        row["heater_w"] = self.bench.heater_set(self.pi.step(target - temp_c, dt))
        row["heater_i_a"] = self.bench.heater_out.measure_current()
        return None

    def _interlock(self, t: float, temp_c: float, target: float | None) -> str | None:
        hc, tc = self.hc, self.tc
        if temp_c > hc.t_cutoff:
            return f"TMP235 reads {temp_c:.1f} C at {t:.0f} s, above heater.t_cutoff ({hc.t_cutoff:g} C): heater off, log stopped"
        if not tc.temp_min <= temp_c <= tc.temp_max:
            return (
                f"TMP235 reads {temp_c:.1f} C at {t:.0f} s while heating, outside {tc.temp_min:g} to {tc.temp_max:g} C: "
                "heater off, log stopped (is VS_BUFF's supply on, and scope 2 on J13 pin 5?)"
            )
        if target is None:
            return None
        self.history.append((t, temp_c, target - temp_c))
        while self.history and self.history[0][0] < t - hc.stall_window:
            self.history.popleft()
        t_first, temp_first, _ = self.history[0]
        if (
            t - t_first >= 0.9 * hc.stall_window
            and all(e > hc.stall_error for _, _, e in self.history)
            and temp_c - temp_first < hc.stall_rise
        ):
            return (
                f"the temperature isn't rising: {temp_c - temp_first:+.2f} C in {(t - t_first) / 60:.0f} min with the target "
                f"{hc.stall_error:g}+ C ahead (heater {self.bench.heater_out.get_voltage_setpoint():.2f} V): is the "
                f"heater on U1 and the TMP235, and on PSU OUT{hc.channel}? Heater off, log stopped"
            )
        return None

    def summary(self, rows: list[dict]) -> dict:
        hr = [r for r in rows if "heater_w" in r]
        temps = [r["temp_c"] for r in hr]
        return {
            "resistance_ohm": self.bench.heater_r,
            "start_c": self.ramp.start_c if self.ramp else None,
            "t_high_c": self.hc.t_high,
            "rate_c_per_min": self.hc.rate,
            "max_power_w": max((r["heater_w"] for r in hr), default=0.0),
            "max_temp_c": max(temps, default=None),
            "profile_done": self.done,
            "stopped_by": self.stop_reason,
        }

    def note(self, rows: list[dict]) -> str:
        s = self.summary(rows)
        end = "profile done" if s["profile_done"] else (s["stopped_by"] or "stopped before the profile's end")
        return (
            f"heater: {s['resistance_ohm']:.3f} Ohm, up to {s['max_power_w']:.2f} W, highest {s['max_temp_c']:.1f} C "
            f"(target {s['t_high_c']:g} C at {s['rate_c_per_min']:g} C/min); {end}"
        )


def _analyse(
    bench: Bench, tc: TempConfig, rows: list[dict], stopped_early: bool, issues: list[str] | None = None
) -> TempResult:
    """Plateaus, tempco, the curve, checks and notes from the logged rows."""
    issues, notes = list(issues or []), []
    if stopped_early:
        notes.append(f"the log was stopped with Ctrl-C after {len(rows)} samples")
    if not rows:
        return TempResult(rows, [], None, None, stopped_early, [*issues, "no samples were taken"], notes)
    vrefs_all = np.array([r["vref_v"] for r in rows])
    curve = tcur.analyse(rows, tc) if np.isfinite(vrefs_all).any() else None
    if curve is not None:
        rows = curve.rows  # with rate_c_per_min, direction, misread, in_curve
    else:
        for r in rows:
            r["misread"] = False
    t = np.array([r["t_s"] for r in rows])
    temps = np.array([r["temp_c"] for r in rows])
    a = bench.cfg.ad2
    out = np.nonzero((temps < tc.temp_min) | (temps > tc.temp_max))[0]
    if len(out):
        k = out[0]
        issues.append(
            f"TMP235 reads {rows[k]['tmp_v']:.3f} V ({temps[k]:.1f} C) at {t[k]:.0f} s, outside {tc.temp_min:g} to "
            f"{tc.temp_max:g} C ({len(out)} of {len(rows)} samples): is scope {a.ref_channel} on J13 pin 5 (TMP), "
            "and is VS_BUFF (J4) powered?"
        )
    src = f"scope {a.vout_channel}"
    bad_vout = [(r["t_s"], i) for r in rows if (i := bench.vout_issue(r["vout_v"], source=src))]
    if bad_vout:
        tv, issue = bad_vout[0]
        issues.append(f"{issue} (at {tv:.0f} s; {len(bad_vout)} of {len(rows)} samples)")
    ok = np.array([not r["misread"] for r in rows])  # a misread is the DMM's, not VREF's: counted on its own
    vrefs = np.where(ok, vrefs_all, np.nan)
    if np.isfinite(vrefs_all).any():
        bad = np.nonzero(np.abs(vrefs - tc.vref_nominal) > tc.vref_tolerance * tc.vref_nominal)[0]
        if len(bad):
            issues.append(
                f"VREF reads {vrefs[bad[0]]:.4f} V at {t[bad[0]]:.0f} s, expected ~{tc.vref_nominal} V "
                f"(+/-{tc.vref_tolerance:.0%}; {len(bad)} of {len(rows)} samples): is the DMM on VREF (TP3)?"
            )
    else:
        notes.append("no DMM: VREF wasn't read, so there's no VREF tempco or curve")
    if curve is not None:
        issues += curve.issues
        notes += curve.notes

    spans = plateaus(t, temps, window=tc.stable_window, delta=tc.stable_delta)
    # a plateau must itself last stable_window: a ramp right at stable_delta / stable_window (1 C/min
    # with the defaults) otherwise flickers into dozens of short ones
    spans = [(i0, i1) for i0, i1 in spans if t[i1] - t[i0] >= tc.stable_window]
    for k, (i0, i1) in enumerate(spans):
        for r in rows[i0 : i1 + 1]:
            r["stable"] = k + 1
    for r in rows:
        r.setdefault("stable", 0)  # 0 = not on a plateau, else the plateau number
    points = []
    for i0, i1 in spans:
        seg = rows[i0 : i1 + 1]
        good = [r["vref_v"] for r in seg if not r["misread"]]
        points.append(
            {
                "temp_c": float(np.mean([r["temp_c"] for r in seg])),
                "vref_v": float(np.mean(good)) if good else float("nan"),
                "vout_v": float(np.mean([r["vout_v"] for r in seg])),
                "t_start_s": seg[0]["t_s"],
                "t_end_s": seg[-1]["t_s"],
                "samples": len(seg),
            }
        )
    vref_tc = vout_tc = None
    pt = [p["temp_c"] for p in points]
    if len(points) >= 2 and np.ptp(pt) >= tc.min_spread:
        if np.isfinite([p["vref_v"] for p in points]).all():
            vref_tc = _tc_ppm(pt, [p["vref_v"] for p in points])
        vout_tc = _tc_ppm(pt, [p["vout_v"] for p in points])
    elif not issues:
        notes.append(
            f"no tempco: it needs two stable temperatures at least {tc.min_spread:g} C apart "
            f"({len(points)} plateau(s) found). Hold each temperature for more than {tc.stable_window:g} s"
        )
    cur = [r["i_psu_a"] for r in rows if "i_psu_a" in r]
    if len(cur) >= 2 and cur[0] > 0 and cur[-1] > (1 + CURRENT_RISE_NOTE) * cur[0]:
        notes.append(
            f"the supply current rose from {cur[0] * 1e3:.2f} to {cur[-1] * 1e3:.2f} mA: check for condensation "
            "on the board (moisture leakage)"
        )
    for i in issues:
        log.warning("temp: %s", i)
    return TempResult(rows, points, vref_tc, vout_tc, stopped_early, issues, notes, curve)


def plot(res: TempResult, dut: str):
    """Against time: T (and the heater's target), VREF, VOUT (plateaus shaded) and the heater's power;
    beside them VREF against T: heating / cooling bins, the combined curve, its fit, plateau means."""
    import matplotlib.pyplot as plt

    heated = any("heater_w" in r for r in res.rows)
    n = 4 if heated else 3
    fig = plt.figure(figsize=(12.0, 7.6 if heated else 6.4), layout="constrained")
    gs = fig.add_gridspec(n, 2, width_ratios=[1.5, 1])
    ax_t = fig.add_subplot(gs[0, 0])
    axes = [ax_t] + [fig.add_subplot(gs[k, 0], sharex=ax_t) for k in range(1, n)]
    ax_r, ax_o = axes[1], axes[2]
    ax_c = fig.add_subplot(gs[:, 1])
    mins = np.array([r["t_s"] for r in res.rows]) / 60
    misread = np.array([bool(r.get("misread")) for r in res.rows])
    ax_t.plot(mins, [r["temp_c"] for r in res.rows], color=plotting.SERIES[2], lw=1.2, label="TMP235")
    if heated:
        ax_t.plot(
            mins,
            [r.get("target_c", np.nan) for r in res.rows],
            "--",
            color=plotting.INK_2,
            lw=1.0,
            label="target",
        )
        ax_t.legend(loc="best")
    ax_t.set_ylabel("T (C)")
    vref = np.array([r["vref_v"] for r in res.rows])
    ax_r.plot(mins[~misread], vref[~misread], color=plotting.SERIES[0], lw=1.2)
    ax_r.set_ylabel("VREF (V)")
    if misread.any():
        good = vref[~misread]
        if np.isfinite(good).any():
            lo, hi = np.nanmin(good), np.nanmax(good)
            pad = max(hi - lo, 1e-4) * 0.1
            ax_r.set_ylim(lo - pad, hi + pad)
        plotting.subtitle(ax_r, f"{int(misread.sum())} DMM misread(s) left out (off scale)")
    ax_o.plot(mins, [r["vout_v"] for r in res.rows], color=plotting.SERIES[1], lw=1.2)
    ax_o.set_ylabel("VOUT (V)")
    if heated:
        axes[3].plot(mins, [r.get("heater_w", np.nan) for r in res.rows], color=plotting.SERIES[3], lw=1.2)
        axes[3].set_ylabel("Heater (W)")
    for ax in axes:
        for p in res.points:
            ax.axvspan(p["t_start_s"] / 60, p["t_end_s"] / 60, color=plotting.GRID, alpha=0.6, lw=0, zorder=0)
    for ax in axes[:-1]:
        ax.tick_params(labelbottom=False)
    axes[-1].set_xlabel("Time (min)  ·  shaded = stable plateau")
    if res.curve is not None:
        tcur.plot_curve(ax_c, res.curve.bins, res.curve.curve, res.curve.fit)
    if res.points:
        ax_c.plot(
            [p["temp_c"] for p in res.points],
            [p["vref_v"] for p in res.points],
            "s",
            mfc="none",
            color=plotting.INK_2,
            ms=7,
            label="plateau mean",
        )
        ax_c.legend(loc="best")
    valid = not res.issues
    if not valid:
        plotting.issue_box(axes[-1], res.issues)
    run = "hot run (heater)" if heated else "cold run"
    plotting.suptitle(fig, f"Temperature: {dut}  ·  {run}  ·  {len(res.rows)} samples", invalid=not valid)
    return fig
