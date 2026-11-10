"""54621A cross-check scope: driver protocol (fake serial) and integration with the transients."""

import numpy as np
import pytest

from ldo_bench.bench import open_bench
from ldo_bench.instruments import discovery
from ldo_bench.instruments.agilent546xx import Agilent54621A
from ldo_bench.instruments.base import InstrumentError
from ldo_bench.instruments.transports import SerialTransport
from ldo_bench.procedures import line_tran, load_tran

IDN = "AGILENT TECHNOLOGIES,54621A,MY12345678,A.02.10"


class FakeScope:
    """Transport-level fake of a 54621A: records commands, answers queries, serves WORD blocks."""

    name = "serial:FAKE"

    def __init__(self, opc_after_extra: int = 0, opc_reply: str = "1"):
        self.sent: list[str] = []
        self.opc_after_extra = opc_after_extra  # extra steps needed before *OPC? answers
        self.opc_reply = opc_reply  # what *OPC? answers once done (the bench 54624A says '+1')
        self.extra = 0
        self.source = 1
        self.timeout = 5.0

    def write(self, cmd):
        self.sent.append(cmd)
        if cmd.startswith(":WAVeform:SOURce CHANnel"):
            self.source = int(cmd[-1])

    def query(self, cmd):
        self.sent.append(cmd)
        if cmd == "*IDN?":
            return IDN
        if cmd == ":SYSTem:ERRor?":
            return '+0,"No error"'
        if cmd == ":WAVeform:PREamble?":
            # format WORD, type AVER, 4 points, 16 avg, 1 us/pt, xorigin -1 us, xref 0,
            # yinc 1 mV/code, yorigin 1.0 V (ch1) / 0 V (ch2), yref 32768
            yorg = 1.0 if self.source == 1 else 0.0
            return f"1,2,4,16,1E-06,-1E-06,0,1E-03,{yorg},32768"
        raise AssertionError(cmd)

    def read_line(self):
        if self.sent[-1] == "*OPC?" and self.extra < self.opc_after_extra:
            return ""  # not done yet: the driver should ask for another step
        return self.opc_reply

    def read_block(self, cmd):
        self.sent.append(cmd)
        codes = [32768, 32768 - 20, 32768 + 5, 32768] if self.source == 1 else [32768, 35068, 35068, 35068]
        return b"".join(c.to_bytes(2, "big") for c in codes)

    def set_timeout(self, s):
        old, self.timeout = self.timeout, s
        return old

    def close(self):
        self.sent.append("<close>")


def arm(scope, **kw):
    args = dict(
        window=819.2e-6,
        pre_trigger=0.2,
        vout_channel=1,
        vout_range=0.16,
        vout_offset=1.0,
        ref_channel=2,
        ref_range=8.0,
        ref_offset=1.65,
        trigger_level=1.65,
        edge="rising",
        averages=16,
        points=2000,
    )
    args.update(kw)
    scope.arm(**args)


def test_rejects_other_instrument():
    t = FakeScope()
    t.query = lambda c: "HEWLETT-PACKARD,34401A,0,11-5-2"
    with pytest.raises(InstrumentError, match="not a 54620"):
        Agilent54621A(t)


def test_arm_sends_the_guides_commands():
    t = FakeScope()
    s = Agilent54621A(t)
    arm(s, edge="falling")
    sent = [c for c in t.sent if not c.startswith(":SYSTem:ERRor")]
    for cmd in (
        ":TIMebase:MODE MAIN",
        ":CHANnel1:RANGe 0.16",
        ":CHANnel1:OFFSet 1",
        ":TIMebase:REFerence LEFT",
        ":TIMebase:POSition -0.00016384",  # left edge is 20 % of the window before the trigger
        ":TRIGger:EDGE:SOURce CHANnel2",
        ":TRIGger:EDGE:SLOPe NEGative",
        ":TRIGger:EDGE:LEVel 1.65",
        ":TRIGger:SWEep NORMal",
        ":ACQuire:TYPE AVERage",
        ":ACQuire:COUNt 16",
        ":WAVeform:FORMat WORD",
        ":WAVeform:POINts 2000",
    ):
        assert cmd in sent, cmd
    assert sent[-1] == ":DIGitize CHANnel1,CHANnel2"  # last: starts the acquisition


def test_collect_decodes_word_blocks():
    t = FakeScope()
    s = Agilent54621A(t)
    arm(s)
    tr = s.collect(lambda: None, 0, 10)
    np.testing.assert_allclose(tr[1].v, [1.0, 0.980, 1.005, 1.0])  # (code - yref) * yinc + yorigin
    np.testing.assert_allclose(tr[1].t, [-1e-6, 0, 1e-6, 2e-6])
    np.testing.assert_allclose(tr[2].v, [0.0, 2.3, 2.3, 2.3])
    assert tr[1].averages == 16


@pytest.mark.parametrize("reply", ["1", "+1", "+1.0E+00"])
def test_collect_accepts_any_numeric_one_from_opc(reply):
    # the bench 54624A (firmware A.02.20) answers *OPC? with '+1', not '1' (2026-11-10)
    t = FakeScope(opc_reply=reply)
    s = Agilent54621A(t)
    arm(s)
    steps = []
    tr = s.collect(lambda: steps.append(1), 10, 10)
    assert not s.busy and not steps and set(tr) == {1, 2}


def test_collect_rejects_an_unexpected_opc_reply_and_says_what_it_was():
    t = FakeScope(opc_reply="-113")
    s = Agilent54621A(t)
    arm(s)
    with pytest.raises(InstrumentError, match="'-113'"):
        s.collect(lambda: None, 10, 10)
    assert s.busy


def test_collect_steps_again_until_average_completes():
    t = FakeScope(opc_after_extra=3)
    s = Agilent54621A(t)
    arm(s)
    steps = []

    def again():
        steps.append(1)
        t.extra += 1

    s.collect(again, 10, 10)
    assert len(steps) == 3 and not s.busy


def test_collect_gives_up_and_marks_busy():
    t = FakeScope(opc_after_extra=999)
    s = Agilent54621A(t)
    arm(s)
    with pytest.raises(InstrumentError, match="Run/Stop"):
        s.collect(lambda: setattr(t, "extra", t.extra + 1), 5, 10)
    assert s.busy


class BlockSerial:
    def __init__(self, data: bytes):
        self.buf = data
        self.dtr = False
        self.timeout = 1.0

    def write(self, b):
        pass

    def flush(self):
        pass

    def reset_input_buffer(self):
        pass

    def read(self, n):
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def readline(self):
        i = self.buf.find(b"\n")
        out, self.buf = (self.buf[: i + 1], self.buf[i + 1 :]) if i >= 0 else (self.buf, b"")
        return out


def test_read_block_parses_ieee_definite_length():
    payload = bytes(range(10))
    t = SerialTransport(
        "COM9", stopbits=1, flow="none", serial_obj=BlockSerial(b"#800000010" + payload + b"\n")
    )
    assert t.read_block(":WAVeform:DATA?") == payload


def test_read_block_rejects_truncation():
    t = SerialTransport("COM9", stopbits=1, flow="none", serial_obj=BlockSerial(b"#800000010abc"))
    with pytest.raises(InstrumentError, match="truncated"):
        t.read_block(":WAVeform:DATA?")


@pytest.mark.parametrize("proc", [load_tran, line_tran])
def test_cross_check_on_sim(proc, jafwak):
    sec = jafwak.load_tran if proc is load_tran else jafwak.line_tran
    an = jafwak.analysis.model_copy(update={"settle_reference": "post"})
    with open_bench(jafwak, sim=True, use_xscope=True) as b:
        res = proc.run(b, sec.model_copy(update={"averages": 4}), an)
    for e in res.edges:
        x = e.xcheck
        assert x is not None and not x.issues and len(x.t) == jafwak.xscope.points
        # both scopes see the same edge: peaks agree within a few tenths of a mV in the sim
        assert x.metrics.deviation == pytest.approx(e.metrics.deviation, abs=1e-3)
    assert "54621A" in res.report() and "xcheck" in res.summary()
    assert set(res.extra_csvs()) >= {f"waveform_xcheck_{e.edge}.csv" for e in res.edges}


def test_missing_scope_is_not_fatal(jafwak, monkeypatch):
    """The scope is optional: if it can't be opened the bench still opens, and says why."""

    class Inst:
        idn = "fake"

        def close(self):
            pass

    monkeypatch.setattr(discovery, "open_psu", lambda *a, **k: Inst())
    monkeypatch.setattr(discovery, "open_ad2", lambda *a, **k: Inst())

    def no_scope(*a, **k):
        raise InstrumentError("no 54620-series scope found")

    monkeypatch.setattr(discovery, "open_xscope", no_scope)
    b = open_bench(jafwak, use_dmm=False, use_xscope=True)
    assert b.xscope is None and "no 54620-series scope found" in b.xscope_issue


def test_unavailable_scope_reported_per_edge_not_invalidating(jafwak):
    with open_bench(jafwak, sim=True) as b:
        b.xscope_issue = "cross-check scope not available: no 54620-series scope found"
        res = load_tran.run(b, jafwak.load_tran.model_copy(update={"averages": 2}), jafwak.analysis)
    for e in res.edges:
        assert e.xcheck.issues == [b.xscope_issue]
    assert not any("cross-check" in i for i in res.issues)  # AD2 validity unaffected


def test_busy_scope_skipped_and_ad2_unaffected(jafwak):
    an = jafwak.analysis.model_copy(update={"settle_reference": "post"})
    with open_bench(jafwak, sim=True, use_xscope=True) as b:
        b.xscope.busy = True  # e.g. a previous edge never completed its average
        res = load_tran.run(b, jafwak.load_tran.model_copy(update={"averages": 2}), an)
    assert all("still busy" in e.xcheck.issues[0] for e in res.edges)
    assert not res.issues
