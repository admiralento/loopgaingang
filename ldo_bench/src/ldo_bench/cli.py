"""Command-line interface: `uv run ldo-bench --help`.

One command per test (load-tran, line-tran, load-reg, line-reg, vref-line, temp, iq, psrr, buffer-check, enable-sine,
enable-startup, psu-check, loadbank-check), plus check-config, discover, smoke and compare (two
saved runs side by side, e.g. the BGR against `--vref ext`). Every test
command follows the same pattern:

  1. Load and validate the config (`_load`). Nothing is touched if it's invalid.
  2. Apply command-line overrides by copying the test's section with the new values and
     re-validating the whole config (`_load_with`), so an option goes through exactly the
     same safety checks as the TOML file.
  3. Open the bench (`_bench`), which shuts everything down on the way out.
  4. Run the procedure: `procedures.<test>.run(bench, section, ...)` returns a result object.
  5. Save the run folder (`_save`; see results.py), inside the `with` block so instrument
     IDs are still available.
  6. Print the report (`_echo_report`) and where it was saved (`_saved`).

Exit codes:
  0    success
  1    instrument error (not found, refused a command, a safety limit tripped)
  2    bad config file or command-line option
  3    the test finished but found a setup problem (data saved, folder marked _INVALID)
  130  Ctrl-C, or wiring not confirmed (outputs are off)
"""

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

# Options shared by several commands.
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
NoXScopeOpt = Annotated[
    bool, typer.Option("--no-xscope", help="Don't use the [xscope] cross-check scope for this run")
]
PlotUsOpt = Annotated[
    float | None,
    typer.Option(
        "--plot-us",
        help="Zoomed plot: µs shown after the step, same for every edge (e.g. 100 = a scope at 10 µs/div)",
    ),
]
YesOpt = Annotated[bool, typer.Option("--yes", "-y", help="Don't ask to confirm wiring")]
VrefOpt = Annotated[
    str,
    typer.Option(
        "--vref",
        help="Reference on VREF_IN: 'bgr' (the on-chip BGR, JP2 fitted) or 'ext' ([vref]: PSU output on "
        "VREF_IN, JP2 removed; asks you to confirm JP2 every run)",
    ),
]
EnableOpt = Annotated[
    str,
    typer.Option(
        "--enable",
        help="LDO enable: 'static' (tied on with jumpers, e.g. J6 1-2) or 'dio' (driven from the AD2 "
        "through [enable]: J6 7-8, J13 pins 12 / 10 / 14; held low while VIN rises)",
    ),
]
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
    """Send log messages to the console: INFO and up, or DEBUG with -v. Quietens noisy libraries."""
    handler = logging.StreamHandler()
    handler.setFormatter(_ConsoleFormatter(verbose))
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO, handlers=[handler], force=True)
    logging.getLogger("matplotlib").setLevel(logging.WARNING)
    logging.getLogger("pyvisa").setLevel(logging.WARNING)


def _load(path: Path) -> BenchConfig:
    """Load and validate the config; on any validation error print it and exit 2."""
    try:
        return load_config(path)
    except ValidationError as e:
        typer.secho(f"Invalid config {path}:\n{e}", fg="red", err=True)
        raise typer.Exit(2) from e


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
):
    """Open the bench and turn instrument errors and Ctrl-C into clean exits.

    vref: 'bgr' or 'ext' (already checked by _vref_mode). enable: 'static' or 'dio' (_enable_mode).

    InstrumentError -> exit 1; KeyboardInterrupt (Ctrl-C or wiring not confirmed) -> exit 130.
    Bench.shutdown has already run by the time either is reported.
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


def _enable_mode(cfg: BenchConfig, enable: str) -> str:
    """Check --enable: 'static' or 'dio', and 'dio' needs enable pins clear of the load bank. Exit 2 otherwise."""
    if enable not in ("static", "dio"):
        typer.secho("--enable must be 'static' or 'dio'", fg="red", err=True)
        raise typer.Exit(2)
    if enable == "dio":
        e = cfg.enable
        clash = ({e.dio_pin} | ({e.dio_pin_n} - {None})) & set(cfg.loadbank.dio_pins)
        if clash:
            typer.secho(
                f"--enable dio: enable DIO pins {sorted(clash)} are also load bank pins", fg="red", err=True
            )
            raise typer.Exit(2)
    return enable


def _no_vref_with_split_buffer(cfg: BenchConfig, vref: str) -> None:
    """line-tran / psrr: --vref ext can't run with [buffer], whose negative rail is the same PSU output. Exit 2."""
    if (
        vref == "ext"
        and cfg.buffer is not None
        and cfg.vref is not None
        and cfg.vref.psu_channel == cfg.buffer.channel
    ):
        typer.secho(
            f"--vref ext can't run with the split-supply buffer ([buffer]): both use PSU OUT{cfg.buffer.channel}",
            fg="red",
            err=True,
        )
        raise typer.Exit(2)


def _vref_mode(cfg: BenchConfig, vref: str) -> str:
    """Check --vref: 'bgr' or 'ext', and 'ext' needs a [vref] section. Exit 2 otherwise."""
    if vref not in ("bgr", "ext"):
        typer.secho("--vref must be 'bgr' or 'ext'", fg="red", err=True)
        raise typer.Exit(2)
    if vref == "ext" and cfg.vref is None:
        typer.secho("--vref ext needs a [vref] section in the config", fg="red", err=True)
        raise typer.Exit(2)
    return vref


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

    cfg = _load(config) if config else None
    try:
        found = _discover(cfg=cfg)
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
    vref: VrefOpt = "bgr",
    enable: EnableOpt = "static",
    yes: YesOpt = False,
    verbose: VerboseOpt = False,
) -> None:
    """Quick sanity check: power up, read VOUT at no load and full load, power down."""
    _logging(verbose)
    cfg = _load(config)
    vref = _vref_mode(cfg, vref)
    # --vin isn't re-validated here; Bench.vin_from_psu still refuses anything above the ceilings.
    vin = vin if vin is not None else (cfg.load_reg.vin if cfg.load_reg else cfg.dut.vout_nominal + 0.6)
    with _bench(cfg, sim, psu, ad_serial, yes, vref=vref, enable=_enable_mode(cfg, enable)) as bench:
        _smoke(bench, vin)


def _smoke(bench: Bench, vin: float) -> None:
    """Body of `smoke`: VIN on, print a table at code 0 and at the heaviest allowed code, load off."""
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


def _section(cfg: BenchConfig, name: str):
    """The config section for a test ('load-tran' -> cfg.load_tran); exit 2 if the file has none."""
    sec = getattr(cfg, name.replace("-", "_"))
    if sec is None:
        typer.secho(f"The config has no [{name.replace('-', '_')}] section.", fg="red", err=True)
        raise typer.Exit(2)
    return sec


def _load_with(cfg: BenchConfig, **sections) -> BenchConfig:
    """Re-validate the config with some sections replaced (keeps the safety checks).

    `sections` maps a section name to its new model, e.g. load_tran=lt.model_copy(update=...).
    model_copy doesn't validate, so this dump-and-validate is what checks the new values. Exit 2
    if they're invalid.
    """
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

    `module` is the procedure module (its NAME, plot() and optional extra_plots()); `res` is its
    result (rows, summary(), optional issues and extra_csvs()). Returns the run folder, or None
    if nothing was saved.
    """
    from ldo_bench import plotting
    from ldo_bench.results import RunDir

    if bench.is_sim and not save_sim:
        return None
    invalid = bool(getattr(res, "issues", []))
    run = RunDir(
        out,
        module.NAME,
        cfg,
        config_path=config_path,
        simulated=bench.is_sim,
        invalid=invalid,
        tag="extref" if bench.vref_external else "",
    )
    try:
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
        for name, fig in figs.items():
            if bench.is_sim:
                plotting.mark_simulated(fig)
            run.save_fig(fig, name)
            plotting.close(fig)
        run.publish_latest()  # results/latest.png etc.: keep one open in the editor, it updates
    except BaseException:
        # Don't leave a folder that looks like a complete run: mark it, then let the error through.
        partial = run.path.with_name(run.path.name + "_PARTIAL")
        run.path.rename(partial)
        typer.secho(f"Saving failed part way: {partial} is incomplete", fg="red", err=True)
        raise
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
    plot_us: PlotUsOpt = None,
    out: OutOpt = Path("results"),
    sim: SimOpt = False,
    save_sim: SaveSimOpt = False,
    psu: PsuOpt = None,
    ad_serial: AdOpt = None,
    no_xscope: NoXScopeOpt = False,
    vref: VrefOpt = "bgr",
    enable: EnableOpt = "static",
    yes: YesOpt = False,
    verbose: VerboseOpt = False,
) -> None:
    """Load transient: step the load bank, capture VOUT, report peak and settling time."""
    from ldo_bench.loadbank import LoadBank, parse_load
    from ldo_bench.procedures import load_tran as proc

    _logging(verbose)
    cfg = _load(config)
    vref = _vref_mode(cfg, vref)
    lt = _section(cfg, "load-tran")
    update = {}
    try:
        bank = LoadBank(cfg.loadbank, None)  # no DIO: only used to convert currents to codes
        if low is not None:
            update["code_low"] = parse_load(low, bank, cfg.dut.vout_nominal)
        if high is not None:
            update["code_high"] = parse_load(high, bank, cfg.dut.vout_nominal)
    except ValueError as e:
        typer.secho(str(e), fg="red", err=True)
        raise typer.Exit(2) from e
    if plot_us is not None:
        if plot_us <= 0:
            typer.secho("--plot-us must be positive", fg="red", err=True)
            raise typer.Exit(2)
        update["plot_window"] = plot_us * 1e-6
    if window_ms is not None:
        if window_ms <= 0:
            typer.secho("--window-ms must be positive", fg="red", err=True)
            raise typer.Exit(2)
        update["sample_rate"] = lt.n_samples / (window_ms * 1e-3)  # keep n_samples, stretch the window
    if averages is not None:
        update["averages"] = averages
    if update:
        cfg = _load_with(cfg, load_tran=lt.model_copy(update=update))
        lt = cfg.load_tran
    with _bench(
        cfg,
        sim,
        psu,
        ad_serial,
        yes,
        use_dmm=False,
        use_xscope=not no_xscope,
        vref=vref,
        enable=_enable_mode(cfg, enable),
    ) as bench:
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
    plot_us: PlotUsOpt = None,
    out: OutOpt = Path("results"),
    sim: SimOpt = False,
    save_sim: SaveSimOpt = False,
    psu: PsuOpt = None,
    ad_serial: AdOpt = None,
    no_xscope: NoXScopeOpt = False,
    vref: VrefOpt = "bgr",
    yes: YesOpt = False,
    verbose: VerboseOpt = False,
) -> None:
    """Line transient: step VIN with W1 through the BUF634, report VOUT peak and settling time."""
    from ldo_bench.loadbank import LoadBank, parse_load
    from ldo_bench.procedures import line_tran as proc

    _logging(verbose)
    cfg = _load(config)
    vref = _vref_mode(cfg, vref)
    _no_vref_with_split_buffer(cfg, vref)
    lt = _section(cfg, "line-tran")
    update: dict = {}
    if low is not None:
        update["vin_low"] = low
    if high is not None:
        update["vin_high"] = high
    if plot_us is not None:
        if plot_us <= 0:
            typer.secho("--plot-us must be positive", fg="red", err=True)
            raise typer.Exit(2)
        update["plot_window"] = plot_us * 1e-6
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
    with _bench(cfg, sim, psu, ad_serial, yes, use_dmm=False, use_xscope=not no_xscope, vref=vref) as bench:
        res = proc.run(bench, lt, cfg.analysis)
        path = _save(bench, cfg, config, out, proc, res, save_sim)
    _echo_report(bench, res)
    _saved(path, fg="green", res=res)


@app.command("enable-sine")
def enable_sine(
    config: ConfigOpt,
    shape: Annotated[
        str | None,
        typer.Option(
            "--shape",
            help="Input wave: sine, triangle (a slow linear ramp up and down) or square (propagation delay)",
        ),
    ] = None,
    freq: Annotated[
        float | None, typer.Option("--freq", help="Wave frequency (Hz), e.g. 1 for a slow ramp")
    ] = None,
    captures: Annotated[
        int | None, typer.Option("--captures", help="Separate captures (threshold spread)")
    ] = None,
    out: OutOpt = Path("results"),
    sim: SimOpt = False,
    save_sim: SaveSimOpt = False,
    psu: PsuOpt = None,
    ad_serial: AdOpt = None,
    yes: YesOpt = False,
    verbose: VerboseOpt = False,
) -> None:
    """Enable block on its own: a sine or triangle into EN_AIN, its square-wave EN_OUT -> thresholds and hysteresis."""
    from ldo_bench.procedures import en_sine as proc

    _logging(verbose)
    cfg = _load(config)
    es = _section(cfg, "en-sine")
    update: dict = {}
    if shape is not None:
        update["shape"] = shape  # checked by _load_with (sine, triangle or square)
    if freq is not None:
        update["frequency"] = freq
    if captures is not None:
        update["captures"] = captures
    if update:
        cfg = _load_with(cfg, en_sine=es.model_copy(update=update))
        es = cfg.en_sine
    with _bench(cfg, sim, psu, ad_serial, yes, use_dmm=False) as bench:
        res = proc.run(bench, es)
        path = _save(bench, cfg, config, out, proc, res, save_sim)
    _echo_report(bench, res)
    _saved(path, res=res)


@app.command("enable-startup")
def enable_startup(
    config: ConfigOpt,
    ch2: Annotated[
        str | None,
        typer.Option("--ch2", help="Scope 2: vref (TP3, BGR), iin (across R13) or iout (across R1)"),
    ] = None,
    averages: Annotated[int | None, typer.Option("--avg", help="Captures averaged per edge")] = None,
    window_ms: WindowOpt = None,
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
    from ldo_bench.loadbank import LoadBank, parse_load
    from ldo_bench.procedures import startup as proc

    _logging(verbose)
    cfg = _load(config)
    su = _section(cfg, "startup")
    update: dict = {}
    if ch2 is not None:
        if ch2 not in ("vref", "iin", "iout"):
            typer.secho("--ch2 must be vref, iin or iout", fg="red", err=True)
            raise typer.Exit(2)
        update["ch2"] = ch2
    if averages is not None:
        update["averages"] = averages
    if window_ms is not None:
        update["window"] = window_ms * 1e-3
    if load is not None:
        try:
            update["load_code"] = parse_load(load, LoadBank(cfg.loadbank, None), cfg.dut.vout_nominal)
        except ValueError as e:
            typer.secho(str(e), fg="red", err=True)
            raise typer.Exit(2) from e
    if update:
        cfg = _load_with(cfg, startup=su.model_copy(update=update))
        su = cfg.startup
    with _bench(cfg, sim, psu, ad_serial, yes, use_dmm=False) as bench:
        res = proc.run(bench, su)
        path = _save(bench, cfg, config, out, proc, res, save_sim)
    _echo_report(bench, res)
    _saved(path, res=res)


@app.command("iq")
def iq(
    config: ConfigOpt,
    method: Annotated[
        str | None,
        typer.Option(help="shunt (DMM volts across R13) or series (DMM amps in place of a J1 jumper)"),
    ] = None,
    rail: Annotated[
        str | None, typer.Option(help="series: total, bgr, vdd, en or ota (the J1 rail the DMM replaces)")
    ] = None,
    vin: Annotated[float | None, typer.Option(help="One VIN point instead of the sweep (V)")] = None,
    states: Annotated[
        str | None, typer.Option(help="Comma list: enabled, disabled (e.g. 'enabled' on its own)")
    ] = None,
    out: OutOpt = Path("results"),
    sim: SimOpt = False,
    save_sim: SaveSimOpt = False,
    psu: PsuOpt = None,
    ad_serial: AdOpt = None,
    vref: VrefOpt = "bgr",
    yes: YesOpt = False,
    verbose: VerboseOpt = False,
) -> None:
    """Quiescent (enabled) and shutdown (disabled) current vs VIN, no load, measured by the DMM."""
    from ldo_bench.procedures import iq as proc

    _logging(verbose)
    cfg = _load(config)
    vref = _vref_mode(cfg, vref)
    sec = _section(cfg, "iq")
    update: dict = {}
    if method is not None:
        update["method"] = method
        if method == "shunt" and rail is None:
            update["rail"] = "total"
    if rail is not None:
        update["rail"] = rail
    if vin is not None:
        update |= {"vin_start": vin, "vin_stop": vin}
    if states is not None:
        update["states"] = [s.strip() for s in states.split(",") if s.strip()]
    if update:
        # Validate the section on its own first (a clearer error); _load_with then checks the whole config.
        try:
            sec = type(sec).model_validate(sec.model_dump() | update)
        except ValidationError as e:
            typer.secho(f"Invalid option:\n{e}", fg="red", err=True)
            raise typer.Exit(2) from e
        cfg = _load_with(cfg, iq=sec)
        sec = cfg.iq
    with _bench(cfg, sim, psu, ad_serial, yes, vref=vref) as bench:
        res = proc.run(bench, sec)
        path = _save(bench, cfg, config, out, proc, res, save_sim)
    _echo_report(bench, res)
    _saved(path, res=res)


@app.command("psrr")
def psrr(
    config: ConfigOpt,
    fstart: Annotated[float | None, typer.Option("--fstart", help="First frequency (Hz)")] = None,
    fstop: Annotated[float | None, typer.Option("--fstop", help="Last frequency (Hz)")] = None,
    ppd: Annotated[int | None, typer.Option("--ppd", help="Points per decade")] = None,
    amp_mv: Annotated[float | None, typer.Option("--amp-mv", help="Sine amplitude on VIN (mV peak)")] = None,
    vin: Annotated[float | None, typer.Option(help="DC level on VIN (V)")] = None,
    load: Annotated[str | None, typer.Option(help="Load as a code or current, e.g. 4 or 19mA")] = None,
    averages: Annotated[int | None, typer.Option("--avg", help="Captures per frequency")] = None,
    out: OutOpt = Path("results"),
    sim: SimOpt = False,
    save_sim: SaveSimOpt = False,
    psu: PsuOpt = None,
    ad_serial: AdOpt = None,
    vref: VrefOpt = "bgr",
    yes: YesOpt = False,
    verbose: VerboseOpt = False,
) -> None:
    """PSRR vs frequency: a sine on VIN through the BUF634, its size on VIN and VOUT (AD2 sine fit)."""
    from ldo_bench.loadbank import LoadBank, parse_load
    from ldo_bench.procedures import psrr as proc

    _logging(verbose)
    cfg = _load(config)
    vref = _vref_mode(cfg, vref)
    _no_vref_with_split_buffer(cfg, vref)
    sec = _section(cfg, "psrr")
    update: dict = {}
    for key, val in (("f_start", fstart), ("f_stop", fstop), ("points_per_decade", ppd), ("vin", vin)):
        if val is not None:
            update[key] = val
    if amp_mv is not None:
        update["amplitude"] = amp_mv * 1e-3
    if averages is not None:
        update["averages"] = averages
    if load is not None:
        try:
            update["load_code"] = parse_load(load, LoadBank(cfg.loadbank, None), cfg.dut.vout_nominal)
        except ValueError as e:
            typer.secho(str(e), fg="red", err=True)
            raise typer.Exit(2) from e
    if update:
        try:
            sec = type(sec).model_validate(sec.model_dump() | update)
        except ValidationError as e:
            typer.secho(f"Invalid option:\n{e}", fg="red", err=True)
            raise typer.Exit(2) from e
        cfg = _load_with(cfg, psrr=sec)
        sec = cfg.psrr
    with _bench(cfg, sim, psu, ad_serial, yes, use_dmm=False, vref=vref) as bench:
        res = proc.run(bench, sec)
        path = _save(bench, cfg, config, out, proc, res, save_sim)
    _echo_report(bench, res)
    _saved(path, res=res)


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
    vref: VrefOpt = "bgr",
    enable: EnableOpt = "static",
    yes: YesOpt = False,
    verbose: VerboseOpt = False,
) -> None:
    """Line regulation: sweep VIN at a fixed load; reports dVOUT/dVIN and the dropout knee."""
    from ldo_bench.loadbank import LoadBank, parse_load
    from ldo_bench.procedures import line_reg as proc

    _logging(verbose)
    cfg = _load(config)
    vref = _vref_mode(cfg, vref)
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
    with _bench(
        cfg, sim, psu, ad_serial, yes, use_dmm=not no_dmm, vref=vref, enable=_enable_mode(cfg, enable)
    ) as bench:
        res = proc.run(bench, lr)
        path = _save(bench, cfg, config, out, proc, res, save_sim)
    _echo_report(bench, res)
    _saved(path, fg="green", res=res)


@app.command("vref-line")
def vref_line(
    config: ConfigOpt,
    high: Annotated[float | None, typer.Option("--high", help="First (highest) VIN (V)")] = None,
    low: Annotated[float | None, typer.Option("--low", help="Last (lowest) VIN (V)")] = None,
    step: Annotated[float | None, typer.Option("--step", help="VIN step (V)")] = None,
    vin_from: Annotated[
        str | None, typer.Option("--vin-from", help="'ad2' (VIN at the board) or 'psu' (supply readback)")
    ] = None,
    out: OutOpt = Path("results"),
    sim: SimOpt = False,
    save_sim: SaveSimOpt = False,
    psu: PsuOpt = None,
    ad_serial: AdOpt = None,
    enable: EnableOpt = "static",
    yes: YesOpt = False,
    verbose: VerboseOpt = False,
) -> None:
    """VREF vs VIN: step VIN down with the LDO on, DMM on VREF (TP3); BGR line sensitivity and minimum VIN."""
    from ldo_bench.procedures import vref_line as proc

    _logging(verbose)
    cfg = _load(config)
    vl = _section(cfg, "vref-line")
    update = {k: v for k, v in (("vin_high", high), ("vin_low", low), ("vin_step", step)) if v is not None}
    if vin_from is not None:
        if vin_from not in ("ad2", "psu"):
            typer.secho("--vin-from must be 'ad2' or 'psu'", fg="red", err=True)
            raise typer.Exit(2)
        update["vin_source"] = vin_from
    if update:
        cfg = _load_with(cfg, vref_line=vl.model_copy(update=update))
        vl = cfg.vref_line
    if cfg.dmm is None or not cfg.dmm.enabled:
        typer.secho(
            "vref-line reads VREF with the DMM: the config's [dmm] must be enabled", fg="red", err=True
        )
        raise typer.Exit(2)
    with _bench(cfg, sim, psu, ad_serial, yes, enable=_enable_mode(cfg, enable)) as bench:
        res = proc.run(bench, vl)
        path = _save(bench, cfg, config, out, proc, res, save_sim)
    _echo_report(bench, res)
    _saved(path, fg="green", res=res)


@app.command("temp")
def temp(
    config: ConfigOpt,
    minutes: Annotated[float | None, typer.Option("--minutes", help="Log length (min), at most 120")] = None,
    interval: Annotated[float | None, typer.Option("--interval", help="Time between samples (s)")] = None,
    vin: Annotated[float | None, typer.Option(help="Override temp.vin (V)")] = None,
    out: OutOpt = Path("results"),
    sim: SimOpt = False,
    save_sim: SaveSimOpt = False,
    psu: PsuOpt = None,
    ad_serial: AdOpt = None,
    no_dmm: NoDmmOpt = False,
    enable: EnableOpt = "static",
    yes: YesOpt = False,
    verbose: VerboseOpt = False,
) -> None:
    """Temperature log: TMP235 (J13 pin 5), VREF (DMM on TP3) and VOUT over time; tempco from stable plateaus.

    Ctrl-C ends the log early and keeps the data.
    """
    from ldo_bench.procedures import temp as proc

    _logging(verbose)
    cfg = _load(config)
    tc = _section(cfg, "temp")
    update = {}
    if minutes is not None:
        update["duration"] = minutes * 60
    if interval is not None:
        update["interval"] = interval
    if vin is not None:
        update["vin"] = vin
    if update:
        cfg = _load_with(cfg, temp=tc.model_copy(update=update))
        tc = cfg.temp
    with _bench(cfg, sim, psu, ad_serial, yes, use_dmm=not no_dmm, enable=_enable_mode(cfg, enable)) as bench:
        res = proc.run(bench, tc)
        path = _save(bench, cfg, config, out, proc, res, save_sim)
    _echo_report(bench, res)
    _saved(path, fg="green", res=res)


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

    _logging(verbose)
    cfg = _load(config)
    if cfg.buffer is None:
        typer.secho("buffer-check needs a [buffer] section (the split-supply U2)", fg="red", err=True)
        raise typer.Exit(2)
    with _bench(cfg, sim, psu, ad_serial, yes, use_dmm=False) as bench:
        res = proc.run(bench, cfg.buffer)
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
    vref: VrefOpt = "bgr",
    enable: EnableOpt = "static",
    yes: YesOpt = False,
    verbose: VerboseOpt = False,
) -> None:
    """Load regulation: step the load bank at fixed VIN, record VOUT vs load current."""
    from ldo_bench.procedures import load_reg as proc

    _logging(verbose)
    cfg = _load(config)
    vref = _vref_mode(cfg, vref)
    lr = _section(cfg, "load-reg")
    if vin is not None:
        cfg = _load_with(cfg, load_reg=lr.model_copy(update={"vin": vin}))
        lr = cfg.load_reg
    with _bench(
        cfg, sim, psu, ad_serial, yes, use_dmm=not no_dmm, vref=vref, enable=_enable_mode(cfg, enable)
    ) as bench:
        res = proc.run(bench, lr)
        path = _save(bench, cfg, config, out, proc, res, save_sim)
    _echo_report(bench, res)
    _saved(path, fg="green", res=res)


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
    from ldo_bench import plotting
    from ldo_bench.results import RunDir

    _logging(verbose)
    try:
        a, b = proc.load_run(run_a), proc.load_run(run_b)
        res = proc.compare(a, b)
    except ValueError as e:
        typer.secho(str(e), fg="red", err=True)
        raise typer.Exit(2) from e
    try:
        cfg = BenchConfig.model_validate(a.config)
    except ValidationError as e:
        typer.secho(
            f"run A's saved config doesn't load with this version of ldo-bench:\n{e}", fg="red", err=True
        )
        raise typer.Exit(2) from e
    run = RunDir(out, proc.NAME, cfg, simulated=a.sim or b.sim, invalid=bool(res.issues), tag=a.test)
    run.write_csv("compare.csv", res.rows)
    run.write_json("summary.json", res.summary())
    run.write_meta(idns={}, sim=a.sim or b.sim, extra={"runs": [str(a.path), str(b.path)]})
    fig = proc.plot(res, a.dut)
    if a.sim or b.sim:
        plotting.mark_simulated(fig)
    run.save_fig(fig)
    plotting.close(fig)
    run.publish_latest()
    typer.echo(res.report())
    _saved(run.path, res=res)


def main() -> None:
    """Entry point for the `ldo-bench` script and `python -m ldo_bench`."""
    app()
