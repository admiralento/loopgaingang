"""Wiring lines: what every command prints, and asks you to confirm, before it powers anything.

One line per connection: "<from> -> <to>". A scope channel's + and - share a line, and each
channel has its own line; the DMM's V and COM share one too. Jumpers get one line each
("Fit JP4 2-3 ..."). The board points come from [board] (config.BoardConfig): on JAFWAK the
silkscreen label with its connector and pin, elsewhere plain names.
"""

from __future__ import annotations

from ldo_bench.config import BenchConfig


def fit(jumpers: list[str]) -> list[str]:
    """One line per jumper to fit."""
    return [f"Fit {j}" for j in jumpers]


def psu(cfg: BenchConfig, plus: str, minus: str | None = None, *, channel: int | None = None) -> list[str]:
    """The PSU output's + and - (one line each); `channel` defaults to psu.channel."""
    n = cfg.psu.channel if channel is None else channel
    return [f"PSU OUT{n} + -> {plus}", f"PSU OUT{n} - -> {minus or cfg.board.supply_gnd}"]


def psu_on_vin(cfg: BenchConfig) -> list[str]:
    """PSU on the DC input, and the jumpers that route VIN from it."""
    return psu(cfg, cfg.board.vin_dc) + fit(cfg.board.dc_jumpers)


def scope(cfg: BenchConfig, which: str, plus: str, minus: str | None = None, note: str = "") -> str:
    """One scope channel ('vout' or 'ref'): its + and - on one line."""
    ch = cfg.ad2.vout_channel if which == "vout" else cfg.ad2.ref_channel
    return (
        f"Scope {ch}+ -> {plus}, Scope {ch}- -> {minus or cfg.board.probe_gnd}{f' ({note})' if note else ''}"
    )


def scope_vout(cfg: BenchConfig, note: str = "") -> str:
    """Scope on VOUT ([board] vout_probe)."""
    return scope(cfg, "vout", cfg.board.vout_probe, note=note)


def scope_vin(cfg: BenchConfig, note: str = "") -> str:
    """Scope ref channel on VIN ([board] vin_probe)."""
    return scope(cfg, "ref", cfg.board.vin_probe, note=note)


def dmm(plus: str, minus: str, note: str = "") -> str:
    """The DMM's V and COM leads on one line."""
    return f"DMM V -> {plus}, DMM COM -> {minus}{f' ({note})' if note else ''}"


def dmm_vout(cfg: BenchConfig) -> str:
    """DMM on VOUT, the VOUT reference in the DC tests."""
    return dmm(cfg.board.dmm_vout, cfg.board.dmm_gnd, "the DMM is the VOUT reference")


def ad2_gnd(cfg: BenchConfig) -> str:
    """AD2 ground to the board (the DIO and scope are referenced to it)."""
    return f"AD2 GND -> {cfg.board.ad2_gnd}"


def bank_bits(cfg: BenchConfig) -> list[str]:
    """Each load bank gate input, in loadbank.dio_pins order."""
    return cfg.board.bank_bits or [f"load bank B{k}" for k in range(len(cfg.loadbank.dio_pins))]


def load_bank(cfg: BenchConfig, *, supply_given: bool = False) -> list[str]:
    """The load bank's supply (unless `supply_given`: the PSU or another line already powers it),
    its jumpers, then one line per gate: AD2 DIO -> bank bit."""
    b = cfg.board
    lines = [] if supply_given else list(b.bank_supply)
    lines += fit(b.bank_jumpers)
    return [
        *lines,
        *(f"AD2 DIO {p} -> {bit}" for p, bit in zip(cfg.loadbank.dio_pins, bank_bits(cfg), strict=True)),
    ]


def enable_dio(cfg: BenchConfig) -> list[str]:
    """The AD2-driven enable path: jumpers, then one line per DIO / supply."""
    b, e = cfg.board, cfg.enable
    lines = [*fit(b.enable_jumpers), f"AD2 DIO {e.dio_pin} -> {b.enable_in}"]
    if e.dio_pin_n is not None:
        lines.append(f"AD2 DIO {e.dio_pin_n} -> {b.enable_in_n}")
    lines.append(f"AD2 V+ -> {b.enable_vio}" if e.v33 is not None else f"3.3 V supply -> {b.enable_vio}")
    return lines


def enable_static(cfg: BenchConfig) -> list[str]:
    """Jumpers that tie the LDO on (DC tests without --enable dio)."""
    return fit(cfg.board.static_enable_jumpers)


def buffer_supply(cfg: BenchConfig) -> list[str]:
    """The buffer's supply and input (line-tran, psrr): split supply with [buffer], else one rail."""
    b, a = cfg.board, cfg.ad2
    lines = psu(cfg, b.vs_buff)
    if cfg.buffer is not None:
        bu = cfg.buffer
        lines += [
            f"PSU OUT{bu.channel} + -> GND (REVERSED: OUT + to GND)",
            f"PSU OUT{bu.channel} - -> U2 pin 4 (-{bu.v_minus:g} V, 100 nF at the pin)",
        ]
    lines += [f"AD2 W{a.wavegen_channel} -> {b.buffer_input}", *fit(b.buffer_jumpers)]
    return lines


# JAFWAK's J1 "VDD CONNECTIONS": each rail's ON jumper (from INB). Shared by iq and enable-startup.
J1_RAILS = {
    "bgr": ("BGR_VDD", "1-3"),
    "vdd": ("VDD", "5-7"),
    "en": ("EN_VDD", "9-11"),
    "ota": ("OTA_VDD", "13-15"),
}


def j1_rails(skip: str | None = None) -> list[str]:
    """One line per J1 rail jumper (the ON column), leaving out `skip` (the one the DMM replaces).

    VDD (J1 5-7) must be powered in every test: only the VDD ring has ESD clamps, and with VDD off
    a driven pin forward-biases them. Leave it out only where the DMM carries VDD (iq --rail vdd).
    """
    return [f"Fit J1 {pins} ({name} from INB)" for key, (name, pins) in J1_RAILS.items() if key != skip]
