"""Command-line interface: `uv run ldo-bench --help`."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from pydantic import ValidationError

from ldo_bench.config import BenchConfig, load_config

app = typer.Typer(
    no_args_is_help=True, help="LDO characterization on the bench (E3638A + Analog Discovery 2)."
)

ConfigOpt = Annotated[
    Path, typer.Option("--config", "-c", exists=True, dir_okay=False, help="Bench TOML config")
]


@app.callback()
def _root() -> None:
    """LDO characterization on the bench (E3638A + Analog Discovery 2)."""


def _load(path: Path) -> BenchConfig:
    try:
        return load_config(path)
    except ValidationError as e:
        typer.secho(f"Invalid config {path}:\n{e}", fg="red", err=True)
        raise typer.Exit(2) from e


@app.command("check-config")
def check_config(config: ConfigOpt) -> None:
    """Validate a config file without touching any instrument."""
    cfg = _load(config)
    typer.secho(f"{config} OK ({cfg.dut.name})", fg="green")


def main() -> None:
    app()
