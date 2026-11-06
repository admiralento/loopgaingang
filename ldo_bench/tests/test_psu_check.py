import pytest

from ldo_bench.bench import open_bench
from ldo_bench.instruments.base import InstrumentError
from ldo_bench.procedures import psu_check


def test_sim_psu_within_spec(jafwak):
    with open_bench(jafwak, sim=True) as b:
        res = psu_check.run(b, jafwak.psu_check)
    assert res.model == "E3648A" and res.passed is True
    # sim supply: -1 mV offset + 0.045 % gain on the output
    first = res.rows[0]
    assert first["output_error_v"] == pytest.approx(-1e-3 + 4.5e-4 * first["set_v"], abs=1e-4)
    assert not b.sim_model.psu_on


def test_needs_dmm(jafwak):
    with (
        open_bench(jafwak, sim=True, use_dmm=False) as b,
        pytest.raises(InstrumentError, match="needs the DMM"),
    ):
        psu_check.run(b, jafwak.psu_check)


def test_disconnected_dmm_stops_early(jafwak, monkeypatch):
    with open_bench(jafwak, sim=True) as b:
        monkeypatch.setattr(b.dmm, "read_dcv", lambda: 0.0005)  # open leads
        with pytest.raises(InstrumentError, match="isn't connected"):
            psu_check.run(b, jafwak.psu_check)
    assert not b.sim_model.psu_on  # bench shut the supply off


def test_points_above_psu_limit_rejected(jafwak):
    from ldo_bench.config import BenchConfig

    d = jafwak.model_dump()
    d["psu_check"]["points"] = [1.0, 6.0]
    with pytest.raises(ValueError, match=r"psu_check\.points"):
        BenchConfig.model_validate(d)
