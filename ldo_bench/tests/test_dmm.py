import pytest

from ldo_bench.bench import open_bench
from ldo_bench.instruments import discovery
from ldo_bench.instruments.base import InstrumentError
from ldo_bench.instruments.discovery import Found
from ldo_bench.instruments.hp34401a import HP34401A
from ldo_bench.instruments.transports import SerialTransport, open_transport

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


def test_open_transport_serial_prefix(monkeypatch):
    made = {}

    class Spy(SerialTransport):
        def __init__(self, port, *, baud, parity, timeout):
            made.update(port=port, baud=baud, parity=parity)

    monkeypatch.setattr("ldo_bench.instruments.transports.SerialTransport", Spy)
    open_transport("serial:/dev/cu.usbserial-ABB83W3S", baud=19200, parity="even")
    assert made == {"port": "/dev/cu.usbserial-ABB83W3S", "baud": 19200, "parity": "even"}


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


def test_open_dmm_not_found(monkeypatch):
    monkeypatch.setattr(discovery, "_serial_instrument_candidates", lambda baud, parity: [])
    monkeypatch.setattr(discovery, "_visa_candidates", list)
    with pytest.raises(InstrumentError, match="--no-dmm"):
        discovery.open_dmm()


def test_open_dmm_prefers_serial(monkeypatch):
    monkeypatch.setattr(
        discovery, "_serial_instrument_candidates", lambda baud, parity: [Found("serial:COM3", IDN)]
    )
    opened = []
    monkeypatch.setattr(discovery, "HP34401A", lambda t: opened.append(t) or "dmm")
    monkeypatch.setattr(discovery, "open_transport", lambda res, baud, parity: res)
    assert discovery.open_dmm() == "dmm" and opened == ["serial:COM3"]


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


def test_discovery_falls_back_to_other_parity(monkeypatch):
    monkeypatch.setattr(discovery, "_serial_ports", lambda: ["COM3"])
    tried = []

    class Probe:
        def __init__(self, port, *, baud, parity, timeout):
            tried.append(parity)
            self.parity = parity

        def write(self, cmd):
            pass

        def query(self, cmd):
            if self.parity != "even":
                raise InstrumentError("garbled reply")
            return IDN

        def close(self):
            pass

    monkeypatch.setattr(discovery, "SerialTransport", Probe)
    found = discovery._serial_instrument_candidates(9600, "none")
    assert tried == ["none", "even"]
    assert found == [Found("serial:COM3", IDN, "even")]
