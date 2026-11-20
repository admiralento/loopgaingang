"""[board] standard_jumpers: every wiring printout with the chip powered lists J1 and JP2 (JAFWAK).

VDD (J1 5-7) must be up in every test, and VREF_IN needs JP2 unless --vref ext drives it, so a
printout that leaves them out isn't the whole setup. A test that changes one (the DMM in place of
a J1 jumper, Method B, --vref ext) keeps its own line instead of the standard one.
"""

import pytest

from ldo_bench.bench import open_bench
from ldo_bench.procedures import iq, line_reg, line_tran, load_reg, load_tran, loadbank_check, psrr

J1 = [
    "Fit J1 1-3 (BGR_VDD from INB)",
    "Fit J1 5-7 (VDD from INB)",
    "Fit J1 9-11 (EN_VDD from INB)",
    "Fit J1 13-15 (OTA_VDD from INB)",
]
JP2 = "Fit JP2 (the BGR drives VREF_IN)"


def wiring_of(cfg, run, *, vref_ext=False):
    with open_bench(cfg, sim=True, vref_external=vref_ext) as b:
        run(b)
        return list(b.last_wiring)


@pytest.mark.parametrize(
    "name, run",
    [
        ("load-reg", lambda b: load_reg.run(b, b.cfg.load_reg)),
        ("line-reg", lambda b: line_reg.run(b, b.cfg.line_reg)),
        ("load-tran", lambda b: load_tran.run(b, b.cfg.load_tran, b.cfg.analysis)),
        ("line-tran", lambda b: line_tran.run(b, b.cfg.line_tran, b.cfg.analysis)),
        ("psrr", lambda b: psrr.run(b, b.cfg.psrr)),
        ("iq", lambda b: iq.run(b, b.cfg.iq)),
    ],
)
def test_powered_tests_list_j1_and_jp2_once(jafwak, name, run):
    lines = wiring_of(jafwak, run)
    for line in [*J1, JP2]:
        assert lines.count(line) == 1, (name, line, lines)


def test_after_the_input_routing(jafwak):
    lines = wiring_of(jafwak, lambda b: load_reg.run(b, b.cfg.load_reg))
    jp4 = next(k for k, ln in enumerate(lines) if "JP4" in ln)
    assert lines.index(J1[0]) == jp4 + 1


def test_a_jumper_the_test_changes_is_not_also_listed_as_fitted(jafwak):
    lines = wiring_of(
        jafwak, lambda b: iq.run(b, b.cfg.iq.model_copy(update={"method": "series", "rail": "vdd"}))
    )
    assert "Fit J1 5-7 (VDD from INB)" not in lines
    assert any(ln.startswith("Remove the J1 5-7 jumper") for ln in lines)
    split = wiring_of(jafwak, lambda b: line_reg.run(b, b.cfg.line_reg, split_rails=True))
    assert any(ln.startswith("Remove the J1 1-3 jumper") for ln in split)
    assert "Fit J1 1-3 (BGR_VDD from INB)" not in split and "Fit J1 13-15 (OTA_VDD from INB)" not in split
    assert "Fit J1 5-7 (VDD from INB)" in split and JP2 in split


def test_vref_ext_drops_jp2(jafwak):
    lines = wiring_of(jafwak, lambda b: load_reg.run(b, b.cfg.load_reg), vref_ext=True)
    assert JP2 not in lines
    assert any(ln.startswith("Remove JP2") for ln in lines)
    assert all(ln in lines for ln in J1)


def test_chip_disconnected_checks_list_none(jafwak):
    lines = wiring_of(jafwak, lambda b: loadbank_check.run(b, b.cfg.loadbank_check))
    assert not any("J1 " in ln or "JP2" in ln for ln in lines)


def test_demo_board_has_none(cfg):
    if not cfg.dut.name.startswith("TPS"):
        pytest.skip("demo board only")
    lines = wiring_of(cfg, lambda b: load_reg.run(b, b.cfg.load_reg))
    assert not any("J1 " in ln or "JP2" in ln for ln in lines)
