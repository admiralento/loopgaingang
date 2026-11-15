import pytest

from ldo_bench.config import LoadBankConfig
from ldo_bench.loadbank import LoadBank, parse_load


class FakeDio:
    def __init__(self):
        self.levels: dict[int, bool] = {}

    def write_pins(self, levels):
        self.levels.update(levels)

    def release_all(self):
        self.levels.clear()


def bank(active_high=True) -> LoadBank:
    cfg = LoadBankConfig(
        dio_pins=[0, 1, 2, 3, 4], resistors=[250.0, 125.0, 62.5, 31.6, 15.4], active_high=active_high
    )
    return LoadBank(cfg, FakeDio())


def test_binary_weighted_currents_at_1v():
    b = bank()
    assert b.current(1, 1.0) == pytest.approx(4e-3)
    assert b.current(16, 1.0) == pytest.approx(1 / 15.4)
    # README: 4 mA <-> 67 mA step on the demo board is code 1 <-> 17
    assert b.current(17, 1.0) == pytest.approx(0.0689, abs=1e-3)
    assert b.current(31, 1.0) == pytest.approx(0.1246, abs=1e-3)


def test_set_drives_pins():
    b = bank()
    b.set(0b10011)
    assert b.dio.levels == {0: True, 1: True, 2: False, 3: False, 4: True}


def test_active_low_inverts():
    b = bank(active_high=False)
    b.set(1)
    assert b.dio.levels[0] is False and b.dio.levels[4] is True


def test_parse_load():
    b = bank()
    assert parse_load("17", b, 1.0) == 17
    assert parse_load("4mA", b, 1.0) == 1
    assert parse_load("67 mA", b, 1.0) == 17
    with pytest.raises(ValueError):
        parse_load("32", b, 1.0)
    with pytest.raises(ValueError):
        parse_load("lots", b, 1.0)


def test_trigger_pin_is_msb_that_changes():
    b = bank()
    assert b.trigger_pin(1, 17) == (4, True)
    assert b.trigger_pin(1, 3) == (1, True)
