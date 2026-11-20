"""sa-check on the simulated 8591E (CAL OUT cabled to INPUT), and finding the analyzer by ID?."""

import pytest
from helpers import config_path, invoke

from ldo_bench.instruments.base import InstrumentError
from ldo_bench.instruments.hp859x import HP859x
from ldo_bench.instruments.sim import Sim8591Link
from ldo_bench.procedures import sa_check


def _run(jafwak, **link_kw):
    link = Sim8591Link(**link_kw)
    return sa_check.run(HP859x(link), jafwak.sa), link


def test_passes_on_the_calibrator(jafwak):
    res, link = _run(jafwak)
    assert not res.issues, res.issues
    assert res.cal.dbm == pytest.approx(-20.0, abs=0.1)
    assert res.narrowest_rbw == 300 and res.summary()["option_130"] is False
    assert res.floor.dbm == pytest.approx(-115.0 + 10 * __import__("math").log10(0.3), abs=0.1)
    assert [c for c in link.sent if c != "ERR?"][-1] == "IP;"  # left preset, sweeping continuously
    assert "calibrator" in res.report() and "no Option 130" in res.report()


def test_finds_option_130():
    from conftest import CONFIG_DIR

    from ldo_bench.config import load_config

    cfg = load_config(CONFIG_DIR / "jafwak.toml")
    link = Sim8591Link()
    link.narrow_rbw = True
    res = sa_check.run(HP859x(link), cfg.sa)
    assert res.narrowest_rbw == 30 and res.summary()["option_130"] is True


def test_no_calibrator_cable_is_a_setup_issue(jafwak):
    res, _ = _run(jafwak, tones={})
    assert any("CAL OUT cabled" in i for i in res.issues), res.issues


def test_a_wrong_level_is_a_setup_issue(jafwak):
    res, _ = _run(jafwak, tones={300e6: -23.0})  # 3 dB low: a bad cable, or the reading
    assert any("-23.00 dBm" in i for i in res.issues), res.issues


def test_identify_falls_back_to_id_query():
    from ldo_bench.instruments.discovery import _identify

    class Scpi:
        def query(self, cmd):
            return {"*IDN?": "Agilent Technologies,E3648A,0,1.7-5.0-1.0"}[cmd]

    class Old:
        def __init__(self):
            self.sent: list[str] = []

        def query(self, cmd):
            self.sent.append(cmd)
            return {"*IDN?": "", "ID?": "HP8591E"}[cmd]

    class Silent:
        def query(self, cmd):
            if cmd == "ID?":
                raise InstrumentError("timeout")
            return ""

    assert _identify(Scpi()).startswith("Agilent")
    old = Old()
    assert _identify(old) == "HP8591E" and old.sent == ["*IDN?", "ID?"]
    assert _identify(Silent()) == ""


def test_open_sa_finds_it_by_its_id_reply(monkeypatch):
    from ldo_bench.instruments import discovery

    monkeypatch.setattr(
        discovery,
        "_visa_candidates",
        lambda: [
            discovery.Found("USB0::1::2::E3648A::INSTR", "Agilent Technologies,E3648A,0,1.7-5.0-1.0"),
            discovery.Found("USB0::1003::8293::HP8591E::0::INSTR", "HP8591E"),
        ],
    )
    opened = []
    monkeypatch.setattr(discovery, "open_transport", lambda res, **k: opened.append(res) or Sim8591Link())
    sa = discovery.open_sa("")
    assert opened == ["USB0::1003::8293::HP8591E::0::INSTR"] and sa.model == "HP8591E"


def test_cli_sa_check_on_the_simulator(tmp_path):
    r = invoke("sa-check", "-c", config_path("jafwak"), "--sim", "-o", str(tmp_path))
    assert r.exit_code == 0, r.output
    assert "SIMULATED" in r.output and "calibrator" in r.output
    assert not any(tmp_path.rglob("*.csv"))
    r = invoke("sa-check", "-c", config_path("demo_board"), "--sim")
    assert r.exit_code == 2 and "section" in r.output
