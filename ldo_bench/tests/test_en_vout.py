"""enable-sine --output vout: VOUT (OUTA) following a slow wave on EN_AIN through the whole enable path."""

import pytest
from helpers import invoke, miswire
from pydantic import ValidationError

from ldo_bench import plotting
from ldo_bench.bench import open_bench
from ldo_bench.config import EnSineConfig
from ldo_bench.procedures import en_sine


def run(cfg, setup=None, **upd):
    es = EnSineConfig.model_validate(
        cfg.en_sine.model_dump()
        | {"output": "vout", "shape": "triangle", "frequency": 0.1, "captures": 1}
        | upd
    )
    with open_bench(cfg, sim=True) as b:
        if setup:
            setup(b)
        return en_sine.run(b, es), b


def test_vout_tracks_en_ain_on_sim(jafwak):
    res, b = run(jafwak)
    m = b.sim_model
    assert not res.issues, res.issues
    s = res.summary()
    assert s["output"] == "vout"
    vin = jafwak.en_sine.vin  # the model's VIN is 0 again after the shutdown
    assert s["vt_rise"]["mean"] == pytest.approx(m.en_vt_rise * vin, abs=0.02)
    assert s["vt_fall"]["mean"] == pytest.approx(m.en_vt_fall * vin, abs=0.02)
    assert s["voh"]["mean"] == pytest.approx(m.vout_nominal, abs=0.02)
    text = res.report()
    assert "VOUT" in text and "EN_AIN level where VOUT" in text
    assert any("shutdown" in n for n in res.notes)  # the lag note for a ramp
    fig = en_sine.plot(res, "x")
    assert any("VOUT" in ax.get_ylabel() for ax in fig.axes)
    plotting.close(fig)


def test_wiring_drives_the_ldo_from_the_chain_and_probes_outa(jafwak):
    _, b = run(jafwak)
    text = "\n".join(b.last_wiring)
    assert 'Fit J8 1-2 "LDO" (EN_OUT -> EN_IN)' in text and 'Fit J8 9-10 "LDO" (~EN_OUT -> ~EN_IN)' in text
    assert "Scope 1+ -> J13 pin 4 (OUTA)" in text
    assert "J8 pin 2" not in text


def test_scope_on_en_out_instead_of_outa_is_flagged(jafwak):
    res, _ = run(jafwak, setup=lambda b: miswire(b, ch1_probe="en_out"))
    assert any("OUTA" in i for i in res.issues), res.issues


def test_square_with_vout_is_refused():
    with pytest.raises(ValidationError, match="enable-startup"):
        EnSineConfig(vin=1.8, output="vout", shape="square")


def test_cli(tmp_path):
    r = invoke(
        "enable-sine",
        "--sim",
        "--yes",
        "-o",
        str(tmp_path),
        "--output",
        "vout",
        "--shape",
        "triangle",
        "--freq",
        "0.1",
        "--captures",
        "1",
    )
    assert r.exit_code == 0, r.output
    assert "VOUT" in r.output
