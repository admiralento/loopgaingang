"""sa-check: the HP 8591E on its own calibrator, before any measurement relies on it.

Wiring: the analyzer's CAL OUT cabled to its INPUT (50 ohm), nothing else. No other instrument and
no board are involved.

run() steps:
1. Preset (IP) and single-sweep mode.
2. Calibrator: tune to sa.cal_frequency (300 MHz) with sa.cal_span / sa.cal_rbw, one sweep, marker to
   the peak. Its frequency must be within the span / 100 (or one RBW) of cal_frequency, and its level
   within sa.cal_tolerance_db of sa.cal_level (-20 dBm). Either failing is a setup issue (exit 3):
   CAL OUT not cabled to INPUT, or the analyzer or the driver reading wrong.
3. Narrowest RBW: ask for 30 Hz and read back what the analyzer set (30 Hz means Option 130 is fitted;
   1 kHz is the standard narrowest).
4. Noise floor: at sa.floor_frequency (1 MHz, no tone there) with that RBW, one sweep, the marker level.
   For the record: it sets the smallest tone the PSRR measurement could see.
5. Preset again (IP only: continuous sweep), so the front panel is left in its normal state.

Every command is checked with ERR?, and every reply is parsed by the driver: a command the analyzer
rejects, or a reply that isn't a reading, stops the run (InstrumentError, exit 1).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from ldo_bench.config import SaConfig
from ldo_bench.instruments.hp859x import HP859x, Marker

log = logging.getLogger(__name__)

NAME = "sa-check"
NARROW_RBW_ASK = 30.0  # Hz: the narrowest RBW the 8590 series has (with Option 130)
FLOOR_SPAN = 10e3  # Hz around floor_frequency
NO_TONE_DB = 20.0  # dB: a "calibrator" this far below cal_level is noise, not CAL OUT


@dataclass
class SaCheckResult:
    """What sa-check measured. Levels in dBm, frequencies and bandwidths in Hz."""

    idn: str
    cfg: SaConfig
    cal: Marker
    cal_rbw: float
    narrowest_rbw: float
    floor: Marker
    issues: list[str] = field(default_factory=list)

    @property
    def cal_error_db(self) -> float:
        return self.cal.dbm - self.cfg.cal_level

    @property
    def rows(self) -> list[dict]:
        return [
            {
                "step": "calibrator",
                "frequency_hz": self.cal.frequency,
                "level_dbm": self.cal.dbm,
                "expected_dbm": self.cfg.cal_level,
                "error_db": self.cal_error_db,
                "rbw_hz": self.cal_rbw,
            },
            {
                "step": "noise floor",
                "frequency_hz": self.floor.frequency,
                "level_dbm": self.floor.dbm,
                "expected_dbm": float("nan"),
                "error_db": float("nan"),
                "rbw_hz": self.narrowest_rbw,
            },
        ]

    def summary(self) -> dict:
        return {
            "valid": not self.issues,
            "issues": self.issues,
            "analyzer": self.idn,
            "cal_frequency_hz": self.cal.frequency,
            "cal_level_dbm": self.cal.dbm,
            "cal_error_db": self.cal_error_db,
            "cal_tolerance_db": self.cfg.cal_tolerance_db,
            "narrowest_rbw_hz": self.narrowest_rbw,
            "option_130": self.narrowest_rbw <= NARROW_RBW_ASK + 1e-9,
            "floor_frequency_hz": self.floor.frequency,
            "floor_dbm": self.floor.dbm,
            "floor_vpeak_v": self.floor.vpeak,
        }

    def report(self) -> str:
        c = self.cfg
        return "\n".join(
            [
                f"Spectrum analyzer check: {self.idn}",
                f"  calibrator  {self.cal.dbm:+.2f} dBm at {self.cal.frequency / 1e6:.4f} MHz "
                f"(expected {c.cal_level:+.1f} dBm, error {self.cal_error_db:+.2f} dB, "
                f"tolerance ±{c.cal_tolerance_db:g} dB; RBW {self.cal_rbw:g} Hz)",
                f"  narrowest RBW  {self.narrowest_rbw:g} Hz"
                + (
                    "  (Option 130 fitted)"
                    if self.narrowest_rbw <= NARROW_RBW_ASK + 1e-9
                    else "  (no Option 130)"
                ),
                f"  noise floor  {self.floor.dbm:+.1f} dBm at {self.floor.frequency / 1e6:g} MHz in "
                f"{self.narrowest_rbw:g} Hz RBW = {self.floor.vpeak * 1e6:.2f} µV peak into 50 Ω",
            ]
        )


def wiring() -> list[str]:
    """Wiring lines shown before the run."""
    return ["8591E CAL OUT -> 8591E INPUT 50 Ω (a short cable), nothing else on the input"]


def run(sa: HP859x, sc: SaConfig) -> SaCheckResult:
    """The steps in the module docstring. Setup problems are issues; analyzer errors raise."""
    issues: list[str] = []
    sa.preset()
    cal_rbw = sa.tune(center=sc.cal_frequency, span=sc.cal_span, rbw=sc.cal_rbw, ref_level_dbm=-10.0)
    sa.sweep()
    cal = sa.peak()
    log.info("calibrator: %.2f dBm at %.6f MHz", cal.dbm, cal.frequency / 1e6)
    f_tol = max(sc.cal_span / 100, cal_rbw)
    if cal.dbm < sc.cal_level - NO_TONE_DB:
        issues.append(
            f"no calibrator tone: the highest peak near {sc.cal_frequency / 1e6:g} MHz reads {cal.dbm:+.1f} dBm "
            f"(noise; expected {sc.cal_level:+g} dBm): is CAL OUT cabled to INPUT?"
        )
    elif abs(cal.frequency - sc.cal_frequency) > f_tol:
        issues.append(
            f"the highest peak is at {cal.frequency / 1e6:.4f} MHz, not CAL OUT's {sc.cal_frequency / 1e6:g} MHz "
            f"(±{f_tol / 1e3:g} kHz): is CAL OUT cabled to INPUT?"
        )
    elif abs(cal.dbm - sc.cal_level) > sc.cal_tolerance_db:
        issues.append(
            f"the calibrator reads {cal.dbm:+.2f} dBm, {cal.dbm - sc.cal_level:+.2f} dB from {sc.cal_level:+g} dBm "
            f"(tolerance ±{sc.cal_tolerance_db:g} dB): check the cable, or the amplitude reading"
        )
    narrowest = sa.tune(center=sc.floor_frequency, span=FLOOR_SPAN, rbw=NARROW_RBW_ASK, ref_level_dbm=-30.0)
    sa.sweep()
    floor = sa.marker_at(sc.floor_frequency)
    log.info("noise floor: %.1f dBm at %g Hz in %g Hz RBW", floor.dbm, sc.floor_frequency, narrowest)
    sa.front_panel()
    for i in issues:
        log.warning("sa-check: %s", i)
    return SaCheckResult(sa.idn, sc, cal, cal_rbw, narrowest, floor, issues)
