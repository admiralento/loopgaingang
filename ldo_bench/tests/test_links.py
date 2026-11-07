"""The supply and the instrument links say so when a command didn't take or a reply is cut off."""

import logging
from pathlib import Path

import pytest
from test_e36xx import FakeTransport
from typer.testing import CliRunner

from ldo_bench import results
from ldo_bench.bench import open_bench
from ldo_bench.cli import app
from ldo_bench.instruments.ad2 import AnalogDiscovery2
from ldo_bench.instruments.base import InstrumentError
from ldo_bench.instruments.e36xx import AgilentE36xx
from ldo_bench.instruments.sim import SimPowerSupply
from ldo_bench.instruments.transports import PrologixTransport, SerialTransport

CONFIGS = Path(__file__).parents[1] / "configs"
runner = CliRunner()


# --- the supply ------------------------------------------------------------------
def test_output_off_is_confirmed():
    t = FakeTransport()
    p = AgilentE36xx(t)
    p.output(True)
    t.ignore_off = True
    with pytest.raises(InstrumentError, match="still on"):
        p.output(False)


def test_ramp_checks_the_supply_reached_its_setpoint(jafwak):
    with open_bench(jafwak, sim=True) as b:
        b.psu.set_voltage = lambda v: None  # commands lost on the way
        with pytest.raises(InstrumentError, match="setpoint"):
            b.vin_from_psu(1.8)


# --- shutdown failures reach the user ----------------------------------------------
def test_failed_shutdown_step_fails_the_command(monkeypatch, tmp_path):
    def broken_close(self):
        raise InstrumentError("adapter unplugged")

    monkeypatch.setattr(SimPowerSupply, "close", broken_close)
    r = runner.invoke(app, ["smoke", "-c", str(CONFIGS / "jafwak.toml"), "--sim"])
    assert r.exit_code == 1, r.output
    assert "shutdown" in r.output.lower() and "adapter unplugged" in r.output


def test_ad2_close_failures_are_warnings(caplog):
    class Broken:
        def __getattr__(self, name):
            raise RuntimeError("usb gone")

    ad2 = AnalogDiscovery2.__new__(AnalogDiscovery2)
    ad2.dev = Broken()
    with caplog.at_level(logging.WARNING):
        ad2.close()
    assert "usb gone" in caplog.text


# --- serial / Prologix replies -----------------------------------------------------
class LineSerial:
    """Serial fake whose readline returns `reply` as is (no LF = cut off by the timeout)."""

    def __init__(self, reply: bytes):
        self.reply = reply
        self.dtr = False
        self.timeout = 1.0

    def write(self, b):
        pass

    def flush(self):
        pass

    def reset_input_buffer(self):
        pass

    def readline(self):
        return self.reply


def test_serial_reply_cut_off_by_timeout_is_rejected():
    t = SerialTransport("COM3", serial_obj=LineSerial(b"+1.2345"))  # "+1.2345E-03\n" cut short
    with pytest.raises(InstrumentError, match="cut off"):
        t.query("READ?")
    assert SerialTransport("COM3", serial_obj=LineSerial(b"+1.2345E-03\r\n")).query("READ?") == "+1.2345E-03"


def test_prologix_reply_cut_off_is_rejected():
    t = PrologixTransport.__new__(PrologixTransport)
    t._ser = LineSerial(b"+1.2345")
    t.name = "prologix:FAKE:5"
    t.write = lambda cmd: None
    t._raw = lambda cmd: None
    with pytest.raises(InstrumentError, match="cut off"):
        t.query("READ?")


def test_losing_the_dsr_handshake_is_a_warning(caplog):
    class NoDsr(LineSerial):
        dsr = False

    t = SerialTransport("COM3", serial_obj=NoDsr(b"1\n"), flow="dsr-pacing")
    t.DSR_WAIT = 0.01
    with caplog.at_level(logging.WARNING):
        t.write("*CLS")
    assert "DSR" in caplog.text


# --- results folders ---------------------------------------------------------------
def test_unknown_git_state_is_recorded_as_unknown(monkeypatch):
    monkeypatch.setattr(results.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError()))
    assert results._git_info()["dirty"] is None


def test_failed_save_marks_the_folder_partial(monkeypatch, tmp_path):
    from ldo_bench.procedures import load_reg

    def broken_plot(res, dut):
        raise RuntimeError("matplotlib exploded")

    monkeypatch.setattr(load_reg, "plot", broken_plot)
    r = runner.invoke(
        app, ["load-reg", "-c", str(CONFIGS / "jafwak.toml"), "--sim", "--save-sim", "-o", str(tmp_path)]
    )
    assert r.exit_code != 0
    runs = [p.name for p in (tmp_path / "load-reg").iterdir() if p.is_dir()]
    assert len(runs) == 1 and runs[0].endswith("_PARTIAL"), runs
