"""Pure-numpy analysis. No instrument code here, so it is easy to unit-test."""

from __future__ import annotations

from dataclasses import asdict, dataclass

import numpy as np


@dataclass(frozen=True)
class LinearFit:
    slope: float
    intercept: float
    r2: float

    def __call__(self, x):
        return self.slope * np.asarray(x) + self.intercept


def linear_fit(x, y) -> LinearFit:
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
        return -self.slope

    def as_dict(self) -> dict:
        return {**asdict(self), "r_out": self.r_out}


def load_regulation(i_load, vout) -> LoadRegulation:
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
        return asdict(self)


def find_dropout(vin, vout, fraction: float) -> tuple[float, float]:
    """(VIN, VOUT) where VOUT first stays within `fraction` of its max-VIN value (interpolated)."""
    order = np.argsort(vin)
    x, y = np.asarray(vin, float)[order], np.asarray(vout, float)[order]
    threshold = (1.0 - fraction) * y[-1]
    below = np.nonzero(y < threshold)[0]
    if not len(below):
        return float("nan"), float("nan")  # in regulation over the whole sweep
    k = int(below[-1]) + 1  # first point of the final in-regulation run
    if k >= len(x):
        return float("nan"), float("nan")  # never regulated
    x0, x1, y0, y1 = x[k - 1], x[k], y[k - 1], y[k]
    xc = x0 + (threshold - y0) * (x1 - x0) / (y1 - y0) if y1 != y0 else x1
    return float(xc), float(threshold)


def line_regulation(
    vin, vout, *, dropout_fraction: float = 0.01, fit_vin_min: float | None = None, knee_margin: float = 0.0
) -> LineRegulation:
    """`knee_margin`: how far above the dropout point the fit starts when fit_vin_min is not given."""
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
