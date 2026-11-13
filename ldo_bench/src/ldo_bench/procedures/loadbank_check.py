"""loadbank-check: verify the AD2 DIO -> load bank wiring using only the PSU.

The load bank goes straight across the PSU (no LDO). With every bit off the
PSU current is the baseline (it includes the readback offset); then each bit
is switched on alone and the extra current is compared with V / R for that
bit. A bit that draws another bit's current points to swapped wires; a bit
that draws nothing points to an open gate wire or a missing AD2 ground.

Wiring: PSU across the load bank's VIN / GND (J5 / J6), AD2 DIO on the gate
headers, AD2 GND on the bank's GND. No DUT, so dut.i_max does not apply (the
bank's limit is lifted for this test only); loadbank_check.voltage (<= 1.5 V)
keeps the resistors within their power rating.

run() steps:
1. Confirm the wiring, every bit off, PSU to `voltage`, wait settle_time.
2. Baseline: PSU voltage and current readback (mean of `readings`) with the bank off.
3. For each bit: switch it on alone, wait, read the current, subtract the baseline.
   Expected = V / (R + switch_resistance). A bit passes if it is within
   tolerance x expected + abs_tolerance (A).
4. Every bit off. `diagnosis()` turns failures into likely causes.

This test has no `issues`: a failing bit is the result, not a setup problem.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np

from ldo_bench import plotting
from ldo_bench import wiring as w
from ldo_bench.bench import Bench
from ldo_bench.config import LoadBankCheckConfig

log = logging.getLogger(__name__)

NAME = "loadbank-check"


@dataclass
class LoadBankCheckResult:
    """Per-bit currents. Each row: bit, dio_pin, resistor_ohm, expected_a, measured_a (baseline
    subtracted), ratio (measured / expected), closest_bit (whose expected current is nearest the
    measurement) and ok.
    """

    voltage: float  # PSU readback with the bank off
    baseline_a: float  # A, PSU current readback with every bit off (subtracted from each bit)
    rows: list[dict]

    @property
    def passed(self) -> bool:
        return all(r["ok"] for r in self.rows)

    def diagnosis(self) -> list[str]:
        """One line per failed bit, with the likely cause."""
        notes = []
        for r in self.rows:
            if r["ok"]:
                continue
            # under a quarter of the expected current: the gate isn't being driven at all
            if r["measured_a"] < 0.25 * r["expected_a"]:
                notes.append(
                    f"DIO {r['dio_pin']} (bit {r['bit']}) draws no current: check that wire to the "
                    f"gate header, and that AD2 GND is tied to the load bank GND"
                )
            elif r["closest_bit"] != r["bit"]:
                notes.append(
                    f"DIO {r['dio_pin']} draws bit {r['closest_bit']}'s current "
                    f"({r['measured_a'] * 1e3:.1f} mA): looks wired to J{r['closest_bit']}, not J{r['bit']}"
                )
            else:
                notes.append(
                    f"DIO {r['dio_pin']} current is {100 * (r['ratio'] - 1):+.0f} % off: "
                    "check the resistor value or a bad switch"
                )
        return notes

    def summary(self) -> dict:
        return {
            "voltage": self.voltage,
            "baseline_a": self.baseline_a,
            "passed": self.passed,
            "diagnosis": self.diagnosis(),
        }

    def report(self) -> str:
        lines = [
            f"Load bank check at {self.voltage:.3f} V (baseline {self.baseline_a * 1e3:.2f} mA, subtracted):",
            f"  {'bit':>3} {'DIO':>3} {'R ohm':>6} {'expected':>10} {'measured':>10} {'error':>7}",
        ]
        for r in self.rows:
            lines.append(
                f"  {r['bit']:>3} {r['dio_pin']:>3} {r['resistor_ohm']:6.1f} "
                f"{r['expected_a'] * 1e3:7.2f} mA {r['measured_a'] * 1e3:7.2f} mA "
                f"{100 * (r['ratio'] - 1):+6.1f}%  {'ok' if r['ok'] else 'FAIL'}"
            )
        lines.append("  PASS: every DIO switches its own bit" if self.passed else "  FAIL:")
        lines += [f"    - {n}" for n in self.diagnosis()]
        return "\n".join(lines)


def _current(bench: Bench, n: int) -> float:
    """PSU current readback (A), mean of n readings."""
    return float(np.mean([bench.psu.measure_current() for _ in range(n)]))


def run(bench: Bench, lc: LoadBankCheckConfig) -> LoadBankCheckResult:
    """Confirm the wiring, then measure every bit with the bank's current limit lifted.

    lc: the [loadbank_check] section. The PSU goes through bench.psu_output (only psu.v_max).
    """
    bank, lb = bench.loadbank, bench.cfg.loadbank
    ch = bench.cfg.psu.channel
    bench.confirm_wiring(
        "load bank check (no LDO)",
        [
            *w.psu(bench.cfg, "load bank VIN (J5)", "load bank GND (J6)"),
            *(f"AD2 DIO {p} -> load bank J{k}" for k, p in enumerate(lb.dio_pins)),
            "AD2 GND -> load bank GND (J6) (the FET gates are driven relative to it)",
        ],
        ref_probe=("vin", 0),
        bank_on_psu=True,
    )
    with bank.unrestricted():  # no DUT here: dut.i_max doesn't apply
        return _run(bench, lc)


def _run(bench: Bench, lc: LoadBankCheckConfig) -> LoadBankCheckResult:
    """The measurement itself (steps 1-4 in the module docstring)."""
    bank, lb = bench.loadbank, bench.cfg.loadbank
    bank.off()
    bench.psu_output(lc.voltage)
    bench.sleep(lc.settle_time)
    v = bench.psu.measure_voltage()
    baseline = _current(bench, lc.readings)
    log.info("bank off: %.4f V, %.2f mA (baseline)", v, baseline * 1e3)

    # every bit's expected current, so a wrong reading can be matched to the bit it looks like
    expected_all = [v / (r + lb.switch_resistance) for r in lb.resistors]
    rows = []
    for bit, (pin, r) in enumerate(zip(lb.dio_pins, lb.resistors, strict=True)):
        bank.set(1 << bit)
        bench.sleep(lc.settle_time)
        measured = _current(bench, lc.readings) - baseline
        expected = expected_all[bit]
        closest = int(np.argmin([abs(measured - e) for e in expected_all]))
        rows.append(
            {
                "bit": bit,
                "dio_pin": pin,
                "resistor_ohm": r,
                "expected_a": expected,
                "measured_a": measured,
                "ratio": measured / expected,
                "closest_bit": closest,
                "ok": abs(measured - expected) <= lc.tolerance * expected + lc.abs_tolerance,
            }
        )
        log.info("bit %d (DIO %d): %.2f mA, expected %.2f mA", bit, pin, measured * 1e3, expected * 1e3)
    bank.off()
    return LoadBankCheckResult(v, baseline, rows)


def plot(res: LoadBankCheckResult, dut: str):
    """Bar chart: expected and measured current per bit; failed bits are marked with a cross."""
    del dut
    bits = [f"B{r['bit']}\nDIO {r['dio_pin']}\n{r['resistor_ohm']:g} Ω" for r in res.rows]
    exp = np.array([r["expected_a"] for r in res.rows]) * 1e3
    meas = np.array([r["measured_a"] for r in res.rows]) * 1e3
    x = np.arange(len(bits))
    w = 0.36
    fig, ax = plotting.figure(size=(7.0, 4.2))
    ax.bar(x - w / 2 - 0.01, exp, w, color=plotting.AXIS, label="Expected (V/R)", zorder=2)
    ax.bar(x + w / 2 + 0.01, meas, w, color=plotting.SERIES[0], label="Measured", zorder=2)
    for xi, m, r in zip(x, meas, res.rows, strict=True):
        ax.text(
            xi + w / 2,
            m,
            f"{m:.1f}" + ("" if r["ok"] else " ✗"),
            ha="center",
            va="bottom",
            fontsize=8,
            color=plotting.INK_2,
        )
    ax.set_xticks(x, bits)
    ax.set_ylabel("Current (mA)")
    ax.grid(axis="x", visible=False)
    ax.set_title("Load bank check: one bit at a time, PSU only", pad=20)
    verdict = "PASS" if res.passed else "FAIL"
    plotting.subtitle(ax, f"{res.voltage:.3f} V across the bank  ·  baseline subtracted  ·  {verdict}")
    ax.legend(loc="upper left")
    return fig
