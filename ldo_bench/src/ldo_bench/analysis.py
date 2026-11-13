"""Pure-numpy analysis. No instrument code here, so it is easy to unit-test.

Every function takes plain arrays (volts, amps, seconds) and returns a frozen dataclass whose
`as_dict()` goes into summary.json. NaN means "not measurable from this data" (e.g. the LDO
never settled within the capture), never an error.

  sweep_points                                                    every VIN sweep
  linear_fit / load_regulation / line_regulation / find_dropout   DC tests
  dropout_strays                                                  line-reg, vref-line
  settling_analysis (with find_step, smooth)                      load-tran, line-tran
  schmitt_analysis / propagation_delays                           enable-sine
  step_metrics                                                    enable-startup
  sine_fit / multi_sine_fit                                       psrr
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

import numpy as np


@dataclass(frozen=True)
class LinearFit:
    """y = slope * x + intercept; r2 is the coefficient of determination (1 = perfect fit)."""

    slope: float
    intercept: float
    r2: float

    def __call__(self, x):
        """Evaluate the fit line at x."""
        return self.slope * np.asarray(x) + self.intercept


def linear_fit(x, y) -> LinearFit:
    """Least-squares straight line through (x, y). All NaN if there are < 2 points or x doesn't vary."""
    x, y = np.asarray(x, float), np.asarray(y, float)
    if len(x) < 2 or np.ptp(x) == 0:
        return LinearFit(float("nan"), float("nan"), float("nan"))
    slope, intercept = np.polyfit(x, y, 1)
    resid = y - (slope * x + intercept)
    ss_tot = float(np.sum((y - y.mean()) ** 2))
    r2 = 1.0 - float(np.sum(resid**2)) / ss_tot if ss_tot > 0 else 1.0
    return LinearFit(float(slope), float(intercept), r2)


@dataclass(frozen=True)
class LoadRegulation:
    """Load regulation over the measured current range.

    `slope` is dVout/dIload from a least-squares fit (V/A, which is also mV/mA);
    it is negative for a normal LDO, and -slope is the DC output resistance.
    `delta_v` = Vout(lightest load) - Vout(heaviest load).

    i_min / i_max: lightest / heaviest load current measured (A).
    v_at_i_min / v_at_i_max: VOUT at those points (V), raw readings, not the fit.
    intercept: fit VOUT at zero load (V). r2: fit quality.
    """

    i_min: float
    i_max: float
    v_at_i_min: float
    v_at_i_max: float
    delta_v: float
    percent: float  # delta_v / Vout(lightest load) * 100
    slope: float
    intercept: float
    r2: float

    @property
    def r_out(self) -> float:
        """DC output resistance (ohm) = -dVout/dIload."""
        return -self.slope

    def as_dict(self) -> dict:
        """Fields plus r_out, for summary.json."""
        return {**asdict(self), "r_out": self.r_out}


def load_regulation(i_load, vout) -> LoadRegulation:
    """Load regulation from VOUT (V) measured at each load current (A).

    The endpoints are the points with the lowest and highest current (not the first and last
    rows); the slope is a least-squares fit over all points.
    """
    i, v = np.asarray(i_load, float), np.asarray(vout, float)
    lo, hi = int(np.argmin(i)), int(np.argmax(i))
    fit = linear_fit(i, v)
    dv = float(v[lo] - v[hi])
    return LoadRegulation(
        i_min=float(i[lo]),
        i_max=float(i[hi]),
        v_at_i_min=float(v[lo]),
        v_at_i_max=float(v[hi]),
        delta_v=dv,
        percent=100.0 * dv / float(v[lo]) if v[lo] else float("nan"),
        slope=fit.slope,
        intercept=fit.intercept,
        r2=fit.r2,
    )


@dataclass(frozen=True)
class LineRegulation:
    """Line regulation over the in-regulation part of a VIN sweep.

    `slope` is dVOUT/dVIN from a fit over [fit_vin_min, fit_vin_max] (V/V).
    Dropout: VOUT reference = VOUT at the highest VIN; the dropout point is
    where VOUT falls `dropout_fraction` below it (interpolated), and
    `dropout_voltage` = VIN - VOUT there. NaN if the sweep never reached it.

    v_ref: VOUT at the highest VIN (V). fit_vin_min / fit_vin_max: VIN range actually fitted (V).
    dropout_vin: VIN at the dropout point (V).
    """

    v_ref: float
    slope: float
    intercept: float
    r2: float
    fit_vin_min: float
    fit_vin_max: float
    delta_v: float  # VOUT change across the fit range (fit line)
    percent_per_v: float  # slope / v_ref * 100
    dropout_vin: float
    dropout_voltage: float
    dropout_fraction: float

    def as_dict(self) -> dict:
        """Fields as a dict, for summary.json."""
        return asdict(self)


def _below(vin, vout, fraction: float):
    """VIN-sorted (x, y, threshold, below-threshold mask) for the dropout functions."""
    order = np.argsort(vin)
    x, y = np.asarray(vin, float)[order], np.asarray(vout, float)[order]
    threshold = (1.0 - fraction) * y[-1]
    return x, y, threshold, y < threshold


def sweep_points(start: float, stop: float, step: float) -> np.ndarray:
    """Setpoints (V) from start towards stop (up or down) in `step`, always ending exactly on stop."""
    n = int(np.floor(abs(stop - start) / step + 1e-9)) + 1  # 1e-9: float error mustn't drop the last step
    pts = start + np.sign(stop - start) * step * np.arange(n)
    if abs(pts[-1] - stop) > 1e-9:
        pts = np.append(pts, stop)
    return np.round(pts, 6)


def find_dropout(vin, vout, fraction: float) -> tuple[float, float]:
    """(VIN, VOUT) where VOUT first rises into regulation, coming up from the bottom of the sweep.

    vin, vout in V (any order; sorted by VIN here). The threshold is (1 - fraction) * VOUT at
    the highest VIN. The dropout region is the run of points below it starting at the lowest VIN;
    the returned point is where VOUT crosses up out of that run, linearly interpolated between the
    two samples either side, so the returned VOUT is the threshold. A low reading further up the
    sweep doesn't move it (see dropout_strays). (NaN, NaN) if the lowest point is already in
    regulation, or VOUT never comes up.
    """
    x, y, threshold, below = _below(vin, vout, fraction)
    if not below[0]:
        return float("nan"), float("nan")  # in regulation from the bottom of the sweep
    above = np.nonzero(~below)[0]
    if not len(above):
        return float("nan"), float("nan")  # never regulated
    k = int(above[0])  # first point out of the bottom dropout run
    x0, x1, y0, y1 = x[k - 1], x[k], y[k - 1], y[k]
    xc = x0 + (threshold - y0) * (x1 - x0) / (y1 - y0) if y1 != y0 else x1
    return float(xc), float(threshold)


def dropout_strays(vin, vout, fraction: float) -> list[float]:
    """VIN (V) of points below the dropout threshold that sit above an in-regulation point.

    In a clean sweep VOUT is below the threshold only at the bottom (dropout). A low point higher
    up is a bad reading or the LDO misbehaving; line-reg reports it instead of moving the knee.
    """
    x, _, _, below = _below(vin, vout, fraction)
    above = np.nonzero(~below)[0]
    if not len(above):
        return []
    return [float(v) for v in x[above[0] :][below[above[0] :]]]


def line_regulation(
    vin, vout, *, dropout_fraction: float = 0.01, fit_vin_min: float | None = None, knee_margin: float = 0.0
) -> LineRegulation:
    """Line regulation from a VIN sweep (vin, vout in V).

    Finds the dropout point (find_dropout), then fits VOUT vs VIN over VIN >= fit_vin_min.
    `knee_margin` (V): how far above the dropout point the fit starts when fit_vin_min is not
    given; with no dropout point in the sweep, the fit uses every point.
    """
    x, y = np.asarray(vin, float), np.asarray(vout, float)
    v_ref = float(y[np.argmax(x)])
    d_vin, d_vout = find_dropout(x, y, dropout_fraction)
    if fit_vin_min is None:
        fit_vin_min = d_vin + knee_margin if np.isfinite(d_vin) else float(x.min())
    mask = x >= fit_vin_min - 1e-9
    fit = linear_fit(x[mask], y[mask])
    lo, hi = (float(x[mask].min()), float(x[mask].max())) if mask.any() else (float("nan"),) * 2
    return LineRegulation(
        v_ref=v_ref,
        slope=fit.slope,
        intercept=fit.intercept,
        r2=fit.r2,
        fit_vin_min=lo,
        fit_vin_max=hi,
        delta_v=fit.slope * (hi - lo),
        percent_per_v=100.0 * fit.slope / v_ref if v_ref else float("nan"),
        dropout_vin=d_vin,
        dropout_voltage=d_vin - d_vout,
        dropout_fraction=dropout_fraction,
    )


# --- transient settling (port of AD_workspaces/load_tran_script.js) -------------------------------
@dataclass(frozen=True)
class SettlingResult:
    """Step response metrics, same definitions as the WaveForms custom measurements.

    nominal: mean VOUT before the step. peak: largest excursion of the smoothed trace
    within `peak_window` after the step. band: settle_fraction * |peak - nominal|.
    settling_time: from the step until the smoothed trace enters [level - band,
    level + band] for the last time, where level is `nominal` ('pre', like the
    WaveForms script) or the final VOUT ('post'). NaN if it never settles in the
    capture. noise: RMS of the smoothed trace before the step; should be <= band / 3.

    All voltages in V, settling_time in s (measured from the step). settle_level: the level the
    band is centred on. final: mean smoothed VOUT over the last 10 % of the capture (V).
    sample_rate: Hz. step_index: sample index of the step (-1 if no edge was found).
    reference: 'pre' or 'post', as passed in.
    """

    nominal: float
    peak: float
    band: float
    settle_level: float
    settling_time: float
    noise: float
    final: float
    sample_rate: float
    step_index: int
    reference: str

    @property
    def deviation(self) -> float:
        """Signed peak excursion (V): negative for a dip (load step up), positive for overshoot."""
        return self.peak - self.nominal

    @property
    def upper(self) -> float:
        """Top of the settling band (V)."""
        return self.settle_level + self.band

    @property
    def lower(self) -> float:
        """Bottom of the settling band (V)."""
        return self.settle_level - self.band

    @property
    def trustworthy(self) -> bool:
        """Noise well inside the band, else noise sets the settling time (see AD_workspaces/README)."""
        return bool(np.isfinite(self.noise) and np.isfinite(self.band) and self.noise <= self.band / 3)

    def as_dict(self) -> dict:
        """Fields plus the derived properties, for summary.json."""
        return {
            **asdict(self),
            "deviation": self.deviation,
            "upper": self.upper,
            "lower": self.lower,
            "trustworthy": self.trustworthy,
        }


def _js_round(x: float) -> int:
    """JavaScript Math.round (half up), not Python's banker's rounding."""
    return int(np.floor(x + 0.5))


def find_step(ref, edge: str) -> int:
    """First crossing of the ref channel's midpoint in the given direction, or -1.

    Midpoint = (min + max) / 2 of the trace; returns the index of the first sample at or past it.
    `edge` is 'rising' or 'falling' (anything else is treated as falling).
    """
    s = np.asarray(ref, float)
    mid = (s.min() + s.max()) / 2
    if edge == "rising":
        idx = np.nonzero((s[:-1] < mid) & (s[1:] >= mid))[0]
    else:
        idx = np.nonzero((s[:-1] > mid) & (s[1:] <= mid))[0]
    return int(idx[0]) + 1 if len(idx) else -1


def smooth(x, h: int) -> np.ndarray:
    """Centred moving average over i-h..i+h, shrinking at the ends (as in the JS)."""
    x = np.asarray(x, float)
    n = len(x)
    p = np.concatenate([[0.0], np.cumsum(x)])  # prefix sums: sum(x[a:b]) = p[b] - p[a]
    i = np.arange(n)
    a, b = np.maximum(0, i - h), np.minimum(n, i + h + 1)
    return (p[b] - p[a]) / (b - a)


def settling_analysis(
    vout,
    ref,
    sample_rate: float,
    edge: str,
    *,
    settle_fraction: float = 0.1,
    smooth_time: float = 1e-6,
    peak_window: float = 20e-6,
    blank_time: float = 0.5e-6,
    reference: str = "pre",
) -> tuple[SettlingResult, np.ndarray]:
    """Return (metrics, smoothed VOUT). Step numbers match load_tran_script.js.

    A port of the WaveForms custom-measurement JavaScript in ../AD_workspaces/load_tran_script.js,
    so the bench and a manual WaveForms session give the same numbers (including JS rounding).

    vout, ref: one capture of VOUT and of the reference / trigger channel (V), same length.
    sample_rate: Hz. edge: 'rising' or 'falling' on `ref`. The keyword arguments are
    [analysis] settings (fractions, times in s); see AnalysisConfig.

    Algorithm: find the step on `ref`; blank the switching glitch +/- blank_time with a straight
    line; smooth with a smooth_time moving average; nominal = mean before the step; peak = the
    largest deviation within peak_window; band = settle_fraction * |peak - nominal|; settling
    time = after the last smoothed sample outside the band. All NaN if no edge is found or it
    is too close to the start of the capture.
    """
    d = np.asarray(vout, float)
    n = len(d)
    dt = 1.0 / sample_rate
    nan = float("nan")

    # 1. step = ref channel edge through its midpoint
    i_step = find_step(ref, edge)
    nb = max(1, _js_round(blank_time / dt))  # blanking half-width (samples)
    i_a, i_b = i_step - nb, min(n - 1, i_step + nb)
    if i_step < 0 or i_a <= 10:  # no edge, or too few samples before it for a nominal
        return SettlingResult(nan, nan, nan, nan, nan, nan, nan, sample_rate, i_step, reference), d.copy()

    # 2. nominal = mean of raw VOUT before the step (excluding the glitch)
    v0 = float(d[:i_a].mean())

    # 3. replace the switching glitch with a straight line
    x = d.copy()
    k = np.arange(i_a + 1, i_b)
    x[k] = d[i_a] + (d[i_b] - d[i_a]) * (k - i_a) / (i_b - i_a)

    # 4. centred moving average
    h = max(1, int(np.floor(smooth_time / dt / 2)))
    f = smooth(x, h)

    # 5. noise = RMS of smoothed VOUT around nominal, before the step
    pre = f[h : i_a - h]
    noise = float(np.sqrt(np.mean((pre - v0) ** 2))) if len(pre) else nan

    # 6. peak = largest deviation from nominal within peak_window after the step
    i_end = min(n, i_step + _js_round(peak_window / dt))
    i_pk = i_b + int(np.argmax(np.abs(f[i_b:i_end] - v0))) if i_end > i_b else i_b
    v_pk = float(f[i_pk])
    band = settle_fraction * abs(v_pk - v0)

    # 7. settling time = after the last smoothed sample outside the band
    i_stop = n - h  # the end, where the average is one-sided, is ignored
    tail = f[max(i_stop - max(1, n // 10), i_step) : i_stop]
    final = float(tail.mean()) if len(tail) else nan
    level = v0 if reference == "pre" else final
    outside = np.nonzero(np.abs(f[i_step:i_stop] - level) > band)[0]
    if i_stop <= i_step:
        t_set = nan  # the step is in the last h samples: nothing after it to judge (the JS says 0)
    elif not len(outside):
        t_set = 0.0
    else:
        i_last = i_step + int(outside[-1])
        # still outside at the last usable sample: it never settled within the capture
        t_set = (i_last + 1 - i_step) * dt if i_last < i_stop - 1 else nan

    return (
        SettlingResult(v0, v_pk, band, level, t_set, noise, final, sample_rate, i_step, reference),
        f,
    )


# --- enable block: Schmitt / comparator thresholds from a sine input ------------------------------
@dataclass(frozen=True)
class SchmittResult:
    """Thresholds and output of a comparator driven by a slow sine.

    vt_rise / vt_fall: input voltage when the output crosses its midpoint going up / down (median over
    the edges in the capture). At 1 kHz into a 0-1.8 V sine, a 100 ns output delay shifts these by < 1 mV.
    edges_per_cycle: output rising edges per input cycle; > 1 means it chatters on each crossing.

    voh / vol: output high / low levels (V), the medians above / below the midpoint of the output's range.
    n_rise / n_fall: output edges found. input_cycles: sine cycles in the capture.
    For an inverting stage, vt_rise is the input level on the *falling* input crossing, so the
    hysteresis comes out negative.
    """

    vt_rise: float
    vt_fall: float
    voh: float
    vol: float
    t_rise: float  # 10-90 %, s (median); limited by the sample interval
    t_fall: float
    duty: float  # fraction of time the output is high
    n_rise: int
    n_fall: int
    input_cycles: float
    edges_per_cycle: float

    @property
    def hysteresis(self) -> float:
        """vt_rise - vt_fall (V)."""
        return self.vt_rise - self.vt_fall

    def as_dict(self) -> dict:
        """Fields plus hysteresis, for summary.json."""
        return {**asdict(self), "hysteresis": self.hysteresis}


def _crossings(x: np.ndarray, level: float, rising: bool) -> np.ndarray:
    """Fractional sample indices where x crosses `level` in one direction."""
    a, b = x[:-1], x[1:]
    k = np.nonzero((a < level) & (b >= level))[0] if rising else np.nonzero((a > level) & (b <= level))[0]
    return k + (level - a[k]) / (b[k] - a[k])


def _edge_time(x: np.ndarray, k: float, lo: float, hi: float, rising: bool, fs: float, span: int) -> float:
    """10-90 % time of the edge around fractional index k (searching +/- span samples)."""
    i0, i1 = max(0, int(k) - span), min(len(x), int(k) + span + 2)
    seg = x[i0:i1]
    c_lo = _crossings(seg, lo, rising)
    c_hi = _crossings(seg, hi, rising)
    if not len(c_lo) or not len(c_hi):
        return float("nan")
    rel = k - i0
    a = c_lo[np.argmin(np.abs(c_lo - rel))]
    b = c_hi[np.argmin(np.abs(c_hi - rel))]
    return abs(b - a) / fs


@dataclass(frozen=True)
class PropagationDelays:
    """Delays from the input's midpoint crossings to the output's (s), from a square-wave capture.

    lh / hl name the OUTPUT edge: lh = output low -> high, hl = output high -> low (mean over the
    capture's edges; _std is their spread). `inverting`: the output rises when the input falls
    (e.g. ~EN_OUT). NaN when the output never switched after an input edge.
    """

    lh: float
    hl: float
    lh_std: float
    hl_std: float
    n_lh: int
    n_hl: int
    inverting: bool

    def as_dict(self) -> dict:
        """Fields as a dict, for data.csv / summary.json."""
        return asdict(self)


def _two_levels(x: np.ndarray) -> tuple[float, float]:
    """(low, high) of a two-level signal: medians either side of the 0.5 / 99.5 % midpoint."""
    lo_x, hi_x = float(np.percentile(x, 0.5)), float(np.percentile(x, 99.5))
    split = (lo_x + hi_x) / 2
    lows, highs = x[x <= split], x[x > split]
    return (float(np.median(lows)) if len(lows) else lo_x, float(np.median(highs)) if len(highs) else hi_x)


def propagation_delays(ain, aout, sample_rate: float) -> PropagationDelays:
    """Input-to-output delay of a logic block driven by a square wave (ain, aout in V; Hz).

    Each input midpoint crossing (sub-sample interpolated) is paired with the output's first
    midpoint crossing after it and before the next input crossing; an input edge the output
    doesn't follow is skipped. The delays are split by the output's direction.
    """
    vin, vout = np.asarray(ain, float), np.asarray(aout, float)
    in_mid, out_mid = sum(_two_levels(vin)) / 2, sum(_two_levels(vout)) / 2
    ins = sorted(
        [(k, True) for k in _crossings(vin, in_mid, True)]
        + [(k, False) for k in _crossings(vin, in_mid, False)]
    )
    outs = sorted(
        [(k, True) for k in _crossings(vout, out_mid, True)]
        + [(k, False) for k in _crossings(vout, out_mid, False)]
    )
    out_idx = np.array([k for k, _ in outs])
    lh, hl, flips = [], [], []
    for n, (k, rising_in) in enumerate(ins):
        nxt = ins[n + 1][0] if n + 1 < len(ins) else np.inf
        j = int(np.searchsorted(out_idx, k, side="right"))
        if j >= len(outs) or outs[j][0] >= nxt:
            continue
        k_out, rising_out = outs[j]
        (lh if rising_out else hl).append((k_out - k) / sample_rate)
        flips.append(rising_out != rising_in)

    def stat(v: list[float]) -> tuple[float, float]:
        return (float(np.mean(v)), float(np.std(v))) if v else (float("nan"), float("nan"))

    (m_lh, s_lh), (m_hl, s_hl) = stat(lh), stat(hl)
    return PropagationDelays(m_lh, m_hl, s_lh, s_hl, len(lh), len(hl), bool(flips) and np.mean(flips) > 0.5)


def schmitt_analysis(ain, aout, sample_rate: float, frequency: float) -> SchmittResult:
    """Comparator thresholds from one capture of its sine input `ain` and output `aout` (V).

    sample_rate and frequency (the input sine's) in Hz. The output's midpoint crossings are
    found with sub-sample interpolation, and the input voltage at each crossing (interpolated)
    gives the thresholds; the median over all edges is reported.
    """
    vin, vout = np.asarray(ain, float), np.asarray(aout, float)
    # Two-level split rather than 5th / 95th percentiles: those both land on one level when the
    # output spends under 5 % of the time at the other (a threshold near a sine extreme).
    lo_x, hi_x = float(np.percentile(vout, 0.5)), float(np.percentile(vout, 99.5))
    split = (lo_x + hi_x) / 2
    lows, highs = vout[vout <= split], vout[vout > split]
    vol = float(np.median(lows)) if len(lows) else lo_x
    voh = float(np.median(highs)) if len(highs) else hi_x
    mid = (vol + voh) / 2
    rise, fall = _crossings(vout, mid, True), _crossings(vout, mid, False)

    def at(idx: np.ndarray) -> float:
        return float(np.median(np.interp(idx, np.arange(len(vin)), vin))) if len(idx) else float("nan")

    span = max(4, int(0.05 * sample_rate / frequency))  # search 5 % of a period around each edge
    swing = voh - vol
    t_r = [_edge_time(vout, k, vol + 0.1 * swing, vol + 0.9 * swing, True, sample_rate, span) for k in rise]
    t_f = [_edge_time(vout, k, voh - 0.1 * swing, voh - 0.9 * swing, False, sample_rate, span) for k in fall]
    cycles = len(vin) / sample_rate * frequency
    return SchmittResult(
        vt_rise=at(rise),
        vt_fall=at(fall),
        voh=voh,
        vol=vol,
        t_rise=float(np.nanmedian(t_r)) if np.isfinite(t_r).any() else float("nan"),
        t_fall=float(np.nanmedian(t_f)) if np.isfinite(t_f).any() else float("nan"),
        duty=float(np.mean(vout > mid)),
        n_rise=len(rise),
        n_fall=len(fall),
        input_cycles=cycles,
        edges_per_cycle=len(rise) / cycles if cycles else float("nan"),
    )


# --- turn-on / turn-off: delay, rise time, settling of a signal after a known edge at t = 0 -------
@dataclass(frozen=True)
class StepMetrics:
    """A signal's response to an edge at t = 0 (e.g. VOUT or VREF after the enable edge).

    before / after: mean before t = 0 and over the last 10 % of the capture.
    t10 / t90: time from the edge to 10 % / 90 % of the way from before to after (startup delay and
    rise = t90 - t10). t_settle: after the last sample outside +/- settle_fraction of the step around
    `after` (NaN if still outside at the end). overshoot: beyond `after`, as a fraction of the step.
    peak: the most extreme value after t = 0 (e.g. peak inrush current).
    """

    before: float
    after: float
    t10: float
    t90: float
    t_settle: float
    overshoot: float
    peak: float

    @property
    def step(self) -> float:
        """after - before (same unit as the signal)."""
        return self.after - self.before

    @property
    def rise_time(self) -> float:
        """10-90 % time (s); also the fall time for a falling step."""
        return self.t90 - self.t10

    def as_dict(self) -> dict:
        """Fields plus step and rise_time, for summary.json."""
        return {**asdict(self), "step": self.step, "rise_time": self.rise_time}


def step_metrics(t, x, settle_fraction: float = 0.02) -> StepMetrics:
    """Delay, rise time, settling and overshoot of `x` after an edge at t = 0.

    t: time (s) with the edge at 0 (negative before it). x: the signal (V, or A for currents).
    The signal is normalised so 0 = before and 1 = after; times are the first sample at or past
    10 % / 90 %, which works for rising and falling steps alike. Times are NaN if not reached.
    """
    t, x = np.asarray(t, float), np.asarray(x, float)
    pre, post = x[t < 0], x[t >= 0]
    tp = t[t >= 0]
    before = float(pre.mean()) if len(pre) else float(x[0])
    after = float(post[-max(1, len(post) // 10) :].mean()) if len(post) else float("nan")
    step = after - before
    nan = float("nan")
    if not len(post) or step == 0:
        return StepMetrics(before, after, nan, nan, nan, nan, float(post.max()) if len(post) else nan)
    up = step > 0
    norm = (post - before) / step  # 0 before, 1 at the final level

    def first_cross(frac: float) -> float:
        k = np.nonzero(norm >= frac)[0]
        return float(tp[k[0]]) if len(k) else nan

    outside = np.nonzero(np.abs(norm - 1) > settle_fraction)[0]
    if not len(outside):
        t_settle = 0.0
    elif outside[-1] < len(post) - max(1, len(post) // 10):
        t_settle = float(tp[outside[-1] + 1])
    else:
        t_settle = nan  # still outside the band in the final 10 %
    peak = float(post.max() if up else post.min())
    return StepMetrics(
        before=before,
        after=after,
        t10=first_cross(0.1),
        t90=first_cross(0.9),
        t_settle=t_settle,
        overshoot=float(max(0.0, norm.max() - 1)),
        peak=peak,
    )


@dataclass(frozen=True)
class SineFit:
    """Result of sine_fit: the tone at the known frequency in one channel.

    phasor: complex amplitude (peak, V) with phase relative to the centre of the capture.
    dc: offset (V) at the centre of the capture. n: samples fitted.
    """

    phasor: complex  # x ~ dc + Re(phasor * exp(j 2 pi f t)) + drift * t
    dc: float
    resid_rms: float  # what the sine doesn't explain: noise, harmonics, other tones
    n: int

    @property
    def amplitude(self) -> float:
        """Peak amplitude (V), not peak-to-peak."""
        return abs(self.phasor)

    @property
    def amplitude_noise(self) -> float:
        """1-sigma uncertainty of each quadrature of the phasor (white noise, least squares)."""
        return self.resid_rms * float(np.sqrt(2.0 / self.n)) if self.n else float("nan")


def sine_fit(x, sample_rate: float, frequency: float) -> SineFit:
    """Least-squares fit of a sine at a known frequency, plus offset and a linear drift (lock-in).

    The fit window doesn't need a whole number of cycles. Time is centred on the capture, so two
    channels from the same capture share a phase reference and their phasor ratio is the transfer.
    x in V, sample_rate and frequency in Hz.
    """
    x = np.asarray(x, float)
    n = len(x)
    t = (np.arange(n) - (n - 1) / 2) / sample_rate
    w = 2 * np.pi * frequency * t
    a = np.column_stack([np.cos(w), np.sin(w), np.ones(n), t])
    coef, *_ = np.linalg.lstsq(a, x, rcond=None)
    resid = x - a @ coef
    dof = max(1, n - 4)  # 4 fitted parameters
    # x = a cos + b sin = Re((a - jb) e^{jwt}), hence the minus sign on the sine coefficient
    return SineFit(complex(coef[0], -coef[1]), float(coef[2]), float(np.sqrt(np.sum(resid**2) / dof)), n)


def multi_sine_fit(x, sample_rate: float, frequencies) -> list[complex]:
    """Phasors (V peak, same convention as sine_fit) of several tones fitted together, plus offset
    and drift. Fitting jointly keeps each tone's leakage out of the others, e.g. so noise fitted
    next to a strong tone doesn't include that tone.
    """
    x = np.asarray(x, float)
    n = len(x)
    t = (np.arange(n) - (n - 1) / 2) / sample_rate
    cols = []
    for f in frequencies:
        w = 2 * np.pi * f * t
        cols += [np.cos(w), np.sin(w)]
    a = np.column_stack([*cols, np.ones(n), t])
    coef, *_ = np.linalg.lstsq(a, x, rcond=None)
    return [complex(coef[2 * k], -coef[2 * k + 1]) for k in range(len(frequencies))]
