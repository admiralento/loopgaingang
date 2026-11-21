"""temp-curve: VREF against temperature from one or more temp logs (curve.csv), for the bandgap's parabola.

Per sample (`annotate`): the temperature's rate (C/min, a line fit over temp.rate_window), the
direction (heating / cooling / steady, at temp.direction_threshold), and whether it is a DMM misread:
VREF off VOUT by more than temp.misread_tolerance beyond their usual (median) difference, while VOUT
(the AD2) is where it should be. VREF and VOUT moving together is the reference itself and is kept.

In the curve: samples ramping at most temp.max_rate, not misreads, and outside any excluded time range
(an operator's choice, listed in the summary). They are binned by temperature (temp.curve_bin) for
each direction separately (bins.csv). The combined curve (curve.csv) averages the directions present in
a bin: heating and cooling together cancel most of the sensor's lag behind the die (the TMP235 sits
beside U1). The parabola fit (VREF = v25 + b (T - 25) + c (T - 25)^2) gives the peak and curvature;
the box tempco is (max - min) / (mean x span) over the combined curve.

`merge` combines runs (a cold one with ice, a hot one with the heater): per bin, the mean of each run's
combined value; where runs overlap, the mean VREF difference between them is reported, not corrected.
Nothing here touches an instrument, and nothing in a source run's folder is changed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np

from ldo_bench import plotting
from ldo_bench.config import TempConfig

log = logging.getLogger(__name__)

NAME = "temp-curve"
T_REF = 25.0  # C, the fit's reference temperature
MIN_FIT_BINS = 3
MIN_FIT_SPAN = 5.0  # C


def rate_c_per_min(t_s: np.ndarray, temp_c: np.ndarray, *, window: float) -> np.ndarray:
    """Each sample's temperature rate (C/min): the slope of a line through the samples within
    +/- window / 2 of it (t in s, T in C). NaN where fewer than 3 samples fall in the window."""
    t, x = np.asarray(t_s, float), np.asarray(temp_c, float)
    out = np.full(len(t), np.nan)
    half = window / 2
    lo = np.searchsorted(t, t - half, side="left")
    hi = np.searchsorted(t, t + half, side="right")
    for k in range(len(t)):
        tt, xx = t[lo[k] : hi[k]], x[lo[k] : hi[k]]
        if len(tt) >= 3 and np.ptp(tt) > 0:
            out[k] = np.polyfit(tt, xx, 1)[0] * 60.0
    return out


@dataclass
class CurveResult:
    """One run's curve. rows: the samples with rate_c_per_min, direction, misread, in_curve added."""

    rows: list[dict]
    bins: list[dict]  # bins.csv: one per (bin, direction)
    curve: list[dict]  # curve.csv: one per bin, the directions averaged
    fit: dict | None
    misreads: list[float]  # t_s of each misread sample
    exclude: list[tuple[float, float]]
    issues: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def summary(self) -> dict:
        return {
            "fit": self.fit,
            "bins": len(self.curve),
            "samples_in_curve": sum(1 for r in self.rows if r["in_curve"]),
            "misreads": len(self.misreads),
            "misread_t_s": self.misreads,
            "exclude_s": [list(e) for e in self.exclude],
            "issues": self.issues,
            "notes": self.notes,
        }


def annotate(rows: list[dict], tc: TempConfig, exclude=()) -> tuple[list[dict], list[float], list[str]]:
    """Copies of `rows` with rate_c_per_min, direction, misread and in_curve; also (misread t_s, notes)."""
    out = [dict(r) for r in rows]
    if not out:
        return out, [], []
    t = np.array([float(r["t_s"]) for r in out])
    T = np.array([float(r["temp_c"]) for r in out])
    vref = np.array([float(r["vref_v"]) for r in out])
    vout = np.array([float(r["vout_v"]) for r in out])
    rate = rate_c_per_min(t, T, window=tc.rate_window)
    diff = vref - vout
    finite = np.isfinite(diff)
    usual = float(np.median(diff[finite])) if finite.any() else 0.0
    misread = finite & (np.abs(diff - usual) > tc.misread_tolerance)
    notes = []
    excluded = np.zeros(len(t), bool)
    for a, b in exclude:
        m = (t >= a) & (t <= b)
        excluded |= m
        notes.append(f"{int(m.sum())} samples excluded by the operator ({a:g}-{b:g} s)")
    fast = ~np.isfinite(rate) | (np.abs(rate) > tc.max_rate)
    for k, r in enumerate(out):
        rk = rate[k]
        r["rate_c_per_min"] = float(rk) if np.isfinite(rk) else float("nan")
        if not np.isfinite(rk) or abs(rk) < tc.direction_threshold:
            r["direction"] = "steady"
        else:
            r["direction"] = "heating" if rk > 0 else "cooling"
        r["misread"] = bool(misread[k])
        r["in_curve"] = bool(np.isfinite(vref[k]) and not misread[k] and not fast[k] and not excluded[k])
    n_fast = int((fast & ~misread & ~excluded).sum())
    if n_fast:
        notes.append(
            f"{n_fast} samples ramping faster than max_rate ({tc.max_rate:g} C/min) left out of the curve"
        )
    return out, [float(x) for x in t[misread]], notes


def _bins(rows: list[dict], width: float) -> list[dict]:
    """bins.csv rows: per temperature bin and direction, the in-curve samples' means and spreads."""
    groups: dict[tuple[float, str], list[dict]] = {}
    for r in rows:
        if r["in_curve"]:
            b = float(np.round(r["temp_c"] / width) * width)  # bins centred on multiples of the width
            groups.setdefault((round(b, 6), r["direction"]), []).append(r)
    out = []
    for (b, d), rs in sorted(groups.items()):
        vref = np.array([r["vref_v"] for r in rs])
        vout = np.array([r["vout_v"] for r in rs])
        out.append(
            {
                "bin_c": b,
                "direction": d,
                "n": len(rs),
                "temp_c": float(np.mean([r["temp_c"] for r in rs])),
                "vref_v": float(vref.mean()),
                "vref_std_v": float(vref.std()),
                "vout_v": float(vout.mean()),
                "vout_std_v": float(vout.std()),
                "rate_c_per_min": float(np.mean([r["rate_c_per_min"] for r in rs])),
            }
        )
    return out


def _combine(bins: list[dict]) -> list[dict]:
    """curve.csv rows: per bin, the mean over the directions present (heating and cooling together
    cancel most of the sensor lag)."""
    by_bin: dict[float, dict[str, dict]] = {}
    for b in bins:
        by_bin.setdefault(b["bin_c"], {})[b["direction"]] = b
    out = []
    for b, ds in sorted(by_bin.items()):
        parts = [ds[d] for d in ("heating", "cooling", "steady") if d in ds]
        names = [d for d in ("heating", "cooling", "steady") if d in ds]
        out.append(
            {
                "bin_c": b,
                "temp_c": float(np.mean([p["temp_c"] for p in parts])),
                "vref_v": float(np.mean([p["vref_v"] for p in parts])),
                "vout_v": float(np.mean([p["vout_v"] for p in parts])),
                "n": int(sum(p["n"] for p in parts)),
                "from": "+".join(names),
            }
        )
    return out


def fit_parabola(curve: list[dict]) -> dict | None:
    """VREF = v25 + b (T - 25) + c (T - 25)^2 through the curve's bins; None with fewer than 3 bins or
    under 5 C of span. peak_c / peak_v: the vertex (peak_in_range: inside the measured span)."""
    if len(curve) < MIN_FIT_BINS:
        return None
    T = np.array([c["temp_c"] for c in curve])
    V = np.array([c["vref_v"] for c in curve])
    if np.ptp(T) < MIN_FIT_SPAN:
        return None
    c2, b, v25 = np.polyfit(T - T_REF, V, 2)
    res = V - np.polyval([c2, b, v25], T - T_REF)
    peak_c = T_REF - b / (2 * c2) if c2 != 0 else float("nan")
    peak_v = float(np.polyval([c2, b, v25], peak_c - T_REF)) if c2 != 0 else float("nan")
    return {
        "v25_v": float(v25),
        "b_v_per_c": float(b),
        "c_v_per_c2": float(c2),
        "tc_at_25_ppm_per_c": float(b / v25 * 1e6),
        "curvature_ppm_per_c2": float(c2 / v25 * 1e6),
        "peak_c": float(peak_c),
        "peak_v": peak_v,
        "peak_in_range": bool(T.min() <= peak_c <= T.max()),
        "shape": "peak" if c2 < 0 else "valley",
        "residual_rms_v": float(res.std()),
        "t_min_c": float(T.min()),
        "t_max_c": float(T.max()),
        "box_tc_ppm_per_c": float(np.ptp(V) / (V.mean() * np.ptp(T)) * 1e6),
        "bins": len(curve),
    }


def analyse(rows: list[dict], tc: TempConfig, exclude=()) -> CurveResult:
    """One run's curve (see the module docstring). `exclude`: (start, end) time ranges in s."""
    exclude = [(float(a), float(b)) for a, b in exclude]
    out, misreads, notes = annotate(rows, tc, exclude)
    issues = []
    if out and len(misreads) > tc.misread_max_fraction * len(out):
        issues.append(
            f"{len(misreads)} of {len(out)} samples are DMM misreads (VREF off VOUT by more than "
            f"{tc.misread_tolerance * 1e3:g} mV), above {tc.misread_max_fraction:.0%}: check the DMM's leads "
            "on VREF (TP3) / GND (TP2) and its RS-232 cable"
        )
    elif misreads:
        notes.append(
            f"{len(misreads)} DMM misread(s) left out (VREF off VOUT, VOUT normal) at "
            + ", ".join(f"{x:.0f}" for x in misreads)
            + " s"
        )
    bins = _bins(out, tc.curve_bin)
    wide = [b for b in bins if b["vref_std_v"] > tc.spread_note]
    if wide:
        notes.append(
            f"{len(wide)} bin(s) with a VREF spread above {tc.spread_note * 1e3:g} mV (e.g. {wide[0]['bin_c']:g} C "
            f"{wide[0]['direction']}, {wide[0]['vref_std_v'] * 1e3:.2f} mV): VREF moved there for a reason other "
            "than temperature; look at the log, and exclude the time range if it was a disturbance"
        )
    curve = _combine(bins)
    fit = fit_parabola(curve)
    if fit is None and curve:
        notes.append(f"no fit: it needs {MIN_FIT_BINS}+ bins over {MIN_FIT_SPAN:g} C or more")
    if not curve and out:
        notes.append("no curve: no sample qualified (see the notes above)")
    return CurveResult(out, bins, curve, fit, misreads, exclude, issues, notes)


@dataclass
class MergedCurve:
    """Several runs' curves (temp-curve): `curve` per bin across runs, `bins` every run's bins."""

    labels: list[str]
    runs: list[CurveResult]
    curve: list[dict]
    bins: list[dict]
    overlaps: list[dict]
    fit: dict | None
    issues: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def rows(self) -> list[dict]:
        return self.curve

    def summary(self) -> dict:
        return {
            "valid": not self.issues,
            "issues": self.issues,
            "notes": self.notes,
            "fit": self.fit,
            "overlaps": self.overlaps,
            "bins": len(self.curve),
            "runs": [{"label": lb, **r.summary()} for lb, r in zip(self.labels, self.runs, strict=True)],
        }

    def report(self) -> str:
        lines = [f"VREF curve from {len(self.runs)} run(s): {len(self.curve)} bins"]
        f = self.fit
        if f:
            lines += [
                f"  {f['shape']:<12}  {f['peak_c']:.1f} C, {f['peak_v']:.5f} V"
                + ("" if f["peak_in_range"] else "  (outside the measured range: extrapolated)"),
                f"  curvature     {f['curvature_ppm_per_c2']:+.4f} ppm/C^2",
                f"  tempco @25 C  {f['tc_at_25_ppm_per_c']:+.1f} ppm/C",
                f"  box tempco    {f['box_tc_ppm_per_c']:.1f} ppm/C over {f['t_min_c']:.1f}-{f['t_max_c']:.1f} C",
                f"  fit residual  {f['residual_rms_v'] * 1e6:.1f} uV rms",
            ]
        for ov in self.overlaps:
            lines.append(
                f"  overlap {ov['runs'][0]} / {ov['runs'][1]}: {ov['bins']} bins, "
                f"{ov['runs'][0]} {ov['mean_diff_mv']:+.3f} mV vs {ov['runs'][1]}"
            )
        lines += [f"  note: {n}" for n in self.notes]
        return "\n".join(lines)


def merge(runs: list[tuple[str, CurveResult]]) -> MergedCurve:
    """Combine runs' curves (see the module docstring). Issues and notes carry the run's label."""
    labels = [lb for lb, _ in runs]
    by_bin: dict[float, dict[str, dict]] = {}
    bins = []
    issues, notes = [], []
    for lb, r in runs:
        for c in r.curve:
            by_bin.setdefault(c["bin_c"], {})[lb] = c
        bins += [{"run": lb, **b} for b in r.bins]
        issues += [f"{lb}: {i}" for i in r.issues]
        notes += [f"{lb}: {n}" for n in r.notes]
    curve = []
    for b, per in sorted(by_bin.items()):
        parts = list(per.values())
        curve.append(
            {
                "bin_c": b,
                "temp_c": float(np.mean([p["temp_c"] for p in parts])),
                "vref_v": float(np.mean([p["vref_v"] for p in parts])),
                "vout_v": float(np.mean([p["vout_v"] for p in parts])),
                "n": int(sum(p["n"] for p in parts)),
                "runs": "+".join(per),
                "from": "; ".join(f"{k}: {p['from']}" for k, p in per.items()),
            }
        )
    overlaps = []
    for i in range(len(labels)):
        for j in range(i + 1, len(labels)):
            a, b = labels[i], labels[j]
            d = [per[a]["vref_v"] - per[b]["vref_v"] for per in by_bin.values() if a in per and b in per]
            if d:
                overlaps.append({"runs": [a, b], "bins": len(d), "mean_diff_mv": float(np.mean(d) * 1e3)})
    if len(runs) > 1 and not overlaps:
        notes.append("the runs don't overlap in temperature: their agreement can't be checked")
    return MergedCurve(
        labels, [r for _, r in runs], curve, bins, overlaps, fit_parabola(curve), issues, notes
    )


def plot(m: MergedCurve, dut: str):
    """VREF against T: each run's heating / cooling bins, the combined curve and the parabola fit."""
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(8.0, 5.2), layout="constrained")
    plot_curve(ax, m.bins, m.curve, m.fit)
    plotting.suptitle(
        fig, f"VREF against temperature: {dut}  ·  {len(m.runs)} run(s)", invalid=bool(m.issues)
    )
    if m.issues:
        plotting.issue_box(ax, m.issues)
    return fig


def plot_curve(ax, bins: list[dict], curve: list[dict], fit: dict | None) -> None:
    """Draw bins (heating / cooling / steady), the combined curve and the fit on `ax` (VREF in V)."""
    style = {
        "heating": (plotting.SERIES[1], "^"),
        "cooling": (plotting.SERIES[0], "v"),
        "steady": (plotting.MUTED, "o"),
    }
    for d, (color, marker) in style.items():
        bs = [b for b in bins if b["direction"] == d]
        if bs:
            ax.plot(
                [b["temp_c"] for b in bs],
                [b["vref_v"] for b in bs],
                marker,
                color=color,
                ms=3.5,
                alpha=0.8,
                ls="none",
                label=d,
            )
    if curve:
        ax.plot(
            [c["temp_c"] for c in curve],
            [c["vref_v"] for c in curve],
            "-",
            color=plotting.INK,
            lw=1.4,
            label="combined",
        )
    if fit:
        T = np.linspace(fit["t_min_c"], fit["t_max_c"], 200)
        V = fit["v25_v"] + fit["b_v_per_c"] * (T - T_REF) + fit["c_v_per_c2"] * (T - T_REF) ** 2
        ax.plot(T, V, "--", color=plotting.SERIES[2], lw=1.2, label="parabola fit")
        ax.set_title(
            f"{fit['shape']} {fit['peak_c']:.1f} C  ·  {fit['curvature_ppm_per_c2']:+.3f} ppm/C²  ·  "
            f"box {fit['box_tc_ppm_per_c']:.1f} ppm/C",
            fontsize=10,
            pad=6,
        )
    ax.set_xlabel("T (C), TMP235")
    ax.set_ylabel("VREF (V), DMM")
    if bins or curve:
        ax.legend(loc="best")
