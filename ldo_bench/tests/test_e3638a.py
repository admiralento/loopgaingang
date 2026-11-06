import pytest

from ldo_bench.instruments import discovery
from ldo_bench.instruments.base import InstrumentError
from ldo_bench.instruments.discovery import Found
from ldo_bench.instruments.e3638a import E3638A
from ldo_bench.instruments.transports import PrologixTransport, parse_prologix

IDN = "Agilent Technologies,E3638A,0,1.2-5.0-1.0"


class FakeTransport:
    name = "fake"

    def __init__(self, idn=IDN, errors=None):
        self.idn = idn
        self.sent: list[str] = []
        self.errors = list(errors or [])
        self.volt = 0.0

    def write(self, cmd):
        self.sent.append(cmd)
        if cmd.startswith("VOLT ") and not cmd.startswith("VOLT:"):
            self.volt = float(cmd.split()[1])

    def query(self, cmd):
        self.sent.append(cmd)
        return {
            "*IDN?": self.idn,
            "SYST:ERR?": self.errors.pop(0) if self.errors else '+0,"No error"',
            "VOLT?": f"{self.volt:+.5E}",
            "MEAS:VOLT?": "+1.80010E+00",
            "MEAS:CURR?": "+1.23000E-02",
        }[cmd]

    def close(self):
        self.sent.append("<close>")


def test_rejects_other_model():
    with pytest.raises(InstrumentError, match="not an E3638A"):
        E3638A(FakeTransport(idn="Agilent Technologies,E3631A,0,2.1"))


def test_setup_and_readback():
    t = FakeTransport()
    p = E3638A(t)
    p.setup(current_limit=0.3, ovp=6.0, voltage_range=None)
    assert "OUTP OFF" in t.sent and "CURR 0.3000" in t.sent and "VOLT:PROT 6.000" in t.sent
    p.set_voltage(1.8)
    assert p.get_voltage_setpoint() == pytest.approx(1.8)
    assert p.measure_voltage() == pytest.approx(1.8001)
    assert p.measure_current() == pytest.approx(0.0123)


def test_scpi_error_raises():
    t = FakeTransport()
    p = E3638A(t)
    t.errors = ['-222,"Data out of range"']
    with pytest.raises(InstrumentError, match="Data out of range"):
        p.setup(current_limit=99, ovp=None, voltage_range=None)


def test_ovp_rejection_is_only_a_warning(caplog):
    t = FakeTransport()
    p = E3638A(t)
    t.errors = ['+0,"No error"', '-113,"Undefined header"']  # CURR ok, OVP rejected
    p.setup(current_limit=0.3, ovp=6.0, voltage_range=None)
    assert "software limits" in caplog.text


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


def test_open_psu_requires_unique_match(monkeypatch):
    two = [Found("GPIB0::5::INSTR", IDN), Found("GPIB0::6::INSTR", IDN)]
    monkeypatch.setattr(discovery, "_visa_candidates", lambda: two)
    with pytest.raises(InstrumentError, match="more than one"):
        discovery.open_psu()


def test_open_psu_falls_back_to_prologix(monkeypatch):
    monkeypatch.setattr(discovery, "_visa_candidates", lambda: [Found("GPIB0::1::INSTR", "HP,34401A,0,1")])
    monkeypatch.setattr(discovery, "_prologix_candidates", lambda addr: [Found("prologix:COM5:5", IDN)])
    opened = []
    monkeypatch.setattr(discovery, "open_transport", lambda res: opened.append(res) or FakeTransport())
    psu = discovery.open_psu()
    assert opened == ["prologix:COM5:5"] and "E3638A" in psu.idn


def test_no_psu_found(monkeypatch):
    monkeypatch.setattr(discovery, "_visa_candidates", list)
    monkeypatch.setattr(discovery, "_prologix_candidates", lambda addr: [])
    with pytest.raises(InstrumentError, match="no E3638A"):
        discovery.open_psu()
