"""Shared test helpers (import with `from helpers import ...`; fixtures are in conftest.py)."""

from pathlib import Path

from typer.testing import CliRunner

CONFIG_DIR = Path(__file__).parents[1] / "configs"


def config_path(board: str = "jafwak") -> str:
    """Path of configs/<board>.toml, as a string for the CLI."""
    return str(CONFIG_DIR / f"{board}.toml")


def miswire(bench, wiring_kw: dict | None = None, **model_fields) -> None:
    """Miswire the simulated bench the way a person could, after the procedure's own wiring step.

    The real confirm_wiring always runs first (so the bench is wired as instructed), then
    `wiring_kw` overrides its keywords (e.g. en_pin=None: EN tied on) and `model_fields` set the
    simulator's wiring state (e.g. dmm_probe="open", psu_to="vin", ref_probe=("dio", 0)).
    """
    real = bench.confirm_wiring

    def confirm(*args, **kwargs):
        real(*args, **(kwargs | (wiring_kw or {})))
        for name, value in model_fields.items():
            setattr(bench.sim_model, name, value)

    bench.confirm_wiring = confirm


def spy(obj, name: str, log: list | None = None, tag: str | None = None) -> list:
    """Wrap obj.name so each call appends (tag or name, args, kwargs) to `log`; returns the log."""
    log = [] if log is None else log
    orig = getattr(obj, name)

    def wrapped(*a, **k):
        log.append((tag or name, a, k))
        return orig(*a, **k)

    setattr(obj, name, wrapped)
    return log


def invoke(*args: str):
    """Run `ldo-bench <args>` in-process; returns the Click result (exit_code, output)."""
    from ldo_bench.cli import app

    return CliRunner().invoke(app, list(args))
