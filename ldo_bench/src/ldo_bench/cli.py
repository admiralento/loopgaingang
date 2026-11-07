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
OutOpt = Annotated[Path, typer.Option("--out", "-o", help="Folder for run results")]
SaveSimOpt = Annotated[
    bool, typer.Option("--save-sim", help="With --sim: save the run folder anyway (marked SIM)")
]
WindowOpt = Annotated[
    float | None,
    typer.Option(
        "--window-ms",
        help="Capture length after scaling the sample rate (ms), e.g. 5 for a 5 ms window. "
        "Default: n_samples / sample_rate from the config (0.82 ms)",
    ),
]
YesOpt = Annotated[bool, typer.Option("--yes", "-y", help="Don't ask to confirm wiring")]
VerboseOpt = Annotated[bool, typer.Option("--verbose", "-v", help="Debug logging")]


@app.callback()
def _root() -> None:
    """LDO characterization on the bench (Agilent E36xxA supply + Analog Discovery 2)."""


class _ConsoleFormatter(logging.Formatter):
    """Plain progress messages; a prefix only for warnings/errors (and everything with -v)."""

    def __init__(self, verbose: bool) -> None:
        super().__init__()
        self.verbose = verbose

    def format(self, record: logging.LogRecord) -> str:
        msg = record.getMessage()
        if self.verbose:
            return f"{record.levelname} {record.name}: {msg}"
        if record.levelno >= logging.WARNING:
            return f"{record.levelname}: {msg}"
        return msg


def _logging(verbose: bool) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(_ConsoleFormatter(verbose))
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO, handlers=[handler], force=True)
    logging.getLogger("matplotlib").setLevel(logging.WARNING)
    logging.getLogger("pyvisa").setLevel(logging.WARNING)


def _load(path: Path) -> BenchConfig:
    try:
        return load_config(path)
    except ValidationError as e:
        typer.secho(f"Invalid config {path}:\n{e}", fg="red", err=True)
        raise typer.Exit(2) from e


@contextmanager
def _bench(cfg: BenchConfig, sim: bool, psu: str | None, ad: str | None, yes: bool, use_dmm: bool = True):
    """Open the bench and turn instrument errors and Ctrl-C into clean exits."""
    try:
        with open_bench(
            cfg,
            sim=sim,
            psu_resource=psu,
            ad_serial=ad,
            assume_yes=yes,
            confirm=typer.confirm,
            use_dmm=use_dmm,
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
    """List the power supplies and Analog Discovery 2s that are connected."""
    _logging(verbose)
    if sim:
        typer.echo("SIM,E3648A,0,sim  (sim)\nSIM Analog Discovery 2 (SN:SIM000)  (sim)")
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
    if bench.is_sim:
        typer.secho(SIM_BANNER, fg="magenta", bold=True)
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
    typer.echo(f"{'code':>4} {'I_est mA':>9} {'VOUT V':>9} {'VIN V':>8} {'V_psu V':>8} {'I_psu mA':>9}")
    for code in (0, bench.loadbank.max_allowed_code):
        bench.loadbank.set(code)
        bench.sleep(0.05)
        vout, ref = bench.measure_dc(0.1)
        i_est = bench.loadbank.current(code, vout.mean)
        typer.echo(
            f"{code:>4} {i_est * 1e3:>9.2f} {vout.mean:>9.4f} {ref.mean:>8.3f} "
            f"{bench.psu.measure_voltage():>8.3f} {bench.psu.measure_current() * 1e3:>9.2f}"
        )
    bench.loadbank.off()


def _section(cfg: BenchConfig, name: str):
    sec = getattr(cfg, name.replace("-", "_"))
    if sec is None:
        typer.secho(f"The config has no [{name.replace('-', '_')}] section.", fg="red", err=True)
        raise typer.Exit(2)
    return sec


def _load_with(cfg: BenchConfig, **sections) -> BenchConfig:
    """Re-validate the config with some sections replaced (keeps the safety checks)."""
    data = cfg.model_dump()
    data.update({k: v.model_dump() for k, v in sections.items()})
    try:
        return BenchConfig.model_validate(data)
    except ValidationError as e:
        typer.secho(f"Invalid option:\n{e}", fg="red", err=True)
        raise typer.Exit(2) from e


def _save(
    bench: Bench, cfg: BenchConfig, config_path: Path, out: Path, module, res, save_sim: bool = False
) -> Path | None:
    """Write data.csv, summary.json, meta.json and plot.png for one run.

    Everything saved is what was measured. Runs with setup problems are saved too, so the plot
    shows what went wrong, but their folder ends in _INVALID and summary.json has valid=false.
    Simulated runs are not saved unless --save-sim (then marked SIM everywhere).
    """
    from ldo_bench import plotting
    from ldo_bench.results import RunDir

    if bench.is_sim and not save_sim:
        return None
    invalid = bool(getattr(res, "issues", []))
    run = RunDir(out, module.NAME, cfg, config_path=config_path, simulated=bench.is_sim, invalid=invalid)
    run.write_csv("data.csv", res.rows)
    for name, rows in getattr(res, "extra_csvs", dict)().items():
        run.write_csv(name, rows)
    run.write_json("summary.json", res.summary())
    run.write_meta(idns=bench.idns(), sim=bench.is_sim)
    figs = {"plot.png": module.plot(res, cfg.dut.name)}
    figs.update(getattr(module, "extra_plots", lambda r, d: {})(res, cfg.dut.name))
    for name, fig in figs.items():
        if bench.is_sim:
            plotting.mark_simulated(fig)
        run.save_fig(fig, name)
        plotting.close(fig)
    return run.path


SIM_BANNER = "SIMULATED (--sim): these numbers come from the simulator, not the board."


def _echo_report(bench: Bench, res) -> None:
    if bench.is_sim:
        typer.secho(SIM_BANNER, fg="magenta", bold=True)
    typer.echo(res.report())


def _saved(path: Path | None, *, fg: str = "green", res=None) -> None:
    issues = list(getattr(res, "issues", []) or [])
    if issues:
        typer.secho("SETUP PROBLEM: this run is NOT a valid measurement.", fg="red", bold=True)
        for i in issues:
            typer.secho(f"  - {i}", fg="red")
    if path is None:
        typer.secho("Simulated run: nothing saved (add --save-sim to keep it, marked SIM).", fg="magenta")
    else:
        typer.secho(f"Saved to {path}", fg="red" if issues else fg)
    if issues:
        raise typer.Exit(3)  # finished, but the data isn't a valid measurement


NoDmmOpt = Annotated[bool, typer.Option("--no-dmm", help="Measure VOUT with the AD2 even if a DMM is set up")]


@app.command("load-tran")
def load_tran(
    config: ConfigOpt,
    low: Annotated[
        str | None, typer.Option("--from", help="Low load: code or current, e.g. 1 or 4mA")
    ] = None,
    high: Annotated[
        str | None, typer.Option("--to", help="High load: code or current, e.g. 17 or 67mA")
    ] = None,
    averages: Annotated[int | None, typer.Option("--avg", help="Captures averaged per edge")] = None,
    window_ms: WindowOpt = None,
    out: OutOpt = Path("results"),
    sim: SimOpt = False,
    save_sim: SaveSimOpt = False,
    psu: PsuOpt = None,
    ad_serial: AdOpt = None,
    yes: YesOpt = False,
    verbose: VerboseOpt = False,
) -> None:
    """Load transient: step the load bank, capture VOUT, report peak and settling time."""
    from ldo_bench.loadbank import LoadBank, parse_load
    from ldo_bench.procedures import load_tran as proc

    _logging(verbose)
    cfg = _load(config)
    lt = _section(cfg, "load-tran")
    update = {}
    try:
        bank = LoadBank(cfg.loadbank, None)
        if low is not None:
            update["code_low"] = parse_load(low, bank, cfg.dut.vout_nominal)
        if high is not None:
            update["code_high"] = parse_load(high, bank, cfg.dut.vout_nominal)
    except ValueError as e:
        typer.secho(str(e), fg="red", err=True)
        raise typer.Exit(2) from e
    if window_ms is not None:
        if window_ms <= 0:
            typer.secho("--window-ms must be positive", fg="red", err=True)
            raise typer.Exit(2)
        update["sample_rate"] = lt.n_samples / (window_ms * 1e-3)
    if averages is not None:
        update["averages"] = averages
    if update:
        cfg = _load_with(cfg, load_tran=lt.model_copy(update=update))
        lt = cfg.load_tran
    with _bench(cfg, sim, psu, ad_serial, yes, use_dmm=False) as bench:
        res = proc.run(bench, lt, cfg.analysis)
        path = _save(bench, cfg, config, out, proc, res, save_sim)
    _echo_report(bench, res)
    _saved(path, fg="green", res=res)


@app.command("line-tran")
def line_tran(
    config: ConfigOpt,
    low: Annotated[float | None, typer.Option("--low", help="VIN low level (V)")] = None,
    high: Annotated[float | None, typer.Option("--high", help="VIN high level (V)")] = None,
    load: Annotated[str | None, typer.Option(help="Load as a code or current, e.g. 4 or 19mA")] = None,
    averages: Annotated[int | None, typer.Option("--avg", help="Captures averaged per edge")] = None,
    window_ms: WindowOpt = None,
    out: OutOpt = Path("results"),
    sim: SimOpt = False,
    save_sim: SaveSimOpt = False,
    psu: PsuOpt = None,
    ad_serial: AdOpt = None,
    yes: YesOpt = False,
    verbose: VerboseOpt = False,
) -> None:
    """Line transient: step VIN with W1 through the BUF634, report VOUT peak and settling time."""
    from ldo_bench.loadbank import LoadBank, parse_load
    from ldo_bench.procedures import line_tran as proc

    _logging(verbose)
    cfg = _load(config)
    lt = _section(cfg, "line-tran")
    update: dict = {}
    if low is not None:
        update["vin_low"] = low
    if high is not None:
        update["vin_high"] = high
    if window_ms is not None:
        if window_ms <= 0:
            typer.secho("--window-ms must be positive", fg="red", err=True)
            raise typer.Exit(2)
        update["sample_rate"] = lt.n_samples / (window_ms * 1e-3)
    if averages is not None:
        update["averages"] = averages
    if load is not None:
        try:
            update["load_code"] = parse_load(load, LoadBank(cfg.loadbank, None), cfg.dut.vout_nominal)
        except ValueError as e:
            typer.secho(str(e), fg="red", err=True)
            raise typer.Exit(2) from e
    if update:
        cfg = _load_with(cfg, line_tran=lt.model_copy(update=update))
        lt = cfg.line_tran
    with _bench(cfg, sim, psu, ad_serial, yes, use_dmm=False) as bench:
        res = proc.run(bench, lt, cfg.analysis)
        path = _save(bench, cfg, config, out, proc, res, save_sim)
    _echo_report(bench, res)
    _saved(path, fg="green", res=res)


@app.command("loadbank-check")
def loadbank_check(
    config: ConfigOpt,
    voltage: Annotated[float | None, typer.Option(help="PSU voltage across the bank (V, max 1.5)")] = None,
    out: OutOpt = Path("results"),
    sim: SimOpt = False,
    save_sim: SaveSimOpt = False,
    psu: PsuOpt = None,
    ad_serial: AdOpt = None,
    yes: YesOpt = False,
    verbose: VerboseOpt = False,
) -> None:
    """Check the AD2 DIO -> load bank wiring with the bank straight across the PSU (no LDO)."""
    from ldo_bench.procedures import loadbank_check as proc

    _logging(verbose)
    cfg = _load(config)
    if voltage is not None:
        cfg = _load_with(cfg, loadbank_check=cfg.loadbank_check.model_copy(update={"voltage": voltage}))
    with _bench(cfg, sim, psu, ad_serial, yes, use_dmm=False) as bench:
        res = proc.run(bench, cfg.loadbank_check)
        path = _save(bench, cfg, config, out, proc, res, save_sim)
    _echo_report(bench, res)
    _saved(path, fg="green" if res.passed else "yellow", res=res)


@app.command("psu-check")
def psu_check(
    config: ConfigOpt,
    out: OutOpt = Path("results"),
    sim: SimOpt = False,
    save_sim: SaveSimOpt = False,
    psu: PsuOpt = None,
    ad_serial: AdOpt = None,
    yes: YesOpt = False,
    verbose: VerboseOpt = False,
) -> None:
    """Check the PSU's output and readback against the DMM (DMM across the PSU, nothing else)."""
    from ldo_bench.procedures import psu_check as proc

    _logging(verbose)
    cfg = _load(config)
    with _bench(cfg, sim, psu, ad_serial, yes) as bench:
        res = proc.run(bench, cfg.psu_check)
        path = _save(bench, cfg, config, out, proc, res, save_sim)
    _echo_report(bench, res)
    _saved(path, fg="green" if res.passed is not False else "yellow", res=res)


@app.command("line-reg")
def line_reg(
    config: ConfigOpt,
    load: Annotated[str | None, typer.Option(help="Load as a code or current, e.g. 8 or 30mA")] = None,
    vin_from: Annotated[
        str | None, typer.Option("--vin-from", help="VIN measurement: 'ad2' (scope at the board) or 'psu'")
    ] = None,
    out: OutOpt = Path("results"),
    sim: SimOpt = False,
    save_sim: SaveSimOpt = False,
    psu: PsuOpt = None,
    ad_serial: AdOpt = None,
    no_dmm: NoDmmOpt = False,
    yes: YesOpt = False,
    verbose: VerboseOpt = False,
) -> None:
    """Line regulation: sweep VIN at a fixed load; reports dVOUT/dVIN and the dropout knee."""
    from ldo_bench.loadbank import LoadBank, parse_load
    from ldo_bench.procedures import line_reg as proc

    _logging(verbose)
    cfg = _load(config)
    lr = _section(cfg, "line-reg")
    if load is not None:
        try:
            code = parse_load(load, LoadBank(cfg.loadbank, None), cfg.dut.vout_nominal)
        except ValueError as e:
            typer.secho(str(e), fg="red", err=True)
            raise typer.Exit(2) from e
        cfg = _load_with(cfg, line_reg=lr.model_copy(update={"load_code": code}))
        lr = cfg.line_reg
    if vin_from is not None:
        if vin_from not in ("ad2", "psu"):
            typer.secho("--vin-from must be 'ad2' or 'psu'", fg="red", err=True)
            raise typer.Exit(2)
        cfg = _load_with(cfg, line_reg=lr.model_copy(update={"vin_source": vin_from}))
        lr = cfg.line_reg
    with _bench(cfg, sim, psu, ad_serial, yes, use_dmm=not no_dmm) as bench:
        res = proc.run(bench, lr)
        path = _save(bench, cfg, config, out, proc, res, save_sim)
    _echo_report(bench, res)
    _saved(path, fg="green", res=res)


@app.command("load-reg")
def load_reg(
    config: ConfigOpt,
    vin: Annotated[float | None, typer.Option(help="Override load_reg.vin (V)")] = None,
    out: OutOpt = Path("results"),
    sim: SimOpt = False,
    save_sim: SaveSimOpt = False,
    psu: PsuOpt = None,
    ad_serial: AdOpt = None,
    no_dmm: NoDmmOpt = False,
    yes: YesOpt = False,
    verbose: VerboseOpt = False,
) -> None:
    """Load regulation: step the load bank at fixed VIN, record VOUT vs load current."""
    from ldo_bench.procedures import load_reg as proc

    _logging(verbose)
    cfg = _load(config)
    lr = _section(cfg, "load-reg")
    if vin is not None:
        cfg = _load_with(cfg, load_reg=lr.model_copy(update={"vin": vin}))
        lr = cfg.load_reg
    with _bench(cfg, sim, psu, ad_serial, yes, use_dmm=not no_dmm) as bench:
        res = proc.run(bench, lr)
        path = _save(bench, cfg, config, out, proc, res, save_sim)
    _echo_report(bench, res)
    _saved(path, fg="green", res=res)


def main() -> None:
    app()
