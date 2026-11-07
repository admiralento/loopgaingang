"""Every GPIB/RS-232 instrument works on every interface (VISA, Prologix, RS-232), found by model."""

import pytest

from ldo_bench.instruments import discovery
from ldo_bench.instruments.base import InstrumentError
from ldo_bench.instruments.discovery import Finder, Found
from ldo_bench.instruments.transports import SerialSettings, SerialTransport, open_transport, parse_block

PSU = "Agilent Technologies,E3648A,0,1.7-5.0-1.0"
DMM = "HEWLETT-PACKARD,34401A,0,11-5-2"
SCOPE = "AGILENT TECHNOLOGIES,54621A,MY123,A.02.10"


class Fake:
    """Transport fake for any instrument; `kind` decides serial-only behaviour."""

    def __init__(self, idn, kind="visa", name="fake"):
        self.idn, self.kind, self.name = idn, kind, name
        self.sent = []

    def write(self, c):
        self.sent.append(c)

    def query(self, c):
        self.sent.append(c)
        if c == "*IDN?":
            return self.idn
        if "ERR" in c:
            return '+0,"No error"'
        return "0"

    def read_line(self):
        return "1"

    def read_block(self, c):
        return b""

    def set_timeout(self, s):
        return 5.0

    def close(self):
        self.sent.append("<close>")


@pytest.fixture
def bus(monkeypatch):
    """A fake bench: what VISA, each RS-232 port (per settings) and Prologix would answer."""
    state = {"visa": [], "serial": {}, "prologix": [], "opened": [], "probes": []}
    monkeypatch.setattr(discovery, "_visa_candidates", lambda: list(state["visa"]))
    monkeypatch.setattr(discovery, "_serial_ports", lambda: sorted({p for p, _ in state["serial"]}))

    def probe(port, s, remote):
        state["probes"].append((port, s.parity, s.baud))
        return state["serial"].get((port, (s.baud, s.parity)))

    monkeypatch.setattr(discovery, "_probe_serial", probe)
    monkeypatch.setattr(
        discovery, "_prologix_candidates", lambda addr, skip=frozenset(): list(state["prologix"])
    )

    def opener(res, serial=None, **k):
        known = {f.resource: f.idn for f in state["visa"] + state["prologix"]}
        known |= {f"serial:{port}": idn for (port, _), idn in state["serial"].items()}
        state["opened"].append((res, serial))
        return Fake(known.get(res, ""), "serial" if res.startswith("serial:") else "visa", res)

    monkeypatch.setattr(discovery, "open_transport", opener)
    return state


def test_psu_on_rs232_found_with_the_parity_it_uses(bus):
    bus["serial"][("COM3", (9600, "even"))] = PSU  # supply set to 7E2; config says none
    psu = discovery.open_psu(serial=SerialSettings(9600, "none", 2, "dsr-pacing"))
    res, used = bus["opened"][-1]
    assert res == "serial:COM3" and used.parity == "even"
    assert psu.serial and psu.t.sent[:2] == ["", "SYST:REM"]  # RS-232: remote first
    psu.close()
    assert psu.t.sent[-2:] == ["SYST:LOC", "<close>"]  # and the front panel handed back


def test_psu_on_gpib_has_no_remote_dance(bus):
    bus["visa"] = [Found("GPIB0::5::INSTR", PSU)]
    psu = discovery.open_psu()
    assert not psu.serial and "SYST:REM" not in psu.t.sent


def test_dmm_on_gpib_found_before_any_serial_probe(bus):
    bus["visa"] = [Found("GPIB0::22::INSTR", DMM)]
    bus["serial"][("COM3", (9600, "none"))] = PSU
    dmm = discovery.open_dmm()
    assert bus["opened"][-1][0] == "GPIB0::22::INSTR" and not dmm.serial
    assert bus["probes"] == []  # VISA had it: no RS-232 probing at all


def test_scope_on_gpib_and_on_prologix(bus):
    bus["visa"] = [Found("GPIB0::7::INSTR", SCOPE)]
    s = discovery.open_xscope()
    assert s.model == "54621A" and bus["opened"][-1][0] == "GPIB0::7::INSTR"
    bus["visa"] = []
    bus["prologix"] = [Found("prologix:COM5:7", SCOPE)]
    discovery.open_xscope()
    assert bus["opened"][-1][0] == "prologix:COM5:7"


def test_one_search_shared_and_claimed_ports_not_reprobed(bus):
    bus["serial"][("COM3", (9600, "none"))] = PSU
    bus["serial"][("COM4", (9600, "none"))] = DMM
    bus["serial"][("COM6", (57600, "none"))] = SCOPE
    f = Finder()
    discovery.open_psu(finder=f)
    discovery.open_dmm(finder=f)
    discovery.open_xscope(serial=SerialSettings(57600, "none", 1, "dsrdtr"), finder=f)
    assert [r for r, _ in bus["opened"]] == ["serial:COM3", "serial:COM4", "serial:COM6"]
    # after the PSU claimed COM3 and the DMM COM4, the scope search never probed them again
    scope_probes = [p for p, _, baud in bus["probes"] if baud == 57600]
    assert "COM3" not in scope_probes and "COM4" not in scope_probes


def test_explicit_resource_any_form(bus):
    for res in ("GPIB0::5::INSTR", "USB0::1003::8293::X::0::INSTR", "prologix:COM5:5", "serial:COM3"):
        bus["visa"] = [Found(res, PSU)] if not res.startswith("serial:") else []
        bus["serial"] = {("COM3", (9600, "none")): PSU}
        discovery.open_psu(res)
        assert bus["opened"][-1][0] == res
    assert bus["probes"] == []  # explicit resources skip the search entirely


def test_not_found_names_every_interface(bus):
    with pytest.raises(InstrumentError, match=r"VISA .*RS-232 ports or Prologix"):
        discovery.open_dmm()


def test_two_matches_asks_to_choose(bus):
    bus["visa"] = [Found("GPIB0::5::INSTR", PSU), Found("GPIB0::6::INSTR", PSU)]
    with pytest.raises(InstrumentError, match="more than one"):
        discovery.open_psu()


def test_open_transport_serial_settings(monkeypatch):
    made = {}

    class Spy(SerialTransport):
        def __init__(self, port, **kw):
            made.update(port=port, **kw)

    monkeypatch.setattr("ldo_bench.instruments.transports.SerialTransport", Spy)
    open_transport("serial:COM3", serial=SerialSettings(57600, "none", 1, "dsrdtr"))
    assert made["port"] == "COM3" and made["baud"] == 57600 and made["stopbits"] == 1
    assert made["flow"] == "dsrdtr"


def test_parse_block():
    assert parse_block(b"#800000003abc\n", "q", "x") == b"abc"
    with pytest.raises(InstrumentError, match="truncated"):
        parse_block(b"#800000009abc", "q", "x")
    with pytest.raises(InstrumentError, match="data block"):
        parse_block(b"1.23", "q", "x")


def test_prologix_read_block():
    from ldo_bench.instruments.transports import PrologixTransport

    class Ser:
        def __init__(self, data):
            self.buf, self.out, self.timeout = data, b"", 1.0

        def write(self, b):
            self.out += b

        def reset_input_buffer(self):
            pass

        def read(self, n):
            r, self.buf = self.buf[:n], self.buf[n:]
            return r

        def readline(self):
            i = self.buf.find(b"\n")
            r, self.buf = (self.buf[: i + 1], self.buf[i + 1 :]) if i >= 0 else (self.buf, b"")
            return r

    payload = bytes([10, 0, 255, 10])  # contains '\n' bytes: must not end the read early
    t = PrologixTransport("COM5", 7, serial_obj=Ser(b"#800000004" + payload + b"\n"))
    assert t.read_block(":WAVeform:DATA?") == payload
    assert t.kind == "prologix"
