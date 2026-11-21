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


class FakeScopeIn:
    """analogIn that walks through a given state sequence and returns a rising edge at 10 %."""

    def __init__(self, states, n=1000):
        self.states, self.n, self.rate = list(states), n, 1e6

    def __getattr__(self, name):  # setters the driver calls that don't matter here
        return lambda *a, **k: None

    def frequencySet(self, f):
        self.rate = f

    def frequencyGet(self):
        return self.rate

    def bufferSizeInfo(self):
        return (16, 8192)

    def bufferSizeSet(self, n):
        self.n = n

    def bufferSizeGet(self):
        return self.n

    def status(self, read):
        return self.states.pop(0) if len(self.states) > 1 else self.states[0]

    def statusData(self, ch, n):
        x = np.zeros(n)
        x[n // 10 :] = 1.8
        return x


def fast_capture(states, **kw):
    from ldo_bench.instruments.base import ChannelSetup

    ad = AnalogDiscovery2(FakeDevice(), "TEST")
    s = ad._p.DwfState
    ad.dev.analogIn = FakeScopeIn([getattr(s, name) for name in states])
    calls = []
    args = {
        "sample_rate": 2e6,
        "n_samples": 1000,
        "setups": {1: ChannelSetup(5.0, 0.0), 2: ChannelSetup(5.0, 0.0)},
        "trigger_channel": 2,
        "trigger_level": 0.9,
        "edge": "rising",
        "pre_trigger": 0.1,
        "stimulus": lambda: calls.append(1),
        "timeout": 0.2,
    }
    return ad.capture(**args, **kw), calls


def test_free_running_capture_that_triggers_before_the_arm_poll_is_kept():
    """A 1 kHz sine triggers within a poll interval: the driver never sees Armed, only Triggered / Done."""
    cap, calls = fast_capture(["Prefill", "Triggered", "Done"], free_running=True)
    assert cap.edge_found and cap.trigger_index == 100 and calls == [1]


def test_stepped_capture_that_triggers_before_the_stimulus_is_an_error():
    """A load step must come after arming: a trigger before it didn't capture the step."""
    import pytest

    from ldo_bench.instruments.base import InstrumentError

    with pytest.raises(InstrumentError, match="before the stimulus"):
        fast_capture(["Prefill", "Done"])


def test_capture_passes_the_trigger_hysteresis():
    """None keeps the 1 % of range default; a value is sent to the AD2 as given (V)."""

    class Recording(FakeScopeIn):
        def triggerHysteresisSet(self, v):
            self.hysteresis = v

    from ldo_bench.instruments.base import ChannelSetup

    for given, expected in ((None, 0.5), (0.025, 0.025)):
        ad = AnalogDiscovery2(FakeDevice(), "TEST")
        s = ad._p.DwfState
        ad.dev.analogIn = scope = Recording([s.Prefill, s.Armed, s.Done])
        ad.capture(
            sample_rate=2e6,
            n_samples=1000,
            setups={1: ChannelSetup(5.0, 0.0), 2: ChannelSetup(50.0, 0.0)},
            trigger_channel=2,
            trigger_level=0.9,
            edge="rising",
            pre_trigger=0.1,
            stimulus=lambda: None,
            timeout=0.2,
            hysteresis=given,
        )
        assert scope.hysteresis == expected


def test_set_dc_after_a_square_is_dc_again():
    """line-tran's square ends with vin_from_wavegen, and shutdown sets W1 to DC 0 V: neither may
    leave the square running with only its offset changed."""
    ad = AnalogDiscovery2(FakeDevice(), "TEST")
    ad.dev.analogOut = ao = FakeAnalogOut()
    p = ad._p
    ad.set_dc(1, 1.75)
    ad.set_wave(1, shape="square", frequency=20.0, low=1.75, high=1.85)
    assert ao.calls["nodeFunctionSet"][2] == p.DwfAnalogOutFunction.Square
    ad.set_dc(1, 0.0)
    assert ao.calls["nodeFunctionSet"][2] == p.DwfAnalogOutFunction.DC
    assert ao.calls["nodeOffsetSet"][2] == 0.0
