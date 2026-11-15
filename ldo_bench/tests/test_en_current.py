"""enable-current: the enable block's EN_VDD current against the frequency of a square on EN_AIN."""

import numpy as np
import pytest
from helpers import invoke, miswire
from pydantic import ValidationError

from ldo_bench import plotting
from ldo_bench.bench import open_bench
from ldo_bench.config import EnCurrentConfig, IqConfig
from ldo_bench.instruments.base import InstrumentError
from ldo_bench.procedures import en_current, en_sine


def run(cfg, setup=None, **upd):
    sec = EnCurrentConfig.model_validate(cfg.en_current.model_dump() | upd)
    with open_bench(cfg, sim=True) as b:
        if setup:
            setup(b)
        res = en_current.run(b, sec)
    return res, b


def test_frequencies_are_1_2_5_per_decade_up_to_1_mhz():
    ec = EnCurrentConfig(vin=1.8, fstart=10, fstop=1e6)
    f = ec.frequencies
    assert f[0] == 10 and f[-1] == 1e6 and len(f) == 16
    assert f[:4] == [10, 20, 50, 100]


def test_above_1_mhz_is_refused():
    with pytest.raises(ValidationError, match="1 MHz"):
        EnCurrentConfig(vin=1.8, fstop=2e6)


def test_sweep_on_sim_gives_the_charge_per_cycle(jafwak):
    res, b = run(jafwak)
    m = b.sim_model
    assert not res.issues, res.issues
    assert [p.frequency for p in res.points] == res.frequencies
    # the current rises with frequency: I = I_static + Q f
    top = res.points[-1]
    assert top.current == pytest.approx(m.en_charge * 1e6, rel=0.05)
    assert res.charge == pytest.approx(m.en_charge, rel=0.05)
    assert res.static_current < res.floor  # ~0.3 nA static: under the 10 nA count, reported as a bound
    text = res.report()
    assert "pC per cycle" in text and "< " in text
    assert not m.psu_on and not m.awg_wave  # everything off at the end
    plotting.close(en_current.plot(res, "x"))


def test_static_point_holds_en_ain_at_0_v_not_floating(jafwak):
    seen = []

    def setup(b):
        real = b.awg.set_dc

        def spy(ch, v):
            seen.append((ch, v, dict(b.sim_model.awg_wave)))
            real(ch, v)

        b.awg.set_dc = spy

    run(jafwak, setup=setup)
    assert seen and seen[0][1] == 0.0 and not seen[0][2]  # DC 0 V before any wave


def test_meter_not_in_the_path_is_flagged(jafwak):
    res, _ = run(jafwak, setup=lambda b: miswire(b, dmm_probe="open"))
    assert any("J1 9-11" in i and "doesn't rise" in i for i in res.issues), res.issues


def test_wave_not_reaching_en_ain_is_flagged(jafwak):
    res, _ = run(jafwak, setup=lambda b: miswire(b, ref_probe=("vin", 0)))
    assert any("EN_AIN" in i and "TP6" in i for i in res.issues), res.issues


def test_wiring_powers_vdd_and_leaves_en_out_unprobed(jafwak):
    with open_bench(jafwak, sim=True) as b:
        lines = en_current.wiring(b, jafwak.en_current)
    text = "\n".join(lines)
    assert "Fit J1 5-7" in text and "Fit J1 9-11" not in text  # VDD on; the DMM replaces EN_VDD's jumper
    assert "DMM I (3A/mA jack) -> J1 pin 11 (INB), DMM LO (COM) -> J1 pin 9 (EN_VDD)" in text
    assert not any(ln.startswith("Scope 1") for ln in lines)  # a probe on EN_OUT would add ~24 pF


def test_enable_sine_wiring_powers_vdd(jafwak):
    with open_bench(jafwak, sim=True) as b:
        lines = en_sine.wiring(b, jafwak.en_sine)
    assert "Fit J1 5-7 (VDD from INB)" in lines and "Fit J1 9-11 (EN_VDD from INB)" in lines


def test_en_ain_dc_is_limited(jafwak):
    with open_bench(jafwak, sim=True) as b, pytest.raises(InstrumentError, match="EN_AIN"):
        b.en_ain_dc(1, 2.5)


def test_iq_has_no_en_rail():
    """EN_VDD's ~0.3 nA is under the DMM's count: enable-current measures it, iq doesn't offer it."""
    with pytest.raises(ValidationError, match="rail"):
        IqConfig(method="series", rail="en", vin_start=1.8, vin_stop=1.8)
    r = invoke("iq", "--sim", "--yes", "--method", "series", "--rail", "en")
    assert r.exit_code == 2 and "en" not in r.output.split("--rail")[-1].split("[")[0].split("|")
    help_text = invoke("iq", "--help").output
    assert "bgr|vdd|ota" in help_text.replace(" ", "") and "|en|" not in help_text.replace(" ", "")


def test_cli(tmp_path):
    r = invoke("enable-current", "--sim", "--yes", "-o", str(tmp_path), "--fstop", "1e4")
    assert r.exit_code == 0, r.output
    assert "10000" in r.output or "10 kHz" in r.output


def test_zero_overload_leaves_currents_nan(jafwak):
    def setup(b):
        b.dmm.read = lambda: float("inf")

    res, _ = run(jafwak, setup=setup)
    assert res.issues and all(np.isnan(p.current) for p in res.points)


def test_vin_and_square_are_held_to_the_ceilings(jafwak):
    data = jafwak.model_dump()
    from ldo_bench.config import BenchConfig

    with pytest.raises(ValidationError, match=r"en_current\.vin"):
        BenchConfig.model_validate(data | {"en_current": data["en_current"] | {"vin": 2.0}})
    with pytest.raises(ValidationError, match=r"en_current\.high"):
        BenchConfig.model_validate(data | {"en_current": data["en_current"] | {"high": 2.0}})
