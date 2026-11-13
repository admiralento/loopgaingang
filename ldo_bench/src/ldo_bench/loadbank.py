"""Binary-weighted switched-resistor load bank driven by AD2 DIO pins.

Bit k switches resistor R_k from VOUT to GND, so the load current for a code is
``Vout * sum(1 / (R_k + R_switch))`` over the set bits. Both boards use the same
bank: 250, 125, 62.5 (125 || 125), 31.6, 15.4 ohm on B0 (LSB) .. B4 (MSB).
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager

from ldo_bench.config import LoadBankConfig
from ldo_bench.instruments.base import DigitalOut, InstrumentError


class LoadBank:
    """The load bank: converts codes to DIO pin levels and estimates their current.

    `i_max` / `vout`: refuse any code whose current at `vout` exceeds `i_max` (the DUT's limit).
    `dio` may be None when the bank is only used for code / current arithmetic (the CLI does this
    to parse --load). `code` is the last code set (None before the first).
    """

    def __init__(
        self,
        cfg: LoadBankConfig,
        dio: DigitalOut,
        *,
        i_max: float | None = None,
        vout: float | None = None,
    ) -> None:
        self.cfg = cfg
        self.dio = dio
        self.code: int | None = None
        self.i_max = i_max
        self.vout = vout
        self._limit_on = True

    @property
    def max_code(self) -> int:
        """Highest code (all bits on), ignoring i_max."""
        return self.cfg.max_code

    def _check(self, code: int) -> None:
        """ValueError if `code` isn't in 0..max_code."""
        if not 0 <= code <= self.max_code:
            raise ValueError(f"load code {code} outside 0..{self.max_code}")

    def conductance(self, code: int) -> float:
        """Total conductance (S) switched in for `code`."""
        self._check(code)
        return self.cfg.conductance(code)

    def current(self, code: int, vout: float) -> float:
        """Estimated load current (A) for `code` at output voltage `vout`."""
        return vout * self.conductance(code)

    def pin_levels(self, code: int) -> dict[int, bool]:
        """{DIO pin: logic level} for `code`, inverted if the bank is active low."""
        self._check(code)
        return {
            pin: bool(code >> bit & 1) == self.cfg.active_high for bit, pin in enumerate(self.cfg.dio_pins)
        }

    def allowed(self, code: int) -> bool:
        """True if `code` draws at most i_max at `vout` (always True with no limit or inside unrestricted())."""
        if self.i_max is None or self.vout is None or not self._limit_on:
            return True
        return self.current(code, self.vout) <= self.i_max

    def allowed_codes(self) -> list[int]:
        """Every allowed code, ascending."""
        return [c for c in range(self.max_code + 1) if self.allowed(c)]

    @property
    def max_allowed_code(self) -> int:
        """The allowed code with the most current (not necessarily the highest number)."""
        return max(self.allowed_codes(), key=self.conductance)

    def set(self, code: int) -> None:
        """Switch the bank to `code`. InstrumentError if it's above i_max, ValueError if out of range."""
        if not self.allowed(code):
            raise InstrumentError(
                f"load code {code} would draw ~{self.current(code, self.vout) * 1e3:.1f} mA, "
                f"above the DUT limit dut.i_max = {self.i_max * 1e3:.1f} mA"
            )
        self.dio.write_pins(self.pin_levels(code))
        self.code = code

    @contextmanager
    def unrestricted(self) -> Iterator[None]:
        """Lift the DUT current limit, for tests with no DUT connected (loadbank-check)."""
        self._limit_on = False
        try:
            yield
        finally:
            self._limit_on = True

    def off(self) -> None:
        """All resistors out (code 0)."""
        self.set(0)

    def code_for_current(self, amps: float, vout: float) -> int:
        """The code whose estimated current at `vout` (V) is closest to `amps` (A).

        Searches every code, not just the allowed ones; the config check then refuses one above dut.i_max.
        """
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
        """True if `bit`'s pin is at logic high in `code_high` (so it rises on the step to code_high)."""
        set_in_high = bool(code_high >> bit & 1)
        return set_in_high == self.cfg.active_high

    def bit_for_pin(self, pin: int) -> int:
        """Bank bit number (0 = LSB) driven by DIO `pin`; ValueError if it isn't a bank pin."""
        try:
            return self.cfg.dio_pins.index(pin)
        except ValueError:
            raise ValueError(f"DIO {pin} is not a load bank pin {self.cfg.dio_pins}") from None


_CURRENT_RE = re.compile(r"^\s*([0-9.]+)\s*(ma|a|ua)\s*$", re.IGNORECASE)
_SCALE = {"a": 1.0, "ma": 1e-3, "ua": 1e-6}


def parse_load(spec: str, bank: LoadBank, vout: float) -> int:
    """Parse a CLI load spec: a code ("17") or a current ("67mA", "0.1A", "500uA").

    A current is rounded to the nearest code at `vout` (V). ValueError if the spec isn't
    understood or the code is out of range.
    """
    spec = spec.strip()
    if spec.isdigit():
        code = int(spec)
        bank._check(code)
        return code
    m = _CURRENT_RE.match(spec)
    if not m:
        raise ValueError(f"load '{spec}' is neither a code nor a current like '67mA'")
    return bank.code_for_current(float(m.group(1)) * _SCALE[m.group(2).lower()], vout)
