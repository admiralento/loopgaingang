# How the code works

This is a tour of `ldo_bench` for someone about to change it. For running tests, see the [README](../README.md); for what each test does, see [tests.md](tests.md).

## The short version

```
ldo-bench <test> -c configs/<board>.toml
        │
        ▼
cli.py ── load_config() ──► BenchConfig (pydantic; every limit checked before anything powers up)
        │  CLI options (--load, --vin, ...) are applied by re-validating the config, so they obey the same limits
        ▼
open_bench() ── real: Finder searches VISA / RS-232 / Prologix and opens PSU, AD2, DMM, 54621A
        │       --sim: LdoModel + fake instruments sharing its state
        ▼
Bench ── the safety layer: voltage ceilings, PSU ramping, load-current limit, wiring prompt, shutdown
        │
        ▼
procedures/<test>.run(bench, cfg.<section>) ──► Result (rows, summary(), report(), issues)
        │
        ▼
cli._save() ── RunDir: data.csv, summary.json, meta.json, plot.png (+ extras), results/latest.png
        │
        ▼
report printed; exit code 0 (ok), 3 (finished, but a setup problem), 1 (instrument error, or a shutdown step failed),
2 (bad config/option), 130 (Ctrl-C)
```

## Folder layout

```
ldo_bench/
├── pyproject.toml          uv project: dependencies, the `ldo-bench` command, ruff + pytest settings
├── uv.lock                 exact versions, so every machine (macOS, Windows) gets the same environment
├── configs/
│   ├── jafwak.toml         our LDO board (1.2 V out, VIN ≤ 1.9 V, load ≤ 20 mA)
│   └── demo_board.toml     TPS7A7001 evaluation board (1.0 V out) + the Switched_R_Load_Bank board
├── docs/                   this folder
├── src/ldo_bench/
│   ├── cli.py              one Typer command per test
│   ├── config.py           the TOML schema (pydantic models) and the cross-checks between sections
│   ├── bench.py            Bench (safety layer) and open_bench()
│   ├── loadbank.py         load bank: code ↔ DIO pins, current estimate, the dut.i_max limit
│   ├── analysis.py         pure numpy: fits, settling (WaveForms port), Schmitt thresholds, step metrics, sine fit
│   ├── results.py          run folders and the latest.png copies
│   ├── compare.py          `ldo-bench compare`: two saved runs side by side (BGR vs external reference)
│   ├── plotting.py         shared matplotlib style, SETUP PROBLEM box, SIMULATED banner
│   ├── instruments/
│   │   ├── base.py         Protocols every driver implements (PowerSupply, SupplyOutput, Oscilloscope, DigitalOut, Wavegen, Voltmeter)
│   │   ├── transports.py   VISA, Prologix and RS-232 links behind one write/query interface
│   │   ├── discovery.py    finds instruments by model on any interface
│   │   ├── e36xx.py        Agilent E3648A / E3638A supply
│   │   ├── hp34401a.py     Agilent 34401A DMM (DC volts, DC amps)
│   │   ├── ad2.py          Analog Discovery 2 through pydwf (scope, wavegen, DIO, V+)
│   │   ├── agilent546xx.py Agilent 54621A cross-check scope
│   │   └── sim.py          the simulated bench (LdoModel and fake instruments)
│   └── procedures/         one module per test (see below), plus transient.py shared by the transient tests
└── tests/                  pytest: drivers against fake serial/VISA links, every procedure on the simulator
```

## Configuration (`config.py`)

Everything that depends on the board, the wiring or the test conditions is in the TOML file: no instrument addresses, pins or voltages are hard-coded. Each `[section]` maps to a pydantic model, and the models use `extra="forbid"`, so a misspelt key is an error instead of being silently ignored.

`BenchConfig._cross_checks` ties sections together. The important ones:
- **VIN ceilings:** any VIN a test can apply (sweep ends, step levels, sine peaks) must be within `dut.vin_max`, and within `psu.v_max` whenever the PSU drives VIN directly.
- **Buffer tests:** in line-tran and psrr the PSU powers the BUF634 rail, which sits *above* `dut.vin_max`. The rail must be within `psu.v_max`, and must be at least 2 V (`buffer_headroom`) above the highest VIN, or the buffer clips.
- **Load:** every load code a test uses must draw no more than `dut.i_max` at `vout_nominal`.
- **Pins:** the enable DIO pins must not also be load-bank pins.
- **External reference** (`[vref]`, if present): it needs the PSU's other output, `vref.v_max` within `dut.vin_max`, and `vref.external_v` no more than `vref.above_vin` (0.3 V) above any test's lowest VIN (`BenchConfig.lowest_vin()`).

CLI options never bypass this: `cli._load_with()` rebuilds the config with the changed section and validates it again.

## The Bench (`bench.py`)

Procedures never program a voltage onto the DUT directly. They call Bench methods that enforce the limits:

| Method | Drives | Limit |
|---|---|---|
| `vin_from_psu(v)` | PSU → VIN | `min(dut.vin_max, psu.v_max)` |
| `buffer_rail(v)` | PSU → BUF634 rail | `psu.v_max` |
| `psu_output(v)` | PSU with only a meter on it (psu-check) | `psu.v_max` |
| `vin_from_wavegen(v)` | W1 DC → buffer → VIN | `min(dut.vin_max, 5 V)` |
| `vin_sine_from_wavegen(dc, amplitude, f)` | W1 sine → buffer → VIN | peak within `min(dut.vin_max, 5 V)` |
| `en_ain_sine(ch, f, low, high)` | W1 sine → EN_AIN (enable-sine) | −0.3 V to `min(dut.vin_max, 5 V)` |
| `enable_logic(on)`, `set_enable(on)` | AD2 V+ → V_33, DIO → EN_CTRL_33 | fixed by `[enable]` |
| `loadbank.set(code)` | AD2 DIO → load bank | `dut.i_max` |
| `reference_on()` / `reference_off()` | PSU's other output → VREF_IN (`--vref ext` only) | `vref.v_max`, and at most `vref.above_vin` above VIN |

Other things the Bench does:
- **Supply setup:** the PSU current limit and OVP are set before the output is first turned on, and voltage changes are ramped in `psu.ramp_step` steps. If the supply rejects the OVP setting, the run stops; only `psu.ovp` left out of the config runs without hardware OVP.
- **VIN through the buffer** (line-tran, psrr): VIN is read on the AD2's fine 5 V range whenever `dut.vin_max` fits on it, and every limit check keeps a margin for the AD2's error below `dut.vin_max` (`ad2.vin_offset_error` + `ad2.vin_gain_error` × V: about 20 mV at 1.9 V; these figures are from memory of the AD2 datasheet and still need checking). The interlock is in two stages; see [tests.md](tests.md#line-tran-line-transient).
- **Wiring prompt:** `confirm_wiring()` prints the wiring a test needs and waits for a yes (`--yes` skips it). Its keyword arguments (`ref_probe`, `dmm_probe`, `psu_to`, ...) don't affect real hardware: they tell the simulator how the bench is wired.
- **External reference** (`--vref ext`, `Bench.vref_external`): JP2 is removed and the PSU's other output (`vref.psu_channel`, OUT2) drives VREF_IN in place of the BGR.
  - **Setup:** that output is set up with the supply (current limit, OVP, 0 V). The E3648A's `OUTP` switches both outputs together, so it sits at 0 V whenever VIN is up and the reference isn't.
  - **Turn-on:** each procedure calls `reference_on()` once VIN is up and the LDO enabled; in the normal BGR mode it does nothing. It ramps to `vref.external_v`, then proves VOUT follows: it steps the reference down by `vref.proof_step` (20 mV) and back, and VOUT on the AD2 must move at least half of that. If it doesn't, the reference goes to 0 V and the run stops (`InstrumentError`): JP2 is probably still fitted and the BGR is holding VREF_IN.
  - **While on:** VIN may not go more than `vref.above_vin` below the reference, `buffer_rail` and `psu_output` are refused, and `supply_off()` and turning off the wavegen that drives VIN ramp the reference to 0 V first.
  - **JP2 can't be measured beforehand**, and the BGR must not be back-driven, so `confirm_wiring()` asks about JP2 in a separate question every run, even with `--yes`. A run that can't ask (no prompt) stops.
  - **Records:** `vref_info()` (source, the applied voltage, the proof readings) goes into `summary.json` and `meta.json` as `vref`, and the folder name gets `_extref`.
- **Shutdown:** leaving the `with` block for any reason (end of test, exception, Ctrl-C) runs `shutdown()`. It turns off, in order: external reference (0 V), wavegen, AD2 V+, load bank, DIO, PSU output (then the setpoint goes to 0 V), and closes every instrument. Each step runs even if an earlier one fails. A failed step is printed in red as SHUTDOWN FAILED and the command exits 1, since the bench may still be powered.
- **Commands are confirmed:** after every ramp the supply's setpoint is read back, and `OUTP OFF` is confirmed with `OUTP?`. A serial reply that ends at the timeout instead of its line feed is rejected, not parsed.
- **Measurements:** `measure_dc()` averages both AD2 channels, `measure_point()` takes VOUT from the DMM when there is one, and `vout_issue()` is the standard "is VOUT where it should be?" check. With a DMM, `vout_agreement_issue()` also checks the AD2 reads the same VOUT (2 % + 50 mV), so a loose scope 1 is caught in the DC tests too.

## Instruments (`instruments/`)

Procedures only use the Protocols in `base.py`, never a concrete driver. That's what makes the simulator possible, and what lets a different supply or scope be swapped in by writing one class.

A driver sits on a **transport** (`transports.py`) with `write`, `query`, `read_line` and `read_block`:
- `VisaTransport`: GPIB cards, NI / Keysight adapters, and the XyphroLabs UsbGpib (USBTMC) through pyvisa or pyvisa-py.
- `PrologixTransport`: Prologix-style USB-GPIB adapters (a serial port speaking `++` commands).
- `SerialTransport`: an instrument's own RS-232 port. It handles HP's DTR/DSR pacing, 7-bit parity, and IEEE 488.2 binary blocks (scope waveforms).

`discovery.py` finds each instrument by its `*IDN?` model, not by address, so the same config works on any machine. The `Finder` searches once per run, in order: VISA, then RS-232 ports with that instrument's serial settings (and the other parity settings, reporting which one worked), then Prologix adapters at every address. A port one instrument claimed is never probed for another. A test that doesn't use the DMM but searches RS-232 for the 54621A first finds the DMM's port with the DMM's own settings and claims it (`reserve_dmm_port`). Otherwise the scope's 8N1 probe would reach a 34401A set to 7E2 as garbage, which it logs as an error and beeps at.

The AD2 (`ad2.py`) is one object implementing Oscilloscope, DigitalOut and Wavegen. It needs the Digilent WaveForms runtime, and only one program can open it, so the WaveForms GUI must be closed.

See [instruments.md](instruments.md) for setting each instrument up.

## Procedures (`procedures/`)

Every test module has the same shape, which `cli._save()` relies on:

```python
NAME = "load-reg"                       # results/<NAME>/ folder

def wiring(bench, cfg_section) -> list[str]: ...   # shown before anything powers up
def run(bench, cfg_section) -> Result: ...         # the measurement
def plot(result, dut_name) -> Figure: ...          # plot.png
def extra_plots(result, dut_name) -> dict: ...     # optional: more figures (plot_full.png, plot_zoom.png)

@dataclass
class Result:
    issues: list[str]          # setup problems found in the data ([] = valid run)
    rows: list[dict]           # → data.csv
    def summary(self) -> dict  # → summary.json
    def report(self) -> str    # printed to the terminal
    def extra_csvs(self)       # optional: {"waveform_rising.csv": rows, ...}
```

`transient.py` holds what load-tran and line-tran share: averaged triggered captures (`averaged_step`), the 54621A cross-check, the setup checks on a captured edge, the per-edge report, and the plot with a common time span.

## Real data only: the validity model

This is the rule the whole package is built around: **nothing saved or plotted is filled in or assumed.** It was added after a load-transient run with a floating scope probe produced a convincing-looking plot (see [bench-log.md](bench-log.md)).

- **Two kinds of problem:**
  - **Safety limits stop the run.** These raise `InstrumentError`: VIN over `vin_max`, load over `i_max`, the buffer interlock tripping.
  - **Setup problems don't.** When the data shows a probe isn't on VOUT, a trigger never came, the stimulus didn't reach the board, or the meter isn't connected, the test keeps going and records the problem in `issues`.
- **What a run with issues looks like:**
  - the folder name ends in `_INVALID`
  - `summary.json` has `"valid": false` and the list
  - the plot has a red **SETUP PROBLEM** box and leaves off result markers
  - the terminal prints the problems in red
  - the command exits with code 3
- **Measurement floors are bounds, not numbers.** A reading under a meter's resolution is reported as one: Iq "< 4 µA", PSRR "≥ 72 dB".
- **The 54621A never invalidates an AD2 result.** Its problems are listed under the cross-check only.

## The simulator (`instruments/sim.py`)

`--sim` replaces every instrument with a fake that shares one `LdoModel`. The model is simple but has what the analysis must cope with:
- output resistance, line regulation and dropout
- noise, a ringing transient and a switching glitch
- the BUF634 offset and its dropout from the rail
- the enable comparator and the startup dynamics
- quiescent and shutdown current, and a PSRR transfer function
- the reference the regulator follows: the BGR (at `vout_nominal`) or, with JP2 out, the PSU's second output (`LdoModel.reference`)

It also tracks **how the bench is wired**: what scope 2 is on, where the PSU and DMM are connected, which DIO pin is the enable, whether JP2 is fitted (`jp2_fitted`; `bgr_backdriven` records a second output driving the BGR). That lets tests miswire the bench on purpose and check that the right setup problem is reported.

Simulated runs save nothing unless `--save-sim`. Then the folder ends in `_SIM` and every plot carries a SIMULATED banner.

## Results (`results.py`)

```
results/
├── latest.png                 newest run of any test (keep it open in VS Code: it updates)
├── latest_run.txt             which run folder latest.png came from
└── <test>/
    ├── latest.png, latest_full.png, latest_zoom.png
    └── <YYYYMMDD-HHMMSS>_<dut>_<test>[_extref][_SIM][_INVALID][_PARTIAL]/
        ├── data.csv           every measured point
        ├── summary.json       headline numbers, "valid", "issues"
        ├── meta.json          config used, instrument IDNs, git commit, package versions
        ├── plot.png           (+ plot_zoom.png, plot_full.png, waveform_*.csv for transients)
```

`_extref` marks a `--vref ext` run. `ldo-bench compare` writes to `results/compare/<stamp>_<dut>_compare_<test>/` (`compare.csv`, `summary.json`, `plot.png`). A second run in the same second gets `<stamp>-2`, so nothing is overwritten. `_PARTIAL` means saving stopped part way (the error is printed): the folder is incomplete. `meta.json`'s `dirty` is `null` when git wasn't available. `results/` is in `.gitignore`: runs stay on the machine that made them.

## Tests and tooling

```
uv run poe verify         # the gate: format check, lint, both configs validated, 329 tests (~10 s, no hardware)
uv run poe test           # just the tests
uv run poe format         # format the code
```

The tasks are defined in `[tool.poe.tasks]` in `pyproject.toml` (poethepoet, a dev dependency, so nothing is installed on the host and it works the same on Windows).

- **Drivers** are tested against fake transports that record what was sent and answer like the instrument (`FakeSerial`, `FakeScope`).
- **Procedures** run end to end on the simulator, including deliberately miswired cases.
- **Before committing,** run `uv run poe verify` and check its exit code, not just the last lines of its output.

## Recipes

**Add a test:**
1. Add a config model in `config.py` and its section in `BenchConfig`, plus any cross-checks (VIN ceiling, load code).
2. Write `procedures/<name>.py` with the shape above. Drive voltages only through the Bench methods, and turn on what you need before measuring. Record setup problems in `issues` instead of stopping.
3. Add the command in `cli.py`, following the existing commands: load, apply options with `_load_with`, `_bench`, `run`, `_save`, `_echo_report`, `_saved`. If it can run on an external reference, give it `--vref` (`VrefOpt`, `_vref_mode`), call `bench.reference_on()` once VIN is up, add its lowest VIN to `BenchConfig.lowest_vin()`, and its plot to `compare.plot()`.
4. Teach `sim.py` enough to run it, and write tests: one on the simulator, one per setup check.
5. Add the section to the board configs and document it in [tests.md](tests.md).

**Add an instrument:**
1. Write a driver that takes a transport and implements the Protocol from `base.py`. Check `*IDN?` in its constructor and raise `InstrumentError` for the wrong model.
2. Add an `open_<kind>()` to `discovery.py` using the `Finder`, and a config section with `resource` and the RS-232 settings.
3. Add a fake to `sim.py`, and driver tests against a fake transport.

**Spectrum analyzer (PSRR past the AD2's floor):** `procedures/psrr.py` measures VOUT through a `PsrrMeter` (`measure(f) -> Tone`). A spectrum analyzer meter would:
- tune to `f` and read the VOUT amplitude, converting dBm into 50 Ω (or through its probe) to volts peak
- take VIN's amplitude from AD2 scope 2
- set `vout_floor` from the analyzer's noise floor at that RBW
- set the phase to `nan` (no phase information)

`psrr.run(bench, cfg, meter=...)` accepts it as is.
