import pytest

from ldo_bench.instruments.base import InstrumentError
from ldo_bench.instruments.e36xx import AgilentE36xx
from ldo_bench.instruments.transports import PrologixTransport, parse_prologix

IDN = "Agilent Technologies,E3638A,0,1.2-5.0-1.0"
IDN_DUAL = "Agilent Technologies,E3648A,0,1.7-5.0-1.0"  # the unit on our bench


class FakeTransport:
    name = "fake"

    def __init__(self, idn=IDN, errors=None):
        self.idn = idn
        self.sent: list[str] = []
        self.errors = list(errors or [])
        self.volt = 0.0
        self.on = False
        self.ignore_off = False  # a supply that drops OUTP OFF

    def write(self, cmd):
        self.sent.append(cmd)
        if cmd.startswith("VOLT ") and not cmd.startswith("VOLT:"):
            self.volt = float(cmd.split()[1])
        if cmd == "OUTP ON":
            self.on = True
        if cmd == "OUTP OFF" and not self.ignore_off:
            self.on = False

    def query(self, cmd):
        self.sent.append(cmd)
        return {
            "*IDN?": self.idn,
            "SYST:ERR?": self.errors.pop(0) if self.errors else '+0,"No error"',
            "VOLT?": f"{self.volt:+.5E}",
            "MEAS:VOLT?": "+1.80010E+00",
            "MEAS:CURR?": "+1.23000E-02",
            "OUTP?": "1" if self.on else "0",
        }[cmd]

    def close(self):
        self.sent.append("<close>")


def test_rejects_other_model():
    with pytest.raises(InstrumentError, match="not a supported supply"):
        AgilentE36xx(FakeTransport(idn="Agilent Technologies,E3631A,0,2.1"))


def test_single_output_has_no_channel_2():
    with pytest.raises(InstrumentError, match="no output 2"):
        AgilentE36xx(FakeTransport(), channel=2)


def test_dual_output_selects_channel_and_zeroes_the_other():
    t = FakeTransport(idn=IDN_DUAL)
    p = AgilentE36xx(t, channel=2)
    p.setup(current_limit=0.3, ovp=None, voltage_range=None)
    i = t.sent.index("INST:SEL OUT1")
    assert t.sent[i + 1] == "VOLT 0"  # unused output held at 0 V
    assert t.sent.index("INST:SEL OUT2") < t.sent.index("CURR 0.3000")
    t.sent.clear()
    p.set_voltage(1.8)
    assert t.sent == ["INST:SEL OUT2", "VOLT 1.8000"]


def test_setup_and_readback():
    t = FakeTransport()
    p = AgilentE36xx(t)
    p.setup(current_limit=0.3, ovp=6.0, voltage_range=None)
    assert "OUTP OFF" in t.sent and "CURR 0.3000" in t.sent and "VOLT:PROT 6.000" in t.sent
    p.set_voltage(1.8)
    assert p.get_voltage_setpoint() == pytest.approx(1.8)
    assert p.measure_voltage() == pytest.approx(1.8001)
    assert p.measure_current() == pytest.approx(0.0123)


def test_scpi_error_raises():
    t = FakeTransport()
    p = AgilentE36xx(t)
    t.errors = ['-222,"Data out of range"']
    with pytest.raises(InstrumentError, match="Data out of range"):
        p.setup(current_limit=99, ovp=None, voltage_range=None)


def test_parse_prologix():
    assert parse_prologix("prologix:COM5:5") == ("COM5", 5)
    assert parse_prologix("prologix:/dev/tty.usbserial-A1:12") == ("/dev/tty.usbserial-A1", 12)
    with pytest.raises(InstrumentError):
        parse_prologix("prologix:COM5")


class FakeSerial:
    def __init__(self, replies):
        self.out = b""
        self.replies = list(replies)

    def write(self, b):
        self.out += b

    def readline(self):
        return (self.replies.pop(0) + "\n").encode() if self.replies else b""

    def reset_input_buffer(self):
        pass

    def close(self):
        pass


def test_prologix_protocol():
    ser = FakeSerial([IDN])
    t = PrologixTransport("COM5", 5, serial_obj=ser)
    assert t.query("*IDN?") == IDN
    lines = ser.out.decode().splitlines()
    assert lines[:2] == ["++mode 1", "++addr 5"]
    assert lines[-2:] == ["*IDN?", "++read eoi"]


def test_prologix_silence_is_an_error():
    t = PrologixTransport("COM5", 5, serial_obj=FakeSerial([]))
    with pytest.raises(InstrumentError, match="no reply"):
        t.query("*IDN?")
