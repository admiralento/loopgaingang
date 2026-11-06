import numpy as np
import pytest

from ldo_bench.bench import open_bench
from ldo_bench.instruments.base import Capture, InstrumentError
from ldo_bench.procedures import load_tran
from ldo_bench.procedures.transient import align_average


def test_align_average_lines_up_triggers():
    base = np.zeros(100)
    a, b = base.copy(), base.copy()
    a[40:] = 1.0
    b[45:] = 1.0  # same step, captured 5 samples later
    avg = align_average([Capture(1e6, a, a, 40), Capture(1e6, b, b, 45)])
    assert avg.trigger_index == 40 and len(avg.ch1) == 40 + 55
    assert avg.ch1[39] == 0.0 and avg.ch1[40] == 1.0  # edges coincide, not smeared


def run(cfg, **upd):
    an = cfg.analysis.model_copy(update={"settle_reference": "post"})
    lt = cfg.load_tran.model_copy(update={"averages": 4, **upd})
    with open_bench(cfg, sim=True) as b:
        res = load_tran.run(b, lt, an)
    return res, b


def test_both_edges_on_sim(cfg):
    res, b = run(cfg)
    m = b.sim_model
    up, down = res.edges
    assert (up.edge, down.edge) == ("rising", "falling")
    assert up.metrics.deviation < 0 < down.metrics.deviation  # dip on load up, overshoot on load down
    # sim: amplitude z_peak * dI, scaled by exp/sin at the peak and the 1 us average (~0.6-0.7)
    di = up.i_to - up.i_from
    assert abs(up.metrics.deviation) == pytest.approx(0.65 * m.z_peak * di, rel=0.2)
    for e in res.edges:
        assert 3e-6 < e.metrics.settling_time < 20e-6 and e.metrics.trustworthy
        assert e.t[e.metrics.step_index] == 0.0
    assert b.loadbank.code == 0  # load released afterwards
    assert set(res.extra_csvs()) == {"waveform_rising.csv", "waveform_falling.csv"}


def test_single_edge_and_auto_trigger_pin(jafwak):
    res, b = run(jafwak, edges=["falling"], trigger_pin=None)
    lt = jafwak.load_tran
    assert [e.edge for e in res.edges] == ["falling"]
    # MSB that changes between the two codes (JAFWAK 1 -> 4: bit 2)
    assert res.trigger_pin == b.loadbank.trigger_pin(lt.code_low, lt.code_high)[0] == 2


def test_trigger_pin_must_change(cfg):
    lt = cfg.load_tran
    still = next(b for b in range(5) if not (lt.code_low ^ lt.code_high) >> b & 1)
    with pytest.raises(InstrumentError, match="never moves"):
        run(cfg, trigger_pin=cfg.loadbank.dio_pins[still])


def test_report_hints_post_when_dc_shift_exceeds_band(cfg):
    lt = cfg.load_tran.model_copy(update={"averages": 2})
    with open_bench(cfg, sim=True) as b:
        res = load_tran.run(b, lt, cfg.analysis)  # 'pre' reference
    assert 'settle_reference = "post"' in res.report()


def test_stops_when_scope_not_on_vout(cfg, monkeypatch):
    """Regression: with scope 1 unconnected the run analysed trigger crosstalk as a load transient."""
    from ldo_bench.instruments.base import DcReading

    lt = cfg.load_tran.model_copy(update={"averages": 2})
    with open_bench(cfg, sim=True) as b:
        floating = {1: DcReading(-0.0004, 2e-4, 8192), 2: DcReading(0.0, 1e-4, 8192)}
        monkeypatch.setattr(b.scope, "measure_dc", lambda *a, **k: floating)
        captured = []
        monkeypatch.setattr(b.scope, "capture", lambda **k: captured.append(1))
        with pytest.raises(InstrumentError, match="isn't VOUT"):
            load_tran.run(b, lt, cfg.analysis)
    assert not captured  # stopped before any transient capture


def test_stops_when_no_step_in_capture(cfg, monkeypatch):
    lt = cfg.load_tran.model_copy(update={"averages": 2})
    with open_bench(cfg, sim=True) as b:
        real = b.scope.capture

        def flat_ref(**k):
            c = real(**k)
            c.ch2[:] = 0.0  # trigger channel shows no edge
            return c

        monkeypatch.setattr(b.scope, "capture", flat_ref)
        with pytest.raises(InstrumentError, match="no step found"):
            load_tran.run(b, lt, cfg.analysis)
