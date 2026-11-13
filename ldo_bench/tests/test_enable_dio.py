"""--enable dio: the DC and transient tests drive the LDO's enable through the AD2 ([enable]).

The order matters on the board: EN must be low while VIN comes up, high only once VIN is there, and
low again before the supply goes away. The simulator's LDO is only on while the enable DIO is high
(en_pin set), so a run that forgets to enable reads VOUT = 0 and is flagged.
"""

import pytest

from ldo_bench.bench import open_bench
from ldo_bench.instruments.base import InstrumentError
from ldo_bench.procedures import line_reg, load_reg, load_tran


def _watch(bench):
    """Record (psu volts, EN pin level) every time the PSU setpoint changes, and every DIO write."""
    m, en = bench.sim_model, bench.cfg.enable.dio_pin
    log, writes = [], []
    set_v, write = bench.psu.set_voltage, bench.dio.write_pins

    def set_voltage(v):
        set_v(v)
        log.append((v, bool(m.dio.get(en, False))))

    def write_pins(levels):
        write(levels)
        writes.append(dict(levels))

    bench.psu.set_voltage = set_voltage
    bench.dio.write_pins = write_pins
    return log, writes


@pytest.mark.parametrize(
    ("proc", "section", "extra"),
    [(load_reg, "load_reg", ()), (line_reg, "line_reg", ()), (load_tran, "load_tran", ("analysis",))],
)
def test_enable_dio_order_and_valid(jafwak, proc, section, extra):
    with open_bench(jafwak, sim=True, enable_dio=True) as b:
        log, _ = _watch(b)
        res = proc.run(b, getattr(jafwak, section), *(getattr(jafwak, e) for e in extra))
        en_at_end = b.sim_model.dio.get(jafwak.enable.dio_pin)
        assert b.sim_model.en_pin == jafwak.enable.dio_pin  # the sim LDO follows the DIO
    assert not res.issues, res.issues
    rising = [(v, en) for v, en in log if v > 0]
    assert rising and not rising[0][1], "EN was already high when VIN started to rise"
    assert en_at_end is False  # the procedure disabled it before leaving


def test_without_enable_dio_the_wired_enable_stays_off(jafwak):
    """The check the option exists for: with EN wired to the DIO and nothing driving it, VOUT is 0."""
    with open_bench(jafwak, sim=True) as b:
        real = b.confirm_wiring
        b.confirm_wiring = lambda *a, **k: (
            real(*a, **k),
            setattr(b.sim_model, "en_pin", jafwak.enable.dio_pin),
        )
        res = load_reg.run(b, jafwak.load_reg)
    assert any("VOUT out of range" in i for i in res.issues)


def test_enable_dio_adds_the_enable_wiring(jafwak):
    with open_bench(jafwak, sim=True, enable_dio=True) as b:
        lines: list[str] = []
        real = b.confirm_wiring

        def spy(title, ls, **k):
            real(title, ls, **k)
            lines.extend(b.last_wiring)

        b.confirm_wiring = spy
        load_reg.run(b, jafwak.load_reg)
    text = "\n".join(lines)
    assert "J13 pin 12" in text and "J6 7-8" in text


def test_shutdown_drives_enable_low_first(jafwak):
    with open_bench(jafwak, sim=True, enable_dio=True) as b:
        _, writes = _watch(b)
        b.confirm_wiring("x", [], ref_probe=("vin", 0))
        b.ldo_enable(False)
        b.vin_from_psu(1.8)
        b.ldo_enable(True)
        writes.clear()
    en = jafwak.enable.dio_pin
    assert writes and writes[0].get(en) is False  # the first DIO write of the shutdown


def test_enable_dio_refuses_pins_shared_with_the_load_bank(jafwak):
    bad = jafwak.model_copy(update={"enable": jafwak.enable.model_copy(update={"dio_pin": 2})})
    with pytest.raises(InstrumentError, match="load bank"):
        open_bench(bad, sim=True, enable_dio=True)
