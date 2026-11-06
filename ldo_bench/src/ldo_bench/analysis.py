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
