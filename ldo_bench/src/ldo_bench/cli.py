"""Command-line interface: `uv run ldo-bench --help`."""

from __future__ import annotations

import logging
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated

import typer
from pydantic import ValidationError

from ldo_bench.bench import Bench, open_bench
from ldo_bench.config import BenchConfig, load_config
from ldo_bench.instruments.base import InstrumentError

app = typer.Typer(no_args_is_help=True, add_completion=False)

ConfigOpt = Annotated[
    Path, typer.Option("--config", "-c", exists=True, dir_okay=False, help="Bench TOML config")
]
SimOpt = Annotated[bool, typer.Option("--sim", help="Use simulated instruments (no hardware)")]
PsuOpt = Annotated[
    str | None, typer.Option("--psu", help="PSU resource, e.g. GPIB0::5::INSTR or prologix:COM5:5")
]
AdOpt = Annotated[str | None, typer.Option("--ad-serial", help="Analog Discovery 2 serial number")]
YesOpt = Annotated[bool, typer.Option("--yes", "-y", help="Don't ask to confirm wiring")]
VerboseOpt = Annotated[bool, typer.Option("--verbose", "-v", help="Debug logging")]


@app.callback()
def _root() -> None:
    """LDO characterization on the bench (E3638A + Analog Discovery 2)."""


def _logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO, format="%(levelname)s %(name)s: %(message)s"
    )
    logging.getLogger("matplotlib").setLevel(logging.WARNING)
    logging.getLogger("pyvisa").setLevel(logging.WARNING)


def _load(path: Path) -> BenchConfig:
    try:
        return load_config(path)
    except ValidationError as e:
        typer.secho(f"Invalid config {path}:\n{e}", fg="red", err=True)
        raise typer.Exit(2) from e


@contextmanager
def _bench(cfg: BenchConfig, sim: bool, psu: str | None, ad: str | None, yes: bool):
    """Open the bench and turn instrument errors and Ctrl-C into clean exits."""
    try:
        with open_bench(
            cfg, sim=sim, psu_resource=psu, ad_serial=ad, assume_yes=yes, confirm=typer.confirm
        ) as bench:
            yield bench
    except InstrumentError as e:
        typer.secho(f"Instrument error: {e}", fg="red", err=True)
        raise typer.Exit(1) from e
    except KeyboardInterrupt as e:
        typer.secho("Aborted; outputs are off.", fg="yellow", err=True)
        raise typer.Exit(130) from e


@app.command("check-config")
def check_config(config: ConfigOpt) -> None:
    """Validate a config file without touching any instrument."""
    cfg = _load(config)
    typer.secho(f"{config} OK ({cfg.dut.name})", fg="green")


@app.command()
def discover(sim: SimOpt = False, verbose: VerboseOpt = False) -> None:
    """List the E3638A supplies and Analog Discovery 2s that are connected."""
    _logging(verbose)
    if sim:
        typer.echo("SIM,E3638A,0,sim  (sim)\nSIM Analog Discovery 2 (SN:SIM000)  (sim)")
        return
    from ldo_bench.instruments.discovery import discover as _discover

    try:
        found = _discover()
    except InstrumentError as e:
        typer.secho(str(e), fg="red", err=True)
        raise typer.Exit(1) from e
    for line in found or ["nothing found"]:
        typer.echo(line)


@app.command()
def smoke(
    config: ConfigOpt,
    vin: Annotated[float | None, typer.Option(help="VIN to apply (default: load_reg.vin)")] = None,
    sim: SimOpt = False,
    psu: PsuOpt = None,
    ad_serial: AdOpt = None,
    yes: YesOpt = False,
    verbose: VerboseOpt = False,
) -> None:
    """Quick sanity check: power up, read VOUT at no load and full load, power down."""
    _logging(verbose)
    cfg = _load(config)
    vin = vin if vin is not None else (cfg.load_reg.vin if cfg.load_reg else cfg.dut.vout_nominal + 0.6)
    with _bench(cfg, sim, psu, ad_serial, yes) as bench:
        _smoke(bench, vin)


def _smoke(bench: Bench, vin: float) -> None:
    a = bench.cfg.ad2
    bench.confirm_wiring(
        "smoke test",
        [
            "PSU -> VIN",
            f"Scope {a.vout_channel}+ -> VOUT, Scope {a.ref_channel}+ -> VIN, both - -> GND",
            f"DIO {bench.cfg.loadbank.dio_pins} -> load bank B0..B{len(bench.cfg.loadbank.dio_pins) - 1}",
        ],
        ref_probe=("vin", 0),
    )
    bench.loadbank.off()
    bench.vin_from_psu(vin)
    typer.echo(f"{'code':>4} {'I_est mA':>9} {'VOUT V':>9} {'VIN V':>8} {'I_psu mA':>9}")
    for code in (0, bench.loadbank.max_code):
        bench.loadbank.set(code)
        bench.sleep(0.05)
        vout, ref = bench.measure_dc(0.1)
        i_est = bench.loadbank.current(code, vout.mean)
        typer.echo(
            f"{code:>4} {i_est * 1e3:>9.2f} {vout.mean:>9.4f} {ref.mean:>8.3f} "
            f"{bench.psu.measure_current() * 1e3:>9.2f}"
        )
    bench.loadbank.off()


def main() -> None:
    app()
