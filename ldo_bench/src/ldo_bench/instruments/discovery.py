"""Find and open instruments by model, not by address.

Hardware drivers land in the next steps; until then only `--sim` works.
"""

from __future__ import annotations

from ldo_bench.instruments.base import InstrumentError, Oscilloscope, PowerSupply


def open_psu(resource: str = "", *, gpib_address: int = 5) -> PowerSupply:
    raise InstrumentError("E3638A driver not implemented yet; use --sim")


def open_ad2(serial: str = "") -> Oscilloscope:
    raise InstrumentError("Analog Discovery 2 driver not implemented yet; use --sim")


def discover() -> list[str]:
    raise InstrumentError("hardware discovery not implemented yet; use --sim")
