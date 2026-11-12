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


class FakeAnalogOut:
    """Records the last value given to each analogOut setter."""

    def __init__(self):
        self.calls = {}

    def __getattr__(self, name):
        def setter(*args):
            self.calls[name] = args

        return setter


def test_wavegen_triangle_uses_the_triangle_function():
    ad = AnalogDiscovery2(FakeDevice(), "TEST")
    ad.dev.analogOut = ao = FakeAnalogOut()
    ad.set_wave(1, shape="triangle", frequency=1.0, low=0.0, high=1.8)
    p = ad._p
    assert ao.calls["nodeFunctionSet"] == (0, p.DwfAnalogOutNode.Carrier, p.DwfAnalogOutFunction.Triangle)
    assert ao.calls["nodeFrequencySet"][2] == 1.0
    assert ao.calls["nodeAmplitudeSet"][2] == 0.9 and ao.calls["nodeOffsetSet"][2] == 0.9
    ad.set_sine(1, frequency=1e3, low=0.0, high=1.8)  # set_sine is still a sine
    assert ao.calls["nodeFunctionSet"][2] == p.DwfAnalogOutFunction.Sine
