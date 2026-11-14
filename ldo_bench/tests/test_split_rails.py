"""Dropout "Method B" (line-reg --split-rails): BGR_VDD and OTA_VDD held on PSU OUT2 while VIN sweeps,
so only the pass device (VDD) drops out."""

import pytest
from helpers import invoke, miswire, spy
from pydantic import ValidationError

from ldo_bench.bench import open_bench
from ldo_bench.config import BenchConfig
from ldo_bench.instruments.base import InstrumentError
from ldo_bench.procedures import line_reg


def run(cfg, setup=None):
    with open_bench(cfg, sim=True) as b:
        if setup:
            setup(b)
        return line_reg.run(b, cfg.line_reg, split_rails=True), b


def test_method_b_on_sim(jafwak):
    levels = []

    def watch(b):
        real = b.psu.aux

        def aux(ch):
            out = real(ch)
            spy(out, "set_voltage", levels)
            return out

        b.psu.aux = aux

    res, _ = run(jafwak, setup=watch)
    assert not res.issues, res.issues
    sr = jafwak.split_rails
    assert max(a[0] for _, a, _ in levels) == pytest.approx(sr.voltage)
    assert levels[-1][1][0] == 0.0  # back to 0 V before the end
    s = res.summary()
    assert s["split_rails_v"] == pytest.approx(sr.voltage)
    assert any("Method B" in n for n in res.notes)
    assert not any("vref-line" in n for n in res.notes)  # the BGR has its own supply here


def test_wiring(jafwak):
    _, b = run(jafwak)
    text = "\n".join(b.last_wiring)
    for line in (
        "Remove the J1 1-3 jumper (BGR_VDD)",
        "Remove the J1 13-15 jumper (OTA_VDD)",
        "PSU OUT2 + -> J1 pin 1 (BGR_VDD)",
        "PSU OUT2 + -> J1 pin 13 (OTA_VDD)",
        "Fit J1 5-7 (VDD from INB)",
        "Fit J1 9-11 (EN_VDD from INB)",
    ):
        assert line in text, line
    assert "Fit J1 1-3" not in text and "Fit J1 13-15" not in text


def test_j1_jumper_left_fitted_stops_the_run(jafwak):
    """OUT2 would fight VIN through INB: its current runs to the limit, so the rails come down."""
    with pytest.raises(InstrumentError, match="J1 1-3"):
        run(jafwak, setup=lambda b: miswire(b, j1_rail_jumpers_out=False))


def test_rails_need_vin_up_first(jafwak):
    with open_bench(jafwak, sim=True) as b, pytest.raises(InstrumentError, match="VIN up"):
        b.split_rails_on()


def test_vin_cannot_drop_under_the_floor_while_the_rails_are_up(jafwak):
    with open_bench(jafwak, sim=True) as b:
        b.confirm_wiring("x", [], ref_probe=("vin", 0), aux_to="rails")
        b.vin_from_psu(1.5)
        b.split_rails_on()
        with pytest.raises(InstrumentError, match="vin_floor"):
            b.vin_from_psu(jafwak.split_rails.vin_floor - 0.1)


def test_shutdown_takes_the_rails_down_before_vin(jafwak):
    calls = []
    with open_bench(jafwak, sim=True) as b:
        b.confirm_wiring("x", [], ref_probe=("vin", 0), aux_to="rails")
        b.vin_from_psu(1.5)
        b.split_rails_on()
        spy(b.rails_out, "set_voltage", calls, "rails")
        spy(b.psu, "output", calls, "psu_output")
    names = [n for n, _, _ in calls]
    assert names.index("rails") < names.index("psu_output")


def test_refused_with_vref_ext(jafwak):
    with open_bench(jafwak, sim=True, vref_external=True) as b, pytest.raises(InstrumentError, match="OUT2"):
        b.vin_from_psu(1.5)
        b.split_rails_on()


def test_config_limits(jafwak):
    data = jafwak.model_dump()
    with pytest.raises(ValidationError, match="split_rails"):
        BenchConfig.model_validate(
            data | {"split_rails": data["split_rails"] | {"voltage": 2.0, "v_max": 2.0}}
        )


def test_cli(tmp_path):
    r = invoke("line-reg", "--sim", "--yes", "-o", str(tmp_path), "--split-rails")
    assert r.exit_code == 0, r.output
    assert "Method B" in r.output


def test_line_reg_refuses_split_rails_with_vref_ext_before_power_up(jafwak):
    with open_bench(jafwak, sim=True, vref_external=True) as b:
        with pytest.raises(InstrumentError, match="--vref ext"):
            line_reg.run(b, jafwak.line_reg, split_rails=True)
        assert not b.sim_model.psu_on
