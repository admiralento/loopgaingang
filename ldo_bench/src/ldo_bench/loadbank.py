"""Binary-weighted switched-resistor load bank driven by AD2 DIO pins.

Bit k switches resistor R_k from VOUT to GND, so the load current for a code is
``Vout * sum(1 / (R_k + R_switch))`` over the set bits. Both boards use the same
bank: 250, 125, 62.5 (125 || 125), 31.6, 15.4 ohm on B0 (LSB) .. B4 (MSB).
"""

from __future__ import annotations

import re

from ldo_bench.config import LoadBankConfig
from ldo_bench.instruments.base import DigitalOut


class LoadBank:
    def __init__(self, cfg: LoadBankConfig, dio: DigitalOut) -> None:
        self.cfg = cfg
        self.dio = dio
        self.code: int | None = None

    @property
    def max_code(self) -> int:
        return self.cfg.max_code

    def _check(self, code: int) -> None:
        if not 0 <= code <= self.max_code:
            raise ValueError(f"load code {code} outside 0..{self.max_code}")

    def conductance(self, code: int) -> float:
        """Total conductance (S) switched in for `code`."""
        self._check(code)
        return sum(
            1.0 / (r + self.cfg.switch_resistance)
            for bit, r in enumerate(self.cfg.resistors)
            if code >> bit & 1
        )

    def current(self, code: int, vout: float) -> float:
        """Estimated load current (A) for `code` at output voltage `vout`."""
        return vout * self.conductance(code)

    def pin_levels(self, code: int) -> dict[int, bool]:
        self._check(code)
        return {
            pin: bool(code >> bit & 1) == self.cfg.active_high for bit, pin in enumerate(self.cfg.dio_pins)
        }

    def set(self, code: int) -> None:
        self.dio.write_pins(self.pin_levels(code))
        self.code = code

    def off(self) -> None:
        self.set(0)

    def code_for_current(self, amps: float, vout: float) -> int:
        """The code whose estimated current is closest to `amps`."""
        return min(range(self.max_code + 1), key=lambda c: abs(self.current(c, vout) - amps))

    def trigger_pin(self, code_low: int, code_high: int) -> tuple[int, bool]:
        """Pick the highest bit that changes between the two codes.

        Returns (dio_pin, rises_with_load): whether that pin's voltage goes up when
        the load steps from `code_low` to `code_high`.
        """
        changed = code_low ^ code_high
        if not changed:
            raise ValueError("codes are equal; nothing to trigger on")
        bit = changed.bit_length() - 1
        return self.cfg.dio_pins[bit], self.pin_edge(bit, code_high)

    def pin_edge(self, bit: int, code_high: int) -> bool:
        set_in_high = bool(code_high >> bit & 1)
        return set_in_high == self.cfg.active_high

    def bit_for_pin(self, pin: int) -> int:
        try:
            return self.cfg.dio_pins.index(pin)
        except ValueError:
            raise ValueError(f"DIO {pin} is not a load bank pin {self.cfg.dio_pins}") from None


_CURRENT_RE = re.compile(r"^\s*([0-9.]+)\s*(ma|a|ua)\s*$", re.IGNORECASE)
_SCALE = {"a": 1.0, "ma": 1e-3, "ua": 1e-6}


def parse_load(spec: str, bank: LoadBank, vout: float) -> int:
    """Parse a CLI load spec: a code ("17") or a current ("67mA", "0.1A")."""
    spec = spec.strip()
    if spec.isdigit():
        code = int(spec)
        bank._check(code)
        return code
    m = _CURRENT_RE.match(spec)
    if not m:
        raise ValueError(f"load '{spec}' is neither a code nor a current like '67mA'")
    return bank.code_for_current(float(m.group(1)) * _SCALE[m.group(2).lower()], vout)
