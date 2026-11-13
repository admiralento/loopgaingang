"""temp: log the board temperature, VREF and VOUT over time, and the tempco from stable plateaus.

First bring-up plan, Test 3.10. The TMP235 next to U1 runs from VS_BUFF (J4, set by hand) and its
output, TEMP (TP7), is on J13 pin 5, read by AD2 scope ref channel: T = (V - tmp_offset) / tmp_slope.
VREF (TP3) is on the DMM and VOUT (OUTA, J13 pin 4) on AD2 scope vout channel. With the LDO on,
the run samples every `interval` for `duration` (or until Ctrl-C). Take readings at room
temperature, then put ice in a sealed bag on U1, and leave each for a few minutes.

Where the temperature held within stable_delta for stable_window, the samples count as stable;
each run of stable samples is a plateau, averaged into one point (T, VREF, VOUT). Two or more
plateaus at least min_spread apart give the temperature coefficient (ppm/C) of VREF and VOUT from
a straight-line fit through the plateau means. That uses temperature differences, so the AD2's DC
offset (~10 mV, ~1 C, a figure still to check) and the TMP235's own offset largely cancel.

run() steps:
1. Confirm the wiring. EN low (--enable dio), VIN up, EN high, load code set.
2. Every `interval`: TMP and VOUT from the AD2 (averaged over dc_window), VREF from the DMM,
   optionally the supply current. Stops at `duration` (2 h at most) or on Ctrl-C, which ends the
   log, keeps the data and still shuts the bench down.
3. EN low, load off. Plateaus and the tempco.

Validity checks (issues): every temperature within temp_min..temp_max (scope on J13 pin 5,
VS_BUFF powered), VOUT near nominal, VREF within vref_tolerance of vref_nominal (DMM on TP3).
Notes: Ctrl-C, no tempco (fewer than two plateaus far enough apart), supply current rising more
than 20 % (moisture on the board can do that).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np

from ldo_bench import plotting
from ldo_bench import wiring as w
from ldo_bench.bench import Bench
from ldo_bench.config import TempConfig
from ldo_bench.instruments.base import ChannelSetup

log = logging.getLogger(__name__)

NAME = "temp"
CURRENT_RISE_NOTE = 0.2  # a supply-current rise above this fraction gets a note (leakage)


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


@dataclass
class TempResult:
    """The log (`rows`, data.csv) and its plateaus."""

    rows: list[dict]
    points: list[dict]  # one per plateau: mean temp_c, vref_v, vout_v, t_start_s, t_end_s, samples
    vref_tc: float | None  # ppm/C, or None without two plateaus min_spread apart (or no DMM)
    vout_tc: float | None
    stopped_early: bool
    issues: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

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
        }

    def report(self) -> str:
        head = f"Temperature log: {len(self.rows)} samples over {self.summary()['duration_s'] / 60:.1f} min"
        if self.issues:
            return "\n".join(
                [head, "  NOT A VALID MEASUREMENT (data saved and plotted as measured):"]
                + [f"    - {i}" for i in self.issues]
            )
        lines = [head, f"  plateaus      {len(self.points)}"]
        for p in self.points:
            vref = f"VREF {p['vref_v']:.5f} V" if np.isfinite(p["vref_v"]) else "VREF not read"
            lines.append(
                f"    {p['temp_c']:6.2f} C   {vref}   VOUT {p['vout_v']:.4f} V   "
                f"({p['samples']} samples, {p['t_start_s'] / 60:.1f}-{p['t_end_s'] / 60:.1f} min)"
            )
        if self.vref_tc is not None:
            lines.append(f"  VREF tempco   {self.vref_tc:+.1f} ppm/C (DMM)")
        if self.vout_tc is not None:
            lines.append(f"  VOUT tempco   {self.vout_tc:+.1f} ppm/C (AD2: its resolution limits this)")
        lines += [f"  note: {n}" for n in self.notes]
        return "\n".join(lines)


def wiring(bench: Bench, tc: TempConfig) -> list[str]:
    """Wiring lines shown before the run, one connection per line (JAFWAK)."""
    cfg = bench.cfg
    lines = [
        *w.psu_on_vin(cfg),
        "Fit JP2 (the BGR drives VREF_IN)",
        *cfg.board.bank_supply,  # VS_BUFF: powers the TMP235 on TEMP (TP7)
        w.scope(cfg, "ref", "J13 pin 5 (TMP)"),
        w.scope_vout(cfg),
    ]
    if bench.dmm is not None:
        lines.append(w.dmm("VREF (TP3)", "GND (TP2)", "the run sets its input to 10 GOhm"))
    if not bench.enable_dio:
        lines += w.enable_static(cfg)
    if tc.load_code:
        lines += w.load_bank(cfg, supply_given=True)
    lines.append("Optional 1 mA load: 1.2 kOhm from VOUT (J5) to GND (J12)")
    return [*lines, w.ad2_gnd(cfg)]


def run(bench: Bench, tc: TempConfig) -> TempResult:
    """Log until tc.duration or Ctrl-C (see the module docstring). Setup problems go in `issues`."""
    a = bench.cfg.ad2
    bench.confirm_wiring("temperature log", wiring(bench, tc), ref_probe=("tmp", 0), dmm_probe="vref")
    setups = {
        a.vout_channel: ChannelSetup(a.vout.range, a.vout.offset),
        a.ref_channel: ChannelSetup(5.0, 0.0),  # TMP235: 0.3-1.3 V, on the fine 5 V range
    }
    bench.ldo_enable(False)  # --enable dio only: EN held low while VIN comes up
    bench.loadbank.set(tc.load_code)
    bench.vin_from_psu(tc.vin)
    bench.ldo_enable(True)
    rows: list[dict] = []
    t0 = bench.now()
    stopped_early = False
    try:
        while (t := bench.now() - t0) <= tc.duration:
            r = bench.scope.measure_dc(tc.dc_window, setups)
            tmp_v, vout_v = r[a.ref_channel].mean, r[a.vout_channel].mean
            row = {
                "t_s": t,
                "temp_c": (tmp_v - tc.tmp_offset) / tc.tmp_slope,
                "tmp_v": tmp_v,
                "vout_v": vout_v,
                "vref_v": bench.dmm.read_dcv() if bench.dmm is not None else float("nan"),
            }
            if tc.psu_readback:
                row["i_psu_a"] = bench.psu.measure_current()
            rows.append(row)
            log.info("%6.0f s  T %6.2f C  VREF %.5f V  VOUT %.4f V", t, row["temp_c"], row["vref_v"], vout_v)
            bench.sleep(tc.interval)
    except KeyboardInterrupt:
        stopped_early = True  # Ctrl-C ends the log; the data so far is kept and the bench still shuts down
        log.warning("temp: stopped by Ctrl-C after %d samples", len(rows))
    bench.ldo_enable(False)
    bench.loadbank.off()
    return _analyse(bench, tc, rows, stopped_early)


def _analyse(bench: Bench, tc: TempConfig, rows: list[dict], stopped_early: bool) -> TempResult:
    """Plateaus, tempco, checks and notes from the logged rows."""
    issues, notes = [], []
    if stopped_early:
        notes.append(f"the log was stopped with Ctrl-C after {len(rows)} samples")
    if not rows:
        return TempResult(rows, [], None, None, stopped_early, ["no samples were taken"], notes)
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
    bad_vout = [(r["t_s"], r["vout_v"]) for r in rows if bench.vout_issue(r["vout_v"], source="x")]
    if bad_vout:
        tv, v = bad_vout[0]
        issues.append(
            f"{bench.vout_issue(v, source=f'scope {a.vout_channel}')} (at {tv:.0f} s; {len(bad_vout)} of {len(rows)} samples)"
        )
    vrefs = np.array([r["vref_v"] for r in rows])
    if np.isfinite(vrefs).any():
        bad = np.nonzero(np.abs(vrefs - tc.vref_nominal) > tc.vref_tolerance * tc.vref_nominal)[0]
        if len(bad):
            issues.append(
                f"VREF reads {vrefs[bad[0]]:.4f} V at {t[bad[0]]:.0f} s, expected ~{tc.vref_nominal} V "
                f"(+/-{tc.vref_tolerance:.0%}; {len(bad)} of {len(rows)} samples): is the DMM on VREF (TP3)?"
            )
    else:
        notes.append("no DMM: VREF wasn't read, so there's no VREF tempco")

    spans = plateaus(t, temps, window=tc.stable_window, delta=tc.stable_delta)
    for k, (i0, i1) in enumerate(spans):
        for r in rows[i0 : i1 + 1]:
            r["stable"] = k + 1
    for r in rows:
        r.setdefault("stable", 0)  # 0 = not on a plateau, else the plateau number
    points = []
    for i0, i1 in spans:
        seg = rows[i0 : i1 + 1]
        points.append(
            {
                "temp_c": float(np.mean([r["temp_c"] for r in seg])),
                "vref_v": float(np.mean([r["vref_v"] for r in seg])),
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
    return TempResult(rows, points, vref_tc, vout_tc, stopped_early, issues, notes)


def plot(res: TempResult, dut: str):
    """Temperature, VREF and VOUT against time (plateaus shaded), and the plateau means against T."""
    import matplotlib.pyplot as plt

    fig = plt.figure(figsize=(11.0, 6.4), layout="constrained")
    gs = fig.add_gridspec(3, 2, width_ratios=[1.6, 1])
    ax_t = fig.add_subplot(gs[0, 0])
    ax_r = fig.add_subplot(gs[1, 0], sharex=ax_t)
    ax_o = fig.add_subplot(gs[2, 0], sharex=ax_t)
    ax_tc = fig.add_subplot(gs[:, 1])
    mins = np.array([r["t_s"] for r in res.rows]) / 60
    series = [
        (ax_t, "temp_c", "T (C)", plotting.SERIES[2]),
        (ax_r, "vref_v", "VREF (V)", plotting.SERIES[0]),
        (ax_o, "vout_v", "VOUT (V)", plotting.SERIES[1]),
    ]
    for ax, key, label, color in series:
        ax.plot(mins, [r[key] for r in res.rows], color=color, lw=1.2)
        ax.set_ylabel(label)
        for p in res.points:
            ax.axvspan(p["t_start_s"] / 60, p["t_end_s"] / 60, color=plotting.GRID, alpha=0.6, lw=0, zorder=0)
    for ax in (ax_t, ax_r):
        ax.tick_params(labelbottom=False)
    ax_o.set_xlabel("Time (min)  ·  shaded = stable plateau")
    if res.points:
        pt = [p["temp_c"] for p in res.points]
        ax_tc.plot(pt, [p["vref_v"] for p in res.points], "o-", color=plotting.SERIES[0], label="VREF (DMM)")
        ax_tc.plot(pt, [p["vout_v"] for p in res.points], "s--", color=plotting.SERIES[1], label="VOUT (AD2)")
        ax_tc.legend(loc="best")
    ax_tc.set_xlabel("T (C), plateau mean")
    ax_tc.set_ylabel("V")
    tc = f"VREF {res.vref_tc:+.1f} ppm/C" if res.vref_tc is not None else "no tempco"
    ax_tc.set_title(tc, fontsize=10, pad=6)
    valid = not res.issues
    if not valid:
        plotting.issue_box(ax_o, res.issues)
    title = f"Temperature: {dut}  ·  {len(res.rows)} samples"
    if not valid:
        title = "SETUP PROBLEM, not a valid measurement\n" + title
    fig.suptitle(
        title,
        x=0.01,
        ha="left",
        fontsize=11,
        fontweight="bold",
        color=plotting.INK if valid else plotting.CRITICAL,
    )
    return fig
