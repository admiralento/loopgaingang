"""Command-line interface: `uv run ldo-bench --help`.

One command per test, plus check-config, discover, smoke and compare (two saved runs side by
side). Every test command does the same five things:

  1. Load and validate the config (`_load`). Nothing is touched if it's invalid.
  2. Apply its options to the test's section and re-validate the whole config (`_override`), so
     an option goes through exactly the same safety checks as the TOML file.
  3. Open the bench (`_bench`), which shuts everything down on the way out.
  4. Run the procedure: `procedures.<test>.run(bench, section, ...)` returns a result object.
  5. Save the run folder (`_save`, see results.py) and print the report (`_run` does 3-5).

Set LDO_BENCH_CONFIG to a config file to leave out `-c` on every command.

Exit codes:
  0    success
  1    instrument error (not found, refused a command, a safety limit tripped)
  2    bad config file or command-line option
  3    the test finished but found a setup problem (data saved, folder marked _INVALID)
  130  Ctrl-C, or wiring not confirmed (outputs are off)
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from enum import StrEnum
from pathlib import Path
from typing import Annotated, NoReturn, get_args

import typer
from pydantic import ValidationError

from ldo_bench import wiring
from ldo_bench.bench import Bench, open_bench
from ldo_bench.config import BenchConfig, EnSineConfig, IqConfig, LineRegConfig, StartupConfig, load_config
from ldo_bench.instruments.base import InstrumentError

app = typer.Typer(no_args_is_help=True, add_completion=False)


def _choices(name: str, model, field: str) -> type[StrEnum]:
    """An option's choices, taken from the config field's Literal (one list, shown in --help)."""
    values = get_args(model.model_fields[field].annotation)
    return StrEnum(name, {v: v for v in values})


Vref = StrEnum("Vref", {"bgr": "bgr", "ext": "ext"})
Enable = StrEnum("Enable", {"static": "static", "dio": "dio"})
Shape = _choices("Shape", EnSineConfig, "shape")
Method = _choices("Method", IqConfig, "method")
Rail = _choices("Rail", IqConfig, "rail")
Ch2 = _choices("Ch2", StartupConfig, "ch2")
VinFrom = _choices("VinFrom", LineRegConfig, "vin_source")

# Options shared by several commands.
ConfigOpt = Annotated[
    Path,
    typer.Option(
        "--config", "-c", exists=True, dir_okay=False, envvar="LDO_BENCH_CONFIG", help="Bench TOML config"
    ),
]
SimOpt = Annotated[bool, typer.Option("--sim", help="Use simulated instruments (no hardware)")]
SaveSimOpt = Annotated[
    bool, typer.Option("--save-sim", help="With --sim: save the run folder anyway (marked SIM)")
]
PsuOpt = Annotated[
    str | None, typer.Option("--psu", help="PSU resource, e.g. GPIB0::5::INSTR or prologix:COM5:5")
]
AdOpt = Annotated[str | None, typer.Option("--ad-serial", help="Analog Discovery 2 serial number")]
OutOpt = Annotated[Path, typer.Option("--out", "-o", help="Folder for run results")]
YesOpt = Annotated[bool, typer.Option("--yes", "-y", help="Don't ask to confirm wiring")]
VerboseOpt = Annotated[bool, typer.Option("--verbose", "-v", help="Debug logging")]
NoDmmOpt = Annotated[bool, typer.Option("--no-dmm", help="Don't use the DMM even if one is set up")]
NoXScopeOpt = Annotated[
    bool, typer.Option("--no-xscope", help="Don't use the [xscope] cross-check scope for this run")
]
VrefOpt = Annotated[
    Vref,
    typer.Option(
        help="Reference on VREF_IN: bgr (the on-chip BGR, JP2 fitted) or ext ([vref]: PSU output on "
        "VREF_IN, JP2 removed; asks you to confirm JP2 every run)",
    ),
]
EnableOpt = Annotated[
    Enable,
    typer.Option(
        help="LDO enable: static (tied on with jumpers, e.g. J6 1-2) or dio (driven from the AD2 "
        "through [enable]; held low while VIN rises)",
    ),
]
LoadOpt = Annotated[str | None, typer.Option(help="Load as a code or current, e.g. 4 or 19mA")]
AvgOpt = Annotated[int | None, typer.Option("--avg", help="Captures averaged per edge")]
WindowOpt = Annotated[
    float | None,
    typer.Option("--window-ms", help="Capture length (ms), e.g. 5; the sample rate is scaled to fit"),
]
PlotUsOpt = Annotated[
    float | None,
    typer.Option("--plot-us", help="Zoomed plot: µs shown after the step (e.g. 100 = a scope at 10 µs/div)"),
]


@app.callback()
def _root() -> None:
    """LDO characterization on the bench (Agilent E36xxA supply + Analog Discovery 2)."""


# --- helpers ------------------------------------------------------------------
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
    """Send log messages to the console: INFO and up, or DEBUG with -v. Quietens noisy libraries."""
    handler = logging.StreamHandler()
    handler.setFormatter(_ConsoleFormatter(verbose))
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO, handlers=[handler], force=True)
    logging.getLogger("matplotlib").setLevel(logging.WARNING)
    logging.getLogger("pyvisa").setLevel(logging.WARNING)


def _fail(message: str, code: int = 2) -> NoReturn:
    """Print `message` in red and exit with `code` (2: a bad config or option)."""
    typer.secho(message, fg="red", err=True)
    raise typer.Exit(code)


def _load(path: Path, verbose: bool = False) -> BenchConfig:
    """Set up logging, then load and validate the config; on any validation error print it and exit 2."""
    _logging(verbose)
    try:
        return load_config(path)
    except ValidationError as e:
        _fail(f"Invalid config {path}:\n{e}")


def _section(cfg: BenchConfig, name: str):
    """The config section `name` (e.g. "load_tran"); exit 2 if the file has none."""
    sec = getattr(cfg, name)
    if sec is None:
        _fail(f"The config has no [{name}] section.")
    return sec


def _load_with(cfg: BenchConfig, **sections) -> BenchConfig:
    """Re-validate the whole config with some sections replaced (keeps every cross-check). Exit 2 if invalid."""
    data = cfg.model_dump() | {k: v.model_dump() for k, v in sections.items()}
    try:
        return BenchConfig.model_validate(data)
    except ValidationError as e:
        _fail(f"Invalid option:\n{e}")


def _override(cfg: BenchConfig, name: str, **updates):
    """(config, section) with the options given (not None) applied to [name] and everything re-validated.

    The section is validated on its own first (a clearer error), then the whole config, so an option
    meets exactly the checks the TOML file does. Exit 2 if either fails.
    """
    sec = _section(cfg, name)
    updates = {k: v for k, v in updates.items() if v is not None}
    if not updates:
        return cfg, sec
    try:
        new = type(sec).model_validate(sec.model_dump() | updates)
    except ValidationError as e:
        _fail(f"Invalid option:\n{e}")
    cfg = _load_with(cfg, **{name: new})
    return cfg, getattr(cfg, name)


def _load_code(cfg: BenchConfig, spec: str | None) -> int | None:
    """A --load / --from / --to value (a code, or a current like 19mA) as a load code; exit 2 if bad."""
    if spec is None:
        return None
    from ldo_bench.loadbank import LoadBank, parse_load

    try:
        return parse_load(spec, LoadBank(cfg.loadbank, None), cfg.dut.vout_nominal)
    except ValueError as e:
        _fail(str(e))


def _rate_for_window(window_ms: float | None, n_samples: int) -> float | None:
    """The sample rate (Hz) that fits `n_samples` into --window-ms; exit 2 unless it's positive."""
    if window_ms is None:
        return None
    if window_ms <= 0:
        _fail("--window-ms must be positive")
    return n_samples / (window_ms * 1e-3)


def _modes(
    cfg: BenchConfig, vref: Vref = Vref.bgr, enable: Enable = Enable.static, *, split_buffer: bool = False
) -> dict:
    """Check --vref / --enable against the config (exit 2) and return them as _bench keywords.

    split_buffer: the test powers U2's split supply (line-tran, psrr), whose negative rail is the
    PSU output the external reference also needs. The Bench refuses the same combinations.
    """
    if vref == Vref.ext:
        if cfg.vref is None:
            _fail("--vref ext needs a [vref] section in the config")
        if split_buffer and cfg.buffer is not None and cfg.vref.psu_channel == cfg.buffer.channel:
            _fail(
                f"--vref ext can't run with the split-supply buffer ([buffer]): both use PSU OUT{cfg.buffer.channel}"
            )
    if enable == Enable.dio and (clash := cfg.enable_pin_clash()):
        _fail(f"--enable dio: enable DIO pins {sorted(clash)} are also load bank pins")
    return {"vref": vref.value, "enable": enable.value}


def _need_dmm(cfg: BenchConfig, what: str) -> None:
    """Exit 2 before anything opens if a test that reads with the DMM has none configured."""
    if cfg.dmm is None or not cfg.dmm.enabled:
        _fail(f"{what} reads with the DMM: the config's [dmm] must be enabled")


@contextmanager
def _bench(
    cfg: BenchConfig,
    sim: bool,
    psu: str | None,
    ad: str | None,
    yes: bool,
    use_dmm: bool = True,
    use_xscope: bool = False,
    vref: str = "bgr",
    enable: str = "static",
) -> Iterator[Bench]:
    """Open the bench and turn instrument errors and Ctrl-C into clean exits.

    InstrumentError -> exit 1; KeyboardInterrupt (Ctrl-C or wiring not confirmed) -> exit 130.
    Bench.shutdown has already run by the time either is reported; a failed shutdown step is
    printed in red whatever ended the run.
    """
    bench = None
    try:
        with open_bench(
            cfg,
            sim=sim,
            psu_resource=psu,
            ad_serial=ad,
            assume_yes=yes,
            confirm=typer.confirm,
            use_dmm=use_dmm,
            use_xscope=use_xscope,
            vref_external=vref == "ext",
            enable_dio=enable == "dio",
        ) as bench:
            yield bench
    except InstrumentError as e:
        typer.secho(f"Instrument error: {e}", fg="red", err=True)
        _shutdown_failed(bench)
        raise typer.Exit(1) from e
    except KeyboardInterrupt as e:
        if not _shutdown_failed(bench):
            typer.secho("Aborted; outputs are off.", fg="yellow", err=True)
        raise typer.Exit(130) from e
    except Exception:  # a bug or an unexpected error: shutdown has run, but say if any step of it failed
        _shutdown_failed(bench)
        raise
    if _shutdown_failed(bench):
        raise typer.Exit(1)


def _shutdown_failed(bench: Bench | None) -> bool:
    """Print any shutdown step that failed (in red); True if one did. The bench may still be powered."""
    errors = getattr(bench, "shutdown_errors", None) or []
    if errors:
        typer.secho(
            "SHUTDOWN FAILED: check the bench by hand, the supply or wavegen may still be on:",
            fg="red",
            err=True,
        )
        for e in errors:
            typer.secho(f"  - {e}", fg="red", err=True)
    return bool(errors)


@contextmanager
def _partial_on_error(run) -> Iterator[None]:
    """If saving a run folder fails part way, rename it _PARTIAL so it can't pass for a complete run."""
    try:
        yield
    except BaseException:
        partial = run.path.with_name(run.path.name + "_PARTIAL")
        run.path.rename(partial)
        typer.secho(f"Saving failed part way: {partial} is incomplete", fg="red", err=True)
        raise


def _write_figs(run, figs: dict, simulated: bool) -> None:
    """Save each figure into the run folder (SIM banner if simulated), close it, update latest.png."""
    from ldo_bench import plotting

    for name, fig in figs.items():
        if simulated:
            plotting.mark_simulated(fig)
        run.save_fig(fig, name)
        plotting.close(fig)
    run.publish_latest()  # results/latest.png etc.: keep one open in the editor, it updates


def _save(
    bench: Bench, cfg: BenchConfig, config_path: Path, out: Path, module, res, save_sim: bool = False
) -> Path | None:
    """Write data.csv, summary.json, meta.json and the plots for one run; return the folder (None if not saved).

    Everything saved is what was measured. A run with setup problems is saved too, so the plot shows
    what went wrong, but its folder ends in _INVALID and summary.json has valid=false. A simulated
    run is saved only with --save-sim (then marked SIM everywhere). `module` is the procedure module
    (NAME, plot(), optional extra_plots()); `res` its result (rows, summary(), issues, extra_csvs()).
    """
    from ldo_bench.results import RunDir

    if bench.is_sim and not save_sim:
        return None
    run = RunDir(
        out,
        module.NAME,
        cfg,
        config_path=config_path,
        simulated=bench.is_sim,
        invalid=bool(getattr(res, "issues", [])),
        tag="extref" if bench.vref_external else "",
    )
    with _partial_on_error(run):
        run.write_csv("data.csv", res.rows)
        for name, rows in getattr(res, "extra_csvs", dict)().items():
            run.write_csv(name, rows)
        # which reference VOUT followed: the BGR, or the external one and its proof (compare uses it)
        run.write_json("summary.json", {**res.summary(), "vref": bench.vref_info()})
        run.write_meta(
            idns=bench.idns(),
            sim=bench.is_sim,
            extra={"vref": bench.vref_info(), "enable": "dio" if bench.enable_dio else "static"},
        )
        figs = {"plot.png": module.plot(res, cfg.dut.name)}
        figs.update(getattr(module, "extra_plots", lambda r, d: {})(res, cfg.dut.name))
        _write_figs(run, figs, bench.is_sim)
    return run.path


SIM_BANNER = "SIMULATED (--sim): these numbers come from the simulator, not the board."


def _echo_report(bench: Bench, res) -> None:
    """Print the procedure's text report, under the SIM banner if simulated."""
    if bench.is_sim:
        typer.secho(SIM_BANNER, fg="magenta", bold=True)
    typer.echo(res.report())


def _saved(path: Path | None, *, fg: str = "green", res=None) -> None:
    """Print where the run was saved and any setup problems; exit 3 if there were any.

    `fg` is the colour of the "Saved to" line when there are no issues (e.g. yellow for a failed check).
    """
    issues = list(getattr(res, "issues", []) or [])
    if issues:
        typer.secho("SETUP PROBLEM: this run is NOT a valid measurement.", fg="red", bold=True)
        for i in issues:
            typer.secho(f"  - {i}", fg="red")
    if path is None:
        typer.secho("Simulated run: nothing saved (add --save-sim to keep it, marked SIM).", fg="magenta")
    else:
        typer.secho(f"Saved to {path.resolve()}", fg="red" if issues else fg)
    if issues:
        raise typer.Exit(3)  # finished, but the data isn't a valid measurement


def _run(
    cfg: BenchConfig,
    config: Path,
    module,
    run: Callable[[Bench], object],
    *,
    out: Path,
    sim: bool,
    save_sim: bool,
    psu: str | None,
    ad: str | None,
    yes: bool,
    passed: Callable[[object], bool] | None = None,
    **bench_kw,
) -> None:
    """Open the bench, run(bench), save the run, print the report and where it went (exit 3 on issues).

    passed(res): False colours the "Saved to" line yellow (a bench check that didn't pass).
    """
    with _bench(cfg, sim, psu, ad, yes, **bench_kw) as bench:
        res = run(bench)
        path = _save(bench, cfg, config, out, module, res, save_sim)
    _echo_report(bench, res)
    _saved(path, fg="green" if passed is None or passed(res) else "yellow", res=res)


# --- bench checks ---------------------------------------------------------------
@app.command("check-config")
def check_config(config: ConfigOpt) -> None:
    """Validate a config file without touching any instrument."""
    cfg = _load(config)
    typer.secho(f"{config} OK ({cfg.dut.name})", fg="green")


@app.command()
def discover(
    config: Annotated[
        Path | None,
        typer.Option("--config", "-c", exists=True, dir_okay=False, help="Use this config's RS-232 settings"),
    ] = None,
    sim: SimOpt = False,
    verbose: VerboseOpt = False,
) -> None:
    """List every instrument reachable over GPIB (VISA / UsbGpib / Prologix), USB and RS-232."""
    _logging(verbose)
    if sim:
        typer.echo("SIM,E3648A,0,sim  (sim)\nSIM Analog Discovery 2 (SN:SIM000)  (sim)")
        return
    from ldo_bench.instruments.discovery import discover as _discover

    cfg = _load(config, verbose) if config else None
    try:
        found = _discover(cfg=cfg)
    except InstrumentError as e:
        _fail(str(e), 1)
    for line in found or ["nothing found"]:
        typer.echo(line)


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

    cfg = _load(config, verbose)
    _run(
        cfg,
        config,
        proc,
        lambda b: proc.run(b, cfg.psu_check),
        out=out,
        sim=sim,
        save_sim=save_sim,
        psu=psu,
        ad=ad_serial,
        yes=yes,
        passed=lambda r: r.passed is not False,
    )


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

    cfg, lc = _override(_load(config, verbose), "loadbank_check", voltage=voltage)
    _run(
        cfg,
        config,
        proc,
        lambda b: proc.run(b, lc),
        out=out,
        sim=sim,
        save_sim=save_sim,
        psu=psu,
        ad=ad_serial,
        yes=yes,
        passed=lambda r: r.passed,
        use_dmm=False,
    )


@app.command()
def smoke(
    config: ConfigOpt,
    vin: Annotated[float | None, typer.Option(help="VIN to apply (default: load_reg.vin)")] = None,
    sim: SimOpt = False,
    psu: PsuOpt = None,
    ad_serial: AdOpt = None,
    vref: VrefOpt = Vref.bgr,
    enable: EnableOpt = Enable.static,
    yes: YesOpt = False,
    verbose: VerboseOpt = False,
) -> None:
    """Quick sanity check: power up, read VOUT at no load and full load, power down. Saves nothing."""
    cfg = _load(config, verbose)
    modes = _modes(cfg, vref, enable)
    # --vin isn't re-validated here; Bench.vin_from_psu still refuses anything above the ceilings.
    vin = vin if vin is not None else (cfg.load_reg.vin if cfg.load_reg else cfg.dut.vout_nominal + 0.6)
    with _bench(cfg, sim, psu, ad_serial, yes, **modes) as bench:
        _smoke(bench, vin)


def _smoke(bench: Bench, vin: float) -> None:
    """Body of `smoke`: VIN on, print a table at code 0 and at the heaviest allowed code, load off."""
    if bench.is_sim:
        typer.secho(SIM_BANNER, fg="magenta", bold=True)
    cfg = bench.cfg
    bench.confirm_wiring(
        "smoke test",
        [
            *wiring.psu_on_vin(cfg),
            wiring.scope_vout(cfg),
            wiring.scope_vin(cfg),
            *([] if bench.enable_dio else wiring.enable_static(cfg)),
            *wiring.load_bank(cfg),
            wiring.ad2_gnd(cfg),
        ],
        ref_probe=("vin", 0),
    )
    bench.ldo_enable(False)  # --enable dio only: EN held low while VIN comes up
    bench.loadbank.off()
    bench.vin_from_psu(vin)
    bench.ldo_enable(True)
    bench.reference_on()  # --vref ext only
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
    bench.ldo_enable(False)
    bench.loadbank.off()


@app.command("buffer-check")
def buffer_check(
    config: ConfigOpt,
    out: OutOpt = Path("results"),
    sim: SimOpt = False,
    save_sim: SaveSimOpt = False,
    psu: PsuOpt = None,
    ad_serial: AdOpt = None,
    yes: YesOpt = False,
    verbose: VerboseOpt = False,
) -> None:
    """U2 buffer on its own, chip disconnected (JP4 open, 100 Ohm dummy): split supply, levels, step, sine."""
    from ldo_bench.procedures import buffer_check as proc

    cfg = _load(config, verbose)
    if cfg.buffer is None:
        _fail("buffer-check needs a [buffer] section (the split-supply U2)")
    _run(
        cfg,
        config,
        proc,
        lambda b: proc.run(b, cfg.buffer),
        out=out,
        sim=sim,
        save_sim=save_sim,
        psu=psu,
        ad=ad_serial,
        yes=yes,
        use_dmm=False,
    )


# --- DC tests -------------------------------------------------------------------
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
    vref: VrefOpt = Vref.bgr,
    enable: EnableOpt = Enable.static,
    yes: YesOpt = False,
    verbose: VerboseOpt = False,
) -> None:
    """Load regulation: step the load bank at fixed VIN, record VOUT vs load current."""
    from ldo_bench.procedures import load_reg as proc

    cfg, lr = _override(_load(config, verbose), "load_reg", vin=vin)
    _run(
        cfg,
        config,
        proc,
        lambda b: proc.run(b, lr),
        out=out,
        sim=sim,
        save_sim=save_sim,
        psu=psu,
        ad=ad_serial,
        yes=yes,
        use_dmm=not no_dmm,
        **_modes(cfg, vref, enable),
    )


@app.command("line-reg")
def line_reg(
    config: ConfigOpt,
    load: LoadOpt = None,
    vin_from: Annotated[
        VinFrom | None, typer.Option(help="VIN measurement: ad2 (scope at the board) or psu (its readback)")
    ] = None,
    out: OutOpt = Path("results"),
    sim: SimOpt = False,
    save_sim: SaveSimOpt = False,
    psu: PsuOpt = None,
    ad_serial: AdOpt = None,
    no_dmm: NoDmmOpt = False,
    vref: VrefOpt = Vref.bgr,
    enable: EnableOpt = Enable.static,
    yes: YesOpt = False,
    verbose: VerboseOpt = False,
) -> None:
    """Line regulation: sweep VIN at a fixed load; reports dVOUT/dVIN and the dropout knee."""
    from ldo_bench.procedures import line_reg as proc

    cfg = _load(config, verbose)
    cfg, lr = _override(
        cfg, "line_reg", load_code=_load_code(cfg, load), vin_source=vin_from and vin_from.value
    )
    _run(
        cfg,
        config,
        proc,
        lambda b: proc.run(b, lr),
        out=out,
        sim=sim,
        save_sim=save_sim,
        psu=psu,
        ad=ad_serial,
        yes=yes,
        use_dmm=not no_dmm,
        **_modes(cfg, vref, enable),
    )


@app.command("vref-line")
def vref_line(
    config: ConfigOpt,
    high: Annotated[float | None, typer.Option(help="First (highest) VIN (V)")] = None,
    low: Annotated[float | None, typer.Option(help="Last (lowest) VIN (V)")] = None,
    step: Annotated[float | None, typer.Option(help="VIN step (V)")] = None,
    vin_from: Annotated[
        VinFrom | None, typer.Option(help="VIN measurement: ad2 (scope at the board) or psu (its readback)")
    ] = None,
    out: OutOpt = Path("results"),
    sim: SimOpt = False,
    save_sim: SaveSimOpt = False,
    psu: PsuOpt = None,
    ad_serial: AdOpt = None,
    enable: EnableOpt = Enable.static,
    yes: YesOpt = False,
    verbose: VerboseOpt = False,
) -> None:
    """VREF vs VIN: step VIN down with the LDO on, DMM on VREF (TP3); BGR line sensitivity and minimum VIN."""
    from ldo_bench.procedures import vref_line as proc

    cfg, vl = _override(
        _load(config, verbose),
        "vref_line",
        vin_high=high,
        vin_low=low,
        vin_step=step,
        vin_source=vin_from and vin_from.value,
    )
    _need_dmm(cfg, "vref-line")
    _run(
        cfg,
        config,
        proc,
        lambda b: proc.run(b, vl),
        out=out,
        sim=sim,
        save_sim=save_sim,
        psu=psu,
        ad=ad_serial,
        yes=yes,
        **_modes(cfg, enable=enable),
    )


@app.command("iq")
def iq(
    config: ConfigOpt,
    method: Annotated[
        Method | None,
        typer.Option(help="shunt (DMM volts across R13) or series (DMM amps in place of a J1 jumper)"),
    ] = None,
    rail: Annotated[Rail | None, typer.Option(help="series: the J1 rail the DMM replaces, or total")] = None,
    vin: Annotated[float | None, typer.Option(help="One VIN point instead of the sweep (V)")] = None,
    states: Annotated[
        str | None, typer.Option(help="Comma list: enabled, disabled (e.g. 'enabled' on its own)")
    ] = None,
    out: OutOpt = Path("results"),
    sim: SimOpt = False,
    save_sim: SaveSimOpt = False,
    psu: PsuOpt = None,
    ad_serial: AdOpt = None,
    vref: VrefOpt = Vref.bgr,
    yes: YesOpt = False,
    verbose: VerboseOpt = False,
) -> None:
    """Quiescent (enabled) and shutdown (disabled) current vs VIN, no load, measured by the DMM."""
    from ldo_bench.procedures import iq as proc

    cfg = _load(config, verbose)
    if method == Method.shunt and rail is None:
        rail = Rail.total  # the shunt measures the total input current
    cfg, sec = _override(
        cfg,
        "iq",
        method=method and method.value,
        rail=rail and rail.value,
        vin_start=vin,
        vin_stop=vin,
        states=None if states is None else [s.strip() for s in states.split(",") if s.strip()],
    )
    _need_dmm(cfg, "iq")
    _run(
        cfg,
        config,
        proc,
        lambda b: proc.run(b, sec),
        out=out,
        sim=sim,
        save_sim=save_sim,
        psu=psu,
        ad=ad_serial,
        yes=yes,
        **_modes(cfg, vref),
    )


# --- transient tests ------------------------------------------------------------
@app.command("load-tran")
def load_tran(
    config: ConfigOpt,
    low: Annotated[
        str | None, typer.Option("--from", help="Low load: code or current, e.g. 1 or 4mA")
    ] = None,
    high: Annotated[
        str | None, typer.Option("--to", help="High load: code or current, e.g. 17 or 67mA")
    ] = None,
    averages: AvgOpt = None,
    window_ms: WindowOpt = None,
    plot_us: PlotUsOpt = None,
    out: OutOpt = Path("results"),
    sim: SimOpt = False,
    save_sim: SaveSimOpt = False,
    psu: PsuOpt = None,
    ad_serial: AdOpt = None,
    no_xscope: NoXScopeOpt = False,
    vref: VrefOpt = Vref.bgr,
    enable: EnableOpt = Enable.static,
    yes: YesOpt = False,
    verbose: VerboseOpt = False,
) -> None:
    """Load transient: step the load bank, capture VOUT, report peak and settling time."""
    from ldo_bench.procedures import load_tran as proc

    cfg = _load(config, verbose)
    n = _section(cfg, "load_tran").n_samples
    cfg, lt = _override(
        cfg,
        "load_tran",
        code_low=_load_code(cfg, low),
        code_high=_load_code(cfg, high),
        averages=averages,
        sample_rate=_rate_for_window(window_ms, n),  # keep n_samples, stretch the window
        plot_window=None if plot_us is None else plot_us * 1e-6,
    )
    _run(
        cfg,
        config,
        proc,
        lambda b: proc.run(b, lt, cfg.analysis),
        out=out,
        sim=sim,
        save_sim=save_sim,
        psu=psu,
        ad=ad_serial,
        yes=yes,
        use_dmm=False,
        use_xscope=not no_xscope,
        **_modes(cfg, vref, enable),
    )


@app.command("line-tran")
def line_tran(
    config: ConfigOpt,
    low: Annotated[float | None, typer.Option(help="VIN low level (V)")] = None,
    high: Annotated[float | None, typer.Option(help="VIN high level (V)")] = None,
    load: LoadOpt = None,
    averages: AvgOpt = None,
    window_ms: WindowOpt = None,
    plot_us: PlotUsOpt = None,
    out: OutOpt = Path("results"),
    sim: SimOpt = False,
    save_sim: SaveSimOpt = False,
    psu: PsuOpt = None,
    ad_serial: AdOpt = None,
    no_xscope: NoXScopeOpt = False,
    vref: VrefOpt = Vref.bgr,
    yes: YesOpt = False,
    verbose: VerboseOpt = False,
) -> None:
    """Line transient: step VIN with W1 through the buffer, report VOUT peak and settling time."""
    from ldo_bench.procedures import line_tran as proc

    cfg = _load(config, verbose)
    n = _section(cfg, "line_tran").n_samples
    cfg, lt = _override(
        cfg,
        "line_tran",
        vin_low=low,
        vin_high=high,
        load_code=_load_code(cfg, load),
        averages=averages,
        sample_rate=_rate_for_window(window_ms, n),
        plot_window=None if plot_us is None else plot_us * 1e-6,
    )
    _run(
        cfg,
        config,
        proc,
        lambda b: proc.run(b, lt, cfg.analysis),
        out=out,
        sim=sim,
        save_sim=save_sim,
        psu=psu,
        ad=ad_serial,
        yes=yes,
        use_dmm=False,
        use_xscope=not no_xscope,
        **_modes(cfg, vref, split_buffer=True),
    )


@app.command("psrr")
def psrr(
    config: ConfigOpt,
    fstart: Annotated[float | None, typer.Option(help="First frequency (Hz)")] = None,
    fstop: Annotated[float | None, typer.Option(help="Last frequency (Hz)")] = None,
    ppd: Annotated[int | None, typer.Option(help="Points per decade")] = None,
    amp_mv: Annotated[float | None, typer.Option(help="Sine amplitude on VIN (mV peak)")] = None,
    vin: Annotated[float | None, typer.Option(help="DC level on VIN (V)")] = None,
    load: LoadOpt = None,
    averages: Annotated[int | None, typer.Option("--avg", help="Captures per frequency")] = None,
    out: OutOpt = Path("results"),
    sim: SimOpt = False,
    save_sim: SaveSimOpt = False,
    psu: PsuOpt = None,
    ad_serial: AdOpt = None,
    vref: VrefOpt = Vref.bgr,
    yes: YesOpt = False,
    verbose: VerboseOpt = False,
) -> None:
    """PSRR vs frequency: a sine on VIN through the buffer, its size on VIN and VOUT (AD2 sine fit)."""
    from ldo_bench.procedures import psrr as proc

    cfg = _load(config, verbose)
    cfg, sec = _override(
        cfg,
        "psrr",
        f_start=fstart,
        f_stop=fstop,
        points_per_decade=ppd,
        vin=vin,
        amplitude=None if amp_mv is None else amp_mv * 1e-3,
        averages=averages,
        load_code=_load_code(cfg, load),
    )
    _run(
        cfg,
        config,
        proc,
        lambda b: proc.run(b, sec),
        out=out,
        sim=sim,
        save_sim=save_sim,
        psu=psu,
        ad=ad_serial,
        yes=yes,
        use_dmm=False,
        **_modes(cfg, vref, split_buffer=True),
    )


# --- enable block and temperature (JAFWAK) --------------------------------------
@app.command("enable-sine")
def enable_sine(
    config: ConfigOpt,
    shape: Annotated[
        Shape | None,
        typer.Option(help="Input wave: sine, triangle (a slow linear ramp) or square (propagation delay)"),
    ] = None,
    freq: Annotated[float | None, typer.Option(help="Wave frequency (Hz), e.g. 1 for a slow ramp")] = None,
    captures: Annotated[int | None, typer.Option(help="Separate captures (threshold spread)")] = None,
    out: OutOpt = Path("results"),
    sim: SimOpt = False,
    save_sim: SaveSimOpt = False,
    psu: PsuOpt = None,
    ad_serial: AdOpt = None,
    yes: YesOpt = False,
    verbose: VerboseOpt = False,
) -> None:
    """Enable block on its own: a sine / triangle into EN_AIN gives the thresholds and hysteresis; a square, the delay."""
    from ldo_bench.procedures import en_sine as proc

    cfg, es = _override(
        _load(config, verbose), "en_sine", shape=shape and shape.value, frequency=freq, captures=captures
    )
    _run(
        cfg,
        config,
        proc,
        lambda b: proc.run(b, es),
        out=out,
        sim=sim,
        save_sim=save_sim,
        psu=psu,
        ad=ad_serial,
        yes=yes,
        use_dmm=False,
    )


@app.command("enable-startup")
def enable_startup(
    config: ConfigOpt,
    ch2: Annotated[
        Ch2 | None, typer.Option(help="Scope 2: vref (TP3, BGR), iin (across R13) or iout (across R1)")
    ] = None,
    averages: AvgOpt = None,
    window_ms: Annotated[float | None, typer.Option("--window-ms", help="Capture length (ms)")] = None,
    load: Annotated[
        str | None, typer.Option(help="Load during the edges: code or current, e.g. 4 or 19mA")
    ] = None,
    out: OutOpt = Path("results"),
    sim: SimOpt = False,
    save_sim: SaveSimOpt = False,
    psu: PsuOpt = None,
    ad_serial: AdOpt = None,
    yes: YesOpt = False,
    verbose: VerboseOpt = False,
) -> None:
    """Enable the LDO through the enable block: VOUT and VREF (or current) startup / shutdown timing."""
    from ldo_bench.procedures import startup as proc

    cfg = _load(config, verbose)
    cfg, su = _override(
        cfg,
        "startup",
        ch2=ch2 and ch2.value,
        averages=averages,
        window=None if window_ms is None else window_ms * 1e-3,
        load_code=_load_code(cfg, load),
    )
    _run(
        cfg,
        config,
        proc,
        lambda b: proc.run(b, su),
        out=out,
        sim=sim,
        save_sim=save_sim,
        psu=psu,
        ad=ad_serial,
        yes=yes,
        use_dmm=False,
    )


@app.command("temp")
def temp(
    config: ConfigOpt,
    minutes: Annotated[float | None, typer.Option(help="Log length (min), at most 120")] = None,
    interval: Annotated[float | None, typer.Option(help="Time between samples (s)")] = None,
    vin: Annotated[float | None, typer.Option(help="Override temp.vin (V)")] = None,
    out: OutOpt = Path("results"),
    sim: SimOpt = False,
    save_sim: SaveSimOpt = False,
    psu: PsuOpt = None,
    ad_serial: AdOpt = None,
    no_dmm: NoDmmOpt = False,
    enable: EnableOpt = Enable.static,
    yes: YesOpt = False,
    verbose: VerboseOpt = False,
) -> None:
    """Temperature log: TMP235 (J13 pin 5), VREF (DMM on TP3) and VOUT over time; tempco from stable plateaus.

    Ctrl-C ends the log early and keeps the data.
    """
    from ldo_bench.procedures import temp as proc

    cfg, tc = _override(
        _load(config, verbose),
        "temp",
        duration=None if minutes is None else minutes * 60,
        interval=interval,
        vin=vin,
    )
    _run(
        cfg,
        config,
        proc,
        lambda b: proc.run(b, tc),
        out=out,
        sim=sim,
        save_sim=save_sim,
        psu=psu,
        ad=ad_serial,
        yes=yes,
        use_dmm=not no_dmm,
        **_modes(cfg, enable=enable),
    )


# --- saved runs -----------------------------------------------------------------
@app.command("compare")
def compare_runs(
    run_a: Annotated[
        Path, typer.Argument(exists=True, file_okay=False, help="First run folder (e.g. the BGR run)")
    ],
    run_b: Annotated[
        Path, typer.Argument(exists=True, file_okay=False, help="Second run folder (e.g. the --vref ext run)")
    ],
    out: OutOpt = Path("results"),
    verbose: VerboseOpt = False,
) -> None:
    """Compare two saved runs of the same test (e.g. BGR vs --vref ext). Touches no instrument."""
    from ldo_bench import compare as proc
    from ldo_bench.results import RunDir

    _logging(verbose)
    try:
        a, b = proc.load_run(run_a), proc.load_run(run_b)
        res = proc.compare(a, b)
    except ValueError as e:
        _fail(str(e))
    sim = a.sim or b.sim
    # run A's saved config as it was saved: it may not load under this version's schema
    run = RunDir(out, proc.NAME, a.config, simulated=sim, invalid=bool(res.issues), tag=a.test)
    with _partial_on_error(run):
        run.write_csv("compare.csv", res.rows)
        run.write_json("summary.json", res.summary())
        run.write_meta(idns={}, sim=sim, extra={"runs": [str(a.path), str(b.path)]})
        _write_figs(run, {"plot.png": proc.plot(res, a.dut)}, sim)
    typer.echo(res.report())
    _saved(run.path, res=res)


def main() -> None:
    """Entry point for the `ldo-bench` script and `python -m ldo_bench`."""
    app()
