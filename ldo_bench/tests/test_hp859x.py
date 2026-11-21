"""HP 8591E driver against the simulated GPIB link (it speaks the 8590 command set)."""

import pytest

from ldo_bench.instruments.base import InstrumentError
from ldo_bench.instruments.hp859x import HP859x, Marker, dbm_to_vpeak, is_hp859x, vpeak_to_dbm
from ldo_bench.instruments.sim import Sim8591Link


def test_identifies_by_id_query_and_clears_old_errors():
    link = Sim8591Link()
    link.err = [113]  # e.g. the *IDN? discovery sent, which it doesn't know
    sa = HP859x(link)
    assert sa.model == "HP8591E"
    assert "940822" in sa.idn and "3728" in sa.idn
    assert link.err == []  # read (and cleared) at open
    assert "*IDN?" not in link.sent


def test_refuses_another_instrument():
    link = Sim8591Link()
    link.id_reply = "E3648A"
    with pytest.raises(InstrumentError, match="not an HP 8590"):
        HP859x(link)


def test_tune_sends_the_8590_commands_and_reports_the_rbw_it_got():
    link = Sim8591Link()
    sa = HP859x(link)
    got = sa.tune(center=300e6, span=1e6, rbw=3e3, vbw=1e3, ref_level_dbm=-10, attenuation_db=10)
    assert [c for c in link.sent if c.split()[0] in ("CF", "SP", "RB", "VB", "RL", "AT")] == [
        "CF 300000000HZ;",
        "SP 1000000HZ;",
        "RB 3000HZ;",
        "VB 1000HZ;",
        "RL -10DBM;",
        "AT 10DB;",
    ]
    assert got == 3e3
    assert sa.tune(center=1e6, span=1e3, rbw=30) == 300  # no Option 130: 300 Hz, as on the bench unit
    link.narrow_rbw = True
    assert sa.tune(center=1e6, span=1e3, rbw=30) == 30


def test_a_command_the_analyzer_rejects_is_an_error():
    link = Sim8591Link()
    sa = HP859x(link)
    link.reject = "SP"
    with pytest.raises(InstrumentError, match=r"SP .* error code"):
        sa.tune(center=1e6, span=1e3, rbw=1e3)


def test_reads_the_calibrator_tone():
    sa = HP859x(Sim8591Link())  # CAL OUT cabled to the input: 300 MHz, -20 dBm
    sa.preset()
    sa.tune(center=300e6, span=1e6, rbw=3e3)
    sa.sweep()
    m = sa.peak()
    assert m.frequency == pytest.approx(300e6, abs=10e3) and m.dbm == pytest.approx(-20.0, abs=0.1)
    floor = sa.marker_at(300e6 + 400e3)
    assert floor.dbm < -100


def test_sweep_not_done_and_garbled_replies_are_errors():
    link = Sim8591Link()
    sa = HP859x(link)
    link.done_reply = "0"
    with pytest.raises(InstrumentError, match="sweep not done"):
        sa.sweep()
    link.done_reply = "1"
    link.garble = "MKA?"
    with pytest.raises(InstrumentError, match="not a number"):
        sa.peak()
    link.garble = None
    link.amplitude_override = 99.0
    with pytest.raises(InstrumentError, match="outside"):
        sa.peak()


def test_dbm_and_volts():
    assert dbm_to_vpeak(-20.0) == pytest.approx(0.031623, rel=1e-4)  # 10 uW in 50 ohm: 22.4 mV rms
    assert vpeak_to_dbm(dbm_to_vpeak(-73.2)) == pytest.approx(-73.2)
    assert Marker(1e6, 0.0).vpeak == pytest.approx(0.316228, rel=1e-4)  # 1 mW
    assert is_hp859x("HP8591E") and is_hp859x(" hp8593e ") and not is_hp859x("")
