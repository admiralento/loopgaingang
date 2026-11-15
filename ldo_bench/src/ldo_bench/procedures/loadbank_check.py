"""loadbank-check: verify the AD2 DIO -> load bank wiring with the bank straight across the PSU.

The load bank goes straight across the PSU (no LDO). With every bit off the current is the baseline
(it includes the meter's offset); then each bit is switched on alone and the extra current is
compared with V / R for that bit. A bit that draws another bit's current points to swapped wires; a
bit that draws nothing points to an open gate wire or a missing AD2 ground; every bit off by the same
factor points to the voltage at the bank or the current reading, not the wiring.

Where the current comes from (loadbank_check.current_from):
- 'dmm' (default): the 34401A on DC amps in series between the PSU + and the bank's VIN, on dmm_range
  (1 A: ~0.1 ohm burden, so the bank keeps its voltage). The supply's own readback is recorded too,
  and a note says if it disagrees with the DMM.
- 'psu': the supply's current readback only.

Wiring: PSU (through the DMM in 'dmm') across the load bank's VIN / GND (J5 / J6), AD2 DIO on the gate
headers, AD2 GND on the bank's GND. No DUT, so dut.i_max does not apply (the bank's limit is lifted
for this test only); loadbank_check.voltage (<= 1.5 V) keeps the resistors within their power rating.

run() steps:
1. Confirm the wiring; DMM on DC amps ('dmm'); every bit off, PSU to `voltage`, wait settle_time.
2. Baseline: PSU voltage readback, and the current (mean of `readings`, DMM and PSU) with the bank off.
3. For each bit: switch it on alone, wait, read the current, subtract the baseline.
   Expected = V / (R + switch_resistance). A bit passes if it is within
   tolerance x expected + abs_tolerance (dmm_abs_tolerance with the DMM), in A.
4. Every bit off. `diagnosis()` turns failures into likely causes; `notes()` compares the readings.

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
from ldo_bench.instruments.base import InstrumentError

log = logging.getLogger(__name__)

NAME = "loadbank-check"

UNIFORM_SPREAD = 0.05  # every bit's ratio within 5 % of their median: one cause for all of them
READBACK_AGREEMENT = 0.05  # supply readback vs DMM: a note beyond 5 % of the DMM's current


@dataclass
class LoadBankCheckResult:
    """Per-bit currents. Each row: bit, dio_pin, resistor_ohm, expected_a, measured_a (the judged
    reading, baseline subtracted), measured_dmm_a and measured_psu_a (each baseline subtracted; NaN for a
    meter not used), ratio (measured / expected), closest_bit (whose expected current is nearest the
    measurement) and ok.
    """

    voltage: float  # V, PSU readback with the bank off
    current_from: str  # "dmm" or "psu": which reading the bits are judged on
    baseline_a: float  # A, the judged meter's current with every bit off (subtracted from each bit)
    baseline_psu_a: float  # A, the supply's readback with every bit off
    rows: list[dict]

    @property
    def passed(self) -> bool:
        return all(r["ok"] for r in self.rows)

    @property
    def source(self) -> str:
        return "the DMM" if self.current_from == "dmm" else "the supply's current readback"

    def _uniform_ratio(self) -> float | None:
        """The common ratio if every bit is off by the same factor (and by more than its tolerance), else None."""
        ratios = np.array([r["ratio"] for r in self.rows])
        med = float(np.median(ratios))
        if self.passed or med < 0.25 or len(ratios) < 2:  # under 25 %: bits drawing nothing, said per bit
            return None
        return med if np.all(np.abs(ratios - med) <= UNIFORM_SPREAD * med) else None

    def diagnosis(self) -> list[str]:
        """One line per failed bit with its likely cause, or one line if every bit is off by the same factor."""
        if (k := self._uniform_ratio()) is not None:
            return [
                f"every bit draws {k * 100:.0f} % of V/R (from {self.source}): not a wiring fault, which would "
                f"differ bit to bit. Either the bank sees less than the supply's {self.voltage:.3f} V (about "
                f"{k * self.voltage:.2f} V: something in series dropping {self.voltage * (1 - k):.2f} V), or the "
                "current reading is off. Measure across one resistor with a meter while its bit is on"
            ]
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

    def notes(self) -> list[str]:
        """Findings that don't fail a bit: the supply's current readback against the DMM."""
        if self.current_from != "dmm":
            return []
        pairs = [(r["measured_psu_a"], r["measured_dmm_a"]) for r in self.rows if r["measured_dmm_a"] > 0]
        if not pairs:
            return []
        psu, dmm = (np.array(x) for x in zip(*pairs, strict=True))
        gain = float(np.sum(psu * dmm) / np.sum(dmm * dmm))  # least-squares psu = gain x dmm
        if abs(gain - 1) <= READBACK_AGREEMENT:
            return []
        return [
            f"the supply's current readback is {100 * (gain - 1):+.0f} % from the DMM (the bits are judged on "
            "the DMM)"
        ]

    def summary(self) -> dict:
        return {
            "voltage": self.voltage,
            "current_from": self.current_from,
            "baseline_a": self.baseline_a,
            "baseline_psu_a": self.baseline_psu_a,
            "passed": self.passed,
            "diagnosis": self.diagnosis(),
            "notes": self.notes(),
        }

    def report(self) -> str:
        dmm = self.current_from == "dmm"
        lines = [
            f"Load bank check at {self.voltage:.3f} V, current from {self.source} "
            f"(baseline {self.baseline_a * 1e3:.3f} mA, subtracted):",
            f"  {'bit':>3} {'DIO':>3} {'R ohm':>6} {'expected':>10} {'measured':>10} {'error':>7}"
            + (f" {'PSU readback':>13}" if dmm else ""),
        ]
        for r in self.rows:
            lines.append(
                f"  {r['bit']:>3} {r['dio_pin']:>3} {r['resistor_ohm']:6.1f} "
                f"{r['expected_a'] * 1e3:7.2f} mA {r['measured_a'] * 1e3:7.3f} mA "
                f"{100 * (r['ratio'] - 1):+6.1f}%"
                + (f" {r['measured_psu_a'] * 1e3:10.2f} mA" if dmm else "")
                + f"  {'ok' if r['ok'] else 'FAIL'}"
            )
        lines.append("  PASS: every DIO switches its own bit" if self.passed else "  FAIL:")
        lines += [f"    - {n}" for n in self.diagnosis()]
        lines += [f"  Note: {n}" for n in self.notes()]
        return "\n".join(lines)


def wiring(bench: Bench, lc: LoadBankCheckConfig) -> list[str]:
    """The bank across the PSU, through the DMM's current input with current_from 'dmm'."""
    cfg, lb = bench.cfg, bench.cfg.loadbank
    ch = cfg.psu.channel
    if lc.current_from == "dmm":
        supply = [
            f"PSU OUT{ch} + -> DMM I (3A/mA jack)",
            "DMM LO (COM) -> load bank VIN (J5) (the DMM in series with the bank)",
            f"PSU OUT{ch} - -> load bank GND (J6)",
        ]
    else:
        supply = w.psu(cfg, "load bank VIN (J5)", "load bank GND (J6)")
    return [
        *supply,
        *(f"AD2 DIO {p} -> load bank J{k}" for k, p in enumerate(lb.dio_pins)),
        "AD2 GND -> load bank GND (J6) (the FET gates are driven relative to it)",
    ]


def run(bench: Bench, lc: LoadBankCheckConfig) -> LoadBankCheckResult:
    """Confirm the wiring, then measure every bit with the bank's current limit lifted.

    lc: the [loadbank_check] section. The PSU goes through bench.psu_output (only psu.v_max).
    InstrumentError if current_from is 'dmm' and there is no DMM that reads DC amps.
    """
    use_dmm = lc.current_from == "dmm"
    if use_dmm and (bench.dmm is None or not hasattr(bench.dmm, "configure_dci")):
        raise InstrumentError(
            "loadbank-check --current-from dmm needs the DMM on DC amps: enable [dmm] (or use --current-from psu)"
        )
    bench.confirm_wiring(
        "load bank check (no LDO)",
        wiring(bench, lc),
        ref_probe=("vin", 0),
        bank_on_psu=True,
        dmm_probe="series" if use_dmm else "vout",
        chip_powered=False,  # the bank straight on the supply, no LDO
    )
    if use_dmm:
        nplc = bench.cfg.dmm.nplc if bench.cfg.dmm else 10
        bench.dmm.configure_dci(range=lc.dmm_range, nplc=nplc)
    bench.set_psu_limit(high=lc.high_current)  # psu.high_current_limit if the section asks
    with bench.loadbank.unrestricted():  # no DUT here: dut.i_max doesn't apply
        return _run(bench, lc)


def _currents(bench: Bench, n: int, use_dmm: bool) -> tuple[float, float]:
    """(DMM current, PSU readback) in A, each the mean of n readings; the DMM is NaN when not used."""
    psu = float(np.mean([bench.psu.measure_current() for _ in range(n)]))
    if not use_dmm:
        return float("nan"), psu
    x = np.array([bench.dmm.read() for _ in range(n)], float)
    if not np.all(np.isfinite(x)):
        raise InstrumentError(
            "the DMM overloaded on DC amps: is it on the 3A/mA jack, and loadbank_check.dmm_range big enough?"
        )
    return float(x.mean()), psu


def _run(bench: Bench, lc: LoadBankCheckConfig) -> LoadBankCheckResult:
    """The measurement itself (steps 1-4 in the module docstring)."""
    bank, lb = bench.loadbank, bench.cfg.loadbank
    use_dmm = lc.current_from == "dmm"
    abs_tol = lc.dmm_abs_tolerance if use_dmm else lc.abs_tolerance
    bank.off()
    bench.psu_output(lc.voltage)
    bench.sleep(lc.settle_time)
    v = bench.psu.measure_voltage()
    base_dmm, base_psu = _currents(bench, lc.readings, use_dmm)
    baseline = base_dmm if use_dmm else base_psu
    log.info("bank off: %.4f V, %.3f mA (baseline, %s)", v, baseline * 1e3, lc.current_from)

    # every bit's expected current, so a wrong reading can be matched to the bit it looks like
    expected_all = [v / (r + lb.switch_resistance) for r in lb.resistors]
    rows = []
    for bit, (pin, r) in enumerate(zip(lb.dio_pins, lb.resistors, strict=True)):
        bank.set(1 << bit)
        bench.sleep(lc.settle_time)
        i_dmm, i_psu = _currents(bench, lc.readings, use_dmm)
        m_dmm, m_psu = i_dmm - base_dmm, i_psu - base_psu
        measured = m_dmm if use_dmm else m_psu
        expected = expected_all[bit]
        closest = int(np.argmin([abs(measured - e) for e in expected_all]))
        rows.append(
            {
                "bit": bit,
                "dio_pin": pin,
                "resistor_ohm": r,
                "expected_a": expected,
                "measured_a": measured,
                "measured_dmm_a": m_dmm,
                "measured_psu_a": m_psu,
                "ratio": measured / expected,
                "closest_bit": closest,
                "ok": abs(measured - expected) <= lc.tolerance * expected + abs_tol,
            }
        )
        log.info(
            "bit %d (DIO %d): %.3f mA, expected %.3f mA (PSU readback %.2f mA)",
            bit,
            pin,
            measured * 1e3,
            expected * 1e3,
            m_psu * 1e3,
        )
    bank.off()
    return LoadBankCheckResult(v, lc.current_from, baseline, base_psu, rows)


def plot(res: LoadBankCheckResult, dut: str):
    """Bar chart: expected and measured current per bit (plus the supply's readback with the DMM); failed bits get a cross."""
    del dut
    bits = [f"B{r['bit']}\nDIO {r['dio_pin']}\n{r['resistor_ohm']:g} Ω" for r in res.rows]
    exp = np.array([r["expected_a"] for r in res.rows]) * 1e3
    meas = np.array([r["measured_a"] for r in res.rows]) * 1e3
    dmm = res.current_from == "dmm"
    x = np.arange(len(bits))
    n_bars = 3 if dmm else 2
    w = 0.8 / n_bars
    fig, ax = plotting.figure(size=(7.0, 4.2))
    ax.bar(x - w, exp, w, color=plotting.AXIS, label="Expected (V/R)", zorder=2)
    ax.bar(x, meas, w, color=plotting.SERIES[0], label="DMM" if dmm else "PSU readback", zorder=2)
    if dmm:
        psu = np.array([r["measured_psu_a"] for r in res.rows]) * 1e3
        ax.bar(x + w, psu, w, color=plotting.SERIES[1], label="PSU readback", zorder=2)
    for xi, m, r in zip(x, meas, res.rows, strict=True):
        ax.text(
            xi,
            m,
            f"{m:.2f}" + ("" if r["ok"] else " ✗"),
            ha="center",
            va="bottom",
            fontsize=8,
            color=plotting.INK_2,
        )
    ax.set_xticks(x, bits)
    ax.set_ylabel("Current (mA)")
    ax.grid(axis="x", visible=False)
    ax.set_title(f"Load bank check: one bit at a time, current from {res.source}", pad=20)
    verdict = "PASS" if res.passed else "FAIL"
    plotting.subtitle(ax, f"{res.voltage:.3f} V across the bank  ·  baseline subtracted  ·  {verdict}")
    ax.legend(loc="upper left")
    return fig
