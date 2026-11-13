"""Wiring lines: one connection per line, as printed before every command powers anything.

A scope channel's + and - may share a line (and the DMM's two leads); anything else with two
connections on one line, or two scope channels on one line, is wrong.
"""

import re

import pytest

from ldo_bench.bench import open_bench
from ldo_bench.procedures import (
    buffer_check,
    en_sine,
    iq,
    line_reg,
    line_tran,
    load_reg,
    load_tran,
    psrr,
    startup,
    temp,
    vref_line,
)


def _all_wiring(cfg, bench):
    """Every command's wiring lines for this board (those its config has a section for)."""
    out = {}
    if cfg.load_reg:
        out["load-reg"] = load_reg.wiring(bench)
    if cfg.line_reg:
        out["line-reg"] = line_reg.wiring(bench, cfg.line_reg)
    if cfg.load_tran:
        out["load-tran"] = load_tran.wiring(bench, load_tran.trigger_setup(bench, cfg.load_tran)[0])
    if cfg.line_tran:
        out["line-tran"] = line_tran.wiring(bench, cfg.line_tran)
    if cfg.psrr:
        out["psrr"] = psrr.wiring(bench, cfg.psrr)
    for name, mod, sec in (
        ("iq", iq, cfg.iq),
        ("enable-startup", startup, cfg.startup),
        ("enable-sine", en_sine, cfg.en_sine),
        ("vref-line", vref_line, cfg.vref_line),
        ("temp", temp, cfg.temp),
        ("buffer-check", buffer_check, cfg.buffer),
    ):
        if sec is not None:
            out[name] = mod.wiring(bench, sec)
    return out


def _check_line(line: str) -> str | None:
    """Why a wiring line breaks the one-connection rule, or None."""
    scopes = set(re.findall(r"Scope (\d)[+-]", line))
    if len(scopes) > 1:
        return "two scope channels on one line"
    arrows = line.count("->")
    if arrows <= 1:
        return None
    if arrows == 2 and (scopes or line.startswith(("DMM V ->", "DMM I "))):
        return None  # a probe's + and -, or the DMM's two leads
    return f"{arrows} connections on one line"


@pytest.mark.parametrize("enable_dio", [False, True])
def test_one_connection_per_line(cfg, enable_dio):
    with open_bench(cfg, sim=True, enable_dio=enable_dio) as b:
        commands = _all_wiring(cfg, b)
    assert commands
    bad = [(cmd, ln, why) for cmd, lines in commands.items() for ln in lines if (why := _check_line(ln))]
    assert not bad, bad


def test_jafwak_points_are_board_labels(jafwak):
    with open_bench(jafwak, sim=True) as b:
        text = "\n".join(load_reg.wiring(b))
    for point in (
        "VIN_DC (J7)",
        "J13 pin 4 (OUTA)",
        "J13 pin 1 (INA)",
        "OUTA (TP4)",
        "J13 pin 7 (B0)",
        'J6 1-2 "HI"',
    ):
        assert point in text, point
    assert "J13 pin 3" not in text  # VOUT is probed at OUTA (before R1), not OUTB


def test_iq_and_startup_probe_outa(jafwak):
    with open_bench(jafwak, sim=True) as b:
        for lines in (iq.wiring(b, jafwak.iq), startup.wiring(b, jafwak.startup)):
            vout = next(ln for ln in lines if ln.startswith(f"Scope {jafwak.ad2.vout_channel}+"))
            assert "J13 pin 4 (OUTA)" in vout
