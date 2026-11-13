"""The reworked U2 on a split supply ([buffer]): rails up together with W1 at 0 V, never a floating input.

The simulator's U2 (buffer_split) stays 1.5 V inside both rails, so V- missing leaves VIN stuck
high, a PSU on VIN shows up as VIN following VS_BUFF, and a floating input sends VIN to the top rail.
"""

from pathlib import Path

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from ldo_bench.bench import open_bench
from ldo_bench.cli import app
from ldo_bench.config import BenchConfig
from ldo_bench.instruments.base import InstrumentError
from ldo_bench.procedures import line_tran, psrr

CFG = str(Path(__file__).parents[1] / "configs" / "jafwak.toml")


def _record(b):
    """Log every rail / W1 change as (event, VS_BUFF, V-, W1 DC or None, output on, VIN)."""
    m, ev = b.sim_model, []

    def snap(what):
        ev.append((what, m.psu_v, m.vref_out_v, m.awg_level(), m.psu_on, m.vin))

    for obj, name in ((b.psu, "set_voltage"), (b.psu, "output"), (b.awg, "set_dc"), (b.awg, "off")):
        orig = getattr(obj, name)

        def wrapped(*a, _o=orig, _n=name, **k):
            r = _o(*a, **k)
            snap(_n)
            return r

        setattr(obj, name, wrapped)
    real_aux = b.psu.aux

    def aux(ch):
        out = real_aux(ch)
        orig = out.set_voltage
        out.set_voltage = lambda v: (orig(v), snap("vminus"))[0]
        return out

    b.psu.aux = aux
    return ev


def run_line_tran(cfg, miswire=None):
    with open_bench(cfg, sim=True) as b:
        ev = _record(b)
        real = b.confirm_wiring

        def confirm(*a, **k):
            real(*a, **k)
            if miswire:
                miswire(b.sim_model)

        b.confirm_wiring = confirm
        try:
            res = line_tran.run(b, cfg.line_tran, cfg.analysis)
        finally:
            ev.append(("after", None, None, None, b.sim_model.psu_on, None))
    return res, ev


def test_line_tran_split_supply_order(jafwak):
    res, ev = run_line_tran(jafwak)
    assert not res.issues, res.issues
    rails_up = [e for e in ev if e[4] and (e[1] > 0 or e[2] > 0)]
    assert rails_up, "the rails never came up"
    # W1 drove U2's input whenever either rail was up
    assert all(e[3] is not None for e in rails_up)
    # while the rails ramped with W1 at 0 V, VIN stayed in the window
    lo, hi = jafwak.buffer.vin_window
    assert all(lo <= e[5] <= hi for e in rails_up if e[0] in ("set_voltage", "vminus") and e[3] == 0.0)
    # V- reached -5 V, and W1 went off only after the outputs were off
    assert max(e[2] for e in ev if e[2] is not None) == pytest.approx(jafwak.buffer.v_minus)
    offs = [k for k, e in enumerate(ev) if e[0] == "off"]
    assert offs and all(not ev[k][4] for k in offs)


def test_psu_on_vin_stops_while_the_rail_is_low(jafwak):
    with pytest.raises(InstrumentError, match="connected to VIN") as e:
        run_line_tran(jafwak, miswire=lambda m: setattr(m, "psu_to", "vin"))
    assert "Rails are down" in str(e.value)


def test_missing_negative_rail_stops(jafwak):
    def miswire(m):
        m.vminus_wired = False

    with pytest.raises(InstrumentError, match="pin 4"):
        run_line_tran(jafwak, miswire=miswire)


def test_vin_probe_off_vin_fails_the_proof(jafwak):
    with pytest.raises(InstrumentError, match="isn't proven to come from U2"):
        run_line_tran(jafwak, miswire=lambda m: setattr(m, "ref_probe", ("dio", 0)))


def test_psu_never_far_up_when_it_is_on_vin(jafwak):
    with open_bench(jafwak, sim=True) as b:
        seen = []
        orig = b.psu.set_voltage
        b.psu.set_voltage = lambda v: (seen.append(v), orig(v))[1]
        real = b.confirm_wiring
        b.confirm_wiring = lambda *a, **k: (real(*a, **k), setattr(b.sim_model, "psu_to", "vin"))
        with pytest.raises(InstrumentError):
            line_tran.run(b, jafwak.line_tran, jafwak.analysis)
    hi = jafwak.buffer.vin_window[1]
    assert max(seen) <= hi + jafwak.buffer.ramp_step + 1e-9  # stopped at the first step past the window


def test_w1_off_refused_while_the_rails_are_up(jafwak):
    with open_bench(jafwak, sim=True) as b:
        b.confirm_wiring("t", [], ref_probe=("vin", 0), psu_to="rail")
        b.vin_from_wavegen(0.0)
        b.buffer_rails_on(1.9)
        with pytest.raises(InstrumentError, match="W1 off"):
            b.wavegen_off()
        b.buffer_rails_off()
        assert not b.sim_model.psu_on and not b.sim_model.awg_v
        b.wavegen_off()  # fine once the rails are down


def test_rails_refused_without_w1_at_zero(jafwak):
    with open_bench(jafwak, sim=True) as b:
        b.confirm_wiring("t", [], ref_probe=("vin", 0), psu_to="rail")
        with pytest.raises(InstrumentError, match="DC 0 V"):
            b.buffer_rails_on(1.9)
        b.vin_from_wavegen(0.2)
        with pytest.raises(InstrumentError, match="DC 0 V"):
            b.buffer_rails_on(1.9)
        assert not b.sim_model.psu_on


def test_single_rail_refused_with_a_buffer_section(jafwak):
    with open_bench(jafwak, sim=True) as b, pytest.raises(InstrumentError, match="buffer_rails_on"):
        b.buffer_rail(1.0)


def test_current_limits(jafwak):
    with open_bench(jafwak, sim=True) as b:
        limits = {}
        orig = b.psu.setup
        b.psu.setup = lambda **k: (limits.setdefault("vs_buff", k["current_limit"]), orig(**k))[1]
        real_aux = b.psu.aux

        def aux(ch):
            out = real_aux(ch)
            o = out.setup
            out.setup = lambda **k: (limits.setdefault("vminus", k["current_limit"]), o(**k))[1]
            return out

        b.psu.aux = aux
        b.confirm_wiring("t", [], ref_probe=("vin", 0), psu_to="rail")
        b.vin_from_wavegen(0.0)
        b.buffer_rails_on(1.9)
    assert limits == {"vs_buff": jafwak.buffer.rail_current_limit, "vminus": jafwak.buffer.current_limit}


def test_shutdown_keeps_w1_at_zero_until_the_rails_are_off(jafwak):
    with open_bench(jafwak, sim=True) as b:
        ev = _record(b)
        b.confirm_wiring("t", [], ref_probe=("vin", 0), psu_to="rail")
        b.vin_from_wavegen(0.0)
        b.buffer_rails_on(jafwak.line_tran.psu_voltage)
        b.vin_from_wavegen(1.8)
        ev.clear()
        with pytest.raises(RuntimeError), b:  # any error mid-run
            raise RuntimeError("boom")
    names = [e[0] for e in ev]
    assert names.index("set_dc") < names.index("output") < names.index("off")
    k = names.index("output")
    assert ev[names.index("set_dc")][3] == 0.0 and not ev[k][4]


def test_psrr_on_split_supply(jafwak):
    with open_bench(jafwak, sim=True) as b:
        res = psrr.run(b, jafwak.psrr)
        m = b.sim_model
        assert not m.psu_on and not m.awg_wave and not m.awg_v  # rails down, W1 off last
    assert not res.issues, res.issues


def test_config_checks(jafwak):
    d = jafwak.model_dump()
    d["buffer"]["channel"] = 1  # = psu.channel
    with pytest.raises(ValidationError, match="other output"):
        BenchConfig.model_validate(d)
    d = jafwak.model_dump()
    d["buffer"]["v_minus"] = 1.0  # < buffer_headroom (2 V)
    with pytest.raises(ValidationError, match="v_minus"):
        BenchConfig.model_validate(d)
    d = jafwak.model_dump()
    d["buffer"]["vin_window"] = (0.05, 0.3)
    with pytest.raises(ValidationError, match="vin_window"):
        BenchConfig.model_validate(d)


@pytest.mark.parametrize("cmd", ["line-tran", "psrr"])
def test_cli_refuses_vref_ext_with_the_split_buffer(cmd, tmp_path):
    r = CliRunner().invoke(app, [cmd, "-c", CFG, "--sim", "--vref", "ext", "-o", str(tmp_path)])
    assert r.exit_code == 2 and "[buffer]" in r.output


# --- buffer-check: U2 on its own, chip disconnected ------------------------------
def test_buffer_check_on_sim(jafwak):
    from ldo_bench import plotting
    from ldo_bench.procedures import buffer_check

    with open_bench(jafwak, sim=True) as b:
        res = buffer_check.run(b, jafwak.buffer)
        m = b.sim_model
        assert not m.psu_on and not m.awg_v and not m.awg_wave  # rails down, W1 off last
    assert not res.issues, res.issues
    assert [r["out_v"] for r in res.rows] == pytest.approx(jafwak.buffer.check_levels, abs=0.005)
    assert res.gain == pytest.approx(1.0, abs=0.01)
    assert res.idle_v == pytest.approx(b.sim_model.buffer_offset, abs=0.002)
    assert res.step["rising"]["peak_v"] <= jafwak.buffer.check_peak_max
    assert res.sine["out_amplitude_v"] == pytest.approx(jafwak.buffer.check_sine_amplitude, rel=0.1)
    assert "READY" in res.report()
    assert "10-90 % under 2 us" in res.report()  # the sim's step is instant: a bound, not 0 us
    plotting.close(buffer_check.plot(res, "x"))


def test_buffer_check_flags_a_level_it_cant_reach(jafwak):
    from ldo_bench import plotting
    from ldo_bench.procedures import buffer_check

    with open_bench(jafwak, sim=True) as b:
        real = b.confirm_wiring
        # healthy at the bottom, but the 5 V rail leaves only 1.8 V at the top
        b.confirm_wiring = lambda *a, **k: (real(*a, **k), setattr(b.sim_model, "buffer_dropout", 3.2))
        res = buffer_check.run(b, jafwak.buffer)
    assert any(i.startswith("1.85 V") for i in res.issues)
    assert "NOT READY" in res.report()
    plotting.close(buffer_check.plot(res, "x"))


def test_buffer_check_probe_off_the_output(jafwak):
    from ldo_bench.procedures import buffer_check

    with open_bench(jafwak, sim=True) as b:
        real = b.confirm_wiring
        b.confirm_wiring = lambda *a, **k: (real(*a, **k), setattr(b.sim_model, "ref_probe", ("dio", 0)))
        res = buffer_check.run(b, jafwak.buffer)
    assert any("JP4 pin 1" in i for i in res.issues)


def test_buffer_check_wiring_says_chip_disconnected(jafwak):
    from ldo_bench.procedures import buffer_check

    with open_bench(jafwak, sim=True) as b:
        text = "\n".join(buffer_check.wiring(b, jafwak.buffer))
    assert "JP4 open" in text and "100 Ohm" in text and "U2 pin 4" in text and "VIN_FG (J3)" in text


def test_cli_buffer_check(tmp_path):
    r = CliRunner().invoke(app, ["buffer-check", "-c", CFG, "--sim", "-o", str(tmp_path)])
    assert r.exit_code == 0, r.output
    assert "READY" in r.output
    demo = str(Path(CFG).with_name("demo_board.toml"))
    assert CliRunner().invoke(app, ["buffer-check", "-c", demo, "--sim"]).exit_code == 2


def test_bench_refuses_the_split_supply_with_the_external_reference(jafwak):
    """Both use OUT2: the reference would move U2's V-. Refused even if the CLI check were bypassed."""
    with open_bench(jafwak, sim=True, vref_external=True) as b:
        b.confirm_wiring("t", [], ref_probe=("vin", 0), psu_to="rail")
        b.vin_from_wavegen(0.0)
        with pytest.raises(InstrumentError, match="--vref ext"):
            b.buffer_rails_on(1.9)
        assert not b.sim_model.psu_on
