"""AD2 driver logic that can be checked without hardware (fake pydwf device)."""

import numpy as np

from ldo_bench.instruments.ad2 import AnalogDiscovery2, _find_crossing


class FakeDigitalIO:
    def __init__(self):
        self.enable = self.value = 0
        self.history = []

    def reset(self):
        self.enable = self.value = 0

    def outputSet(self, v):
        self.value = v

    def outputEnableSet(self, m):
        self.enable = m

    def configure(self):
        self.history.append((self.enable, self.value))


class FakeAnalogIn:
    def reset(self):
        pass


class FakeDevice:
    def __init__(self):
        self.digitalIO = FakeDigitalIO()
        self.analogIn = FakeAnalogIn()


def test_dio_bits_accumulate_and_release():
    ad = AnalogDiscovery2(FakeDevice(), "TEST")
    dio = ad.dev.digitalIO
    ad.write_pins({0: True, 4: True})
    ad.write_pins({0: False, 1: True})
    assert dio.history[-1] == (0b10011, 0b10010)
    ad.release_all()
    assert dio.history[-1] == (0, 0)


def test_find_crossing_picks_edge_nearest_expected():
    x = np.zeros(100)
    x[60:] = 3.3
    x[10] = 3.3  # noise spike creates an extra early crossing
    assert _find_crossing(x, 1.65, "rising", near=55) == 60
    assert _find_crossing(x[::-1].copy(), 1.65, "falling", near=40) == 40
    assert _find_crossing(np.zeros(10), 1.65, "rising", near=5) is None
