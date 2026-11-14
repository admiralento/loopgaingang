import math

import pytest

from ldo_bench.bench import open_bench
from ldo_bench.instruments import discovery
from ldo_bench.instruments.base import InstrumentError
from ldo_bench.instruments.hp34401a import HP34401A
from ldo_bench.instruments.transports import SerialTransport

IDN = "HEWLETT-PACKARD,34401A,0,11-5-2"


class FakeSerial:
    """Echoes canned replies; records what was written."""

    def __init__(self, replies):
        self.out = b""
        self.replies = dict(replies)
        self._pending: list[bytes] = []
        self.dtr = False

    def write(self, b):
        self.out += b
        cmd = b.decode().strip()
        if cmd in self.replies:
            self._pending.append((self.replies[cmd] + "\r\n").encode())

    def readline(self):
        return self._pending.pop(0) if self._pending else b""

    def reset_input_buffer(self):
        pass

    def close(self):
        self.out += b"<closed>"


def test_serial_transport_query_and_missing_reply():
    ser = FakeSerial({"*IDN?": IDN})
    t = SerialTransport("COM3", serial_obj=ser)
    assert t.query("*IDN?") == IDN  # CR LF stripped
    with pytest.raises(InstrumentError, match="null-modem"):
        t.query("READ?")


def test_34401a_remote_local_and_config():
    ser = FakeSerial({"*IDN?": IDN, "SYST:ERR?": '+0,"No error"', "READ?": "+1.19987654E+00"})
    dmm = HP34401A(SerialTransport("COM3", serial_obj=ser))
    dmm.configure_dcv(range=None, nplc=10)
    assert dmm.read_dcv() == pytest.approx(1.19987654)
    dmm.close()
    lines = ser.out.decode().split("\n")
    assert lines[:2] == ["", "SYST:REM"]  # flush junk, then remote before anything else
    assert "CONF:VOLT:DC AUTO" in lines and "VOLT:DC:NPLC 10" in lines and "INP:IMP:AUTO ON" in lines
    assert ser.out.endswith(b"SYST:LOC\n<closed>")  # front panel handed back


def test_34401a_silent_gives_rs232_hint():
    with pytest.raises(InstrumentError, match="I/O MENU"):
        HP34401A(SerialTransport("COM3", serial_obj=FakeSerial({})))


def test_34401a_wrong_model():
    with pytest.raises(InstrumentError, match="not a 34401A"):
        HP34401A(SerialTransport("COM3", serial_obj=FakeSerial({"*IDN?": "HP,3478A,0,1"})))


def test_failed_dmm_open_closes_psu_and_ad2(monkeypatch, jafwak):
    closed = []

    class Inst:
        def __init__(self, name):
            self.name = name

        def close(self):
            closed.append(self.name)

    monkeypatch.setattr(discovery, "open_psu", lambda *a, **k: Inst("psu"))
    monkeypatch.setattr(discovery, "open_ad2", lambda *a, **k: Inst("ad2"))

    def no_dmm(*a, **k):
        raise InstrumentError("no 34401A")

    monkeypatch.setattr(discovery, "open_dmm", no_dmm)
    with pytest.raises(InstrumentError):
        open_bench(jafwak)
    assert closed == ["ad2", "psu"]


def test_measure_point_sources(jafwak):
    with open_bench(jafwak, sim=True) as b:
        b.vin_from_psu(1.8)
        p = b.measure_point(0.1)
        assert p.source == "dmm" and p.vout == pytest.approx(p.vout_ad2, abs=3e-3)
        assert "dmm" in b.idns()
    with open_bench(jafwak, sim=True, use_dmm=False) as b:
        b.vin_from_psu(1.8)
        assert b.measure_point(0.1).source == "ad2"


def test_parity_mismatch_is_reported_not_parsed():
    # What the bench meter (EVEN: 7 BITS) looked like when read as 8N2: bit 7 set on some chars.
    garbled = "H\xc5\xd7\xcc\xc5\xd4\xd4-PA\xc3KA\xd2D,34401A"

    class RawSerial(FakeSerial):
        def readline(self):
            return garbled.encode("latin-1") + b"\r\n"

    t = SerialTransport("COM3", serial_obj=RawSerial({}))
    with pytest.raises(InstrumentError, match="parity"):
        t.query("*IDN?")


def test_unknown_parity_rejected():
    with pytest.raises(InstrumentError, match="unknown parity"):
        SerialTransport("COM3", parity="mark", serial_obj=FakeSerial({}))


def test_34401a_dci_and_overload():
    sent = []

    class T:
        name, kind = "fake", "gpib"

        def write(self, c):
            sent.append(c)

        def query(self, c):
            sent.append(c)
            return {"*IDN?": "HEWLETT-PACKARD,34401A,0,11-5-2", "SYST:ERR?": '+0,"No error"'}.get(
                c, "+9.9E+37"
            )

    d = HP34401A(T())
    d.configure_dci(range=0.01, nplc=10)
    assert "CONF:CURR:DC 0.01" in sent and "CURR:DC:NPLC 10" in sent
    assert d.read() == math.inf


def test_34401a_dcv_overload_is_inf_not_a_voltage():
    """An overloaded DC volts reading (+9.9E+37) must never be averaged or saved as a voltage."""

    class T:
        name, kind = "fake", "gpib"

        def write(self, c):
            pass

        def query(self, c):
            return {"*IDN?": "HEWLETT-PACKARD,34401A,0,11-5-2", "SYST:ERR?": '+0,"No error"'}.get(
                c, "-9.9E+37"
            )

    assert HP34401A(T()).read_dcv() == -math.inf


def test_34401a_input_impedance_switch():
    ser = FakeSerial({"*IDN?": IDN, "SYST:ERR?": '+0,"No error"'})
    dmm = HP34401A(SerialTransport("COM3", serial_obj=ser))
    dmm.set_input_z(high=False)
    dmm.set_input_z(high=True)
    lines = ser.out.decode().split("\n")
    assert lines.index("INP:IMP:AUTO OFF") < lines.index("INP:IMP:AUTO ON", lines.index("INP:IMP:AUTO OFF"))
