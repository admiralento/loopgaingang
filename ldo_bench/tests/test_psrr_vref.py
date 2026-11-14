"""psrr --measure vref: the BGR's rejection, VREF (TP3) through a 10x probe instead of VOUT."""

import pytest
from helpers import invoke, miswire

from ldo_bench import plotting
from ldo_bench.bench import open_bench
from ldo_bench.config import PsrrConfig
from ldo_bench.procedures import psrr


def run(cfg, setup=None, **upd):
    ps = PsrrConfig.model_validate(cfg.psrr.model_dump() | {"measure": "vref", "f_stop": 1e4} | upd)
    with open_bench(cfg, sim=True) as b:
        if setup:
            setup(b)
        return psrr.run(b, ps), b


def weak_bgr(b):
    b.sim_model.bgr_psrr_db = 25.0  # enough ripple at TP3 to clear the AD2's floor through the 10x probe


def test_vref_psrr_on_sim(jafwak):
    res, b = run(jafwak, setup=weak_bgr)
    assert not res.issues, res.issues
    measured = [p for p in res.points if not p.limited]
    assert measured, "every point under the floor"
    for p in measured:
        assert p.psrr_db == pytest.approx(b.sim_model.bgr_psrr_db, abs=3)
    s = res.summary()
    assert s["measured"] == "vref" and s["probe_attenuation"] == 10
    assert "VREF" in res.report() and "VOUT (uV" not in res.report()
    plotting.close(psrr.plot(res, "x"))


def test_a_good_bgr_through_the_probe_gives_bounds(jafwak):
    """60 dB on 50 mV is 50 uV at TP3, 5 uV after the 10x probe: under the AD2's floor, so "at least"."""
    res, _ = run(jafwak)
    assert not res.issues, res.issues
    assert all(p.limited for p in res.points)
    assert any("VREF's ripple is under what the AD2 resolves" in n for n in res.notes)


def test_wiring_puts_scope_1_on_tp3_through_the_probe(jafwak):
    _, b = run(jafwak)
    text = "\n".join(b.last_wiring)
    assert "Scope 1+ -> VREF (TP3) through a 10x probe" in text
    assert "OUTA" not in text.split("Scope 1+")[1].split("\n")[0]


def test_probe_set_to_1x_shows_as_a_vref_level_issue(jafwak):
    """The config says 10x but the probe is on 1x: VREF reads ten times too high."""
    res, _ = run(jafwak, setup=lambda b: miswire(b, ch1_atten=1.0))
    assert any("VREF" in i and "10x" in i for i in res.issues), res.issues


def test_vout_is_still_the_default(jafwak):
    assert jafwak.psrr.measure == "vout"


def test_cli(tmp_path):
    r = invoke("psrr", "--sim", "--yes", "-o", str(tmp_path), "--measure", "vref", "--fstop", "1e4")
    assert r.exit_code == 0, r.output
    assert "VREF" in r.output
