# ldo-bench

Bench automation for characterizing our LDO (the JAFWAK board) and the TPS7A7001 demo board. One command per test: it checks the config, tells you how to wire the bench, runs the measurement, then saves the data, a plot and a summary.

| Test | Command | Measures |
|---|---|---|
| Load regulation | `load-reg` | VOUT vs load current → output resistance |
| Line regulation | `line-reg` | VOUT vs VIN → mV/V and the dropout voltage |
| Load transient | `load-tran` | VOUT after a load step → peak deviation, settling time |
| Line transient | `line-tran` | VOUT after a VIN step (through the BUF634) |
| PSRR | `psrr` | VIN ripple rejection, 100 Hz – 1 MHz |
| Quiescent current | `iq` | supply current enabled / disabled vs VIN |
| Enable thresholds | `enable-sine` | enable comparator VT+, VT-, hysteresis (JAFWAK) |
| Startup | `enable-startup` | VOUT / VREF / current after the enable edge (JAFWAK) |
| Bench checks | `discover`, `psu-check`, `loadbank-check`, `smoke` | instruments found, supply accuracy, load bank wiring, quick power-up |
| BGR vs external reference | any DC, transient, psrr or iq test with `--vref ext`, then `compare` | the same test with 1.204 V on VREF_IN in place of the BGR (JP2 removed), side by side with the BGR run |

**Docs:**
- [docs/tests.md](docs/tests.md): each test's wiring, what it does, what it computes, its options.
- [docs/instruments.md](docs/instruments.md): setting up the supply, DMM, AD2 and 54621A, and the GPIB / RS-232 connections.
- [docs/architecture.md](docs/architecture.md): how the code is organised, the safety layer, the simulator, adding a test or an instrument.
- [docs/bench-log.md](docs/bench-log.md): what we built and measured on 2026-11-06, and the problems we hit.

## Hardware

- **Agilent E3648A** (dual output; an E3638A works too): VIN, or the BUF634 rail for line-tran / psrr. On GPIB through a XyphroLabs UsbGpib adapter.
- **Digilent Analog Discovery 2:** scope (VOUT, VIN / trigger), W1 (line-tran, psrr and enable-sine stimulus), DIO 0–4 (load bank), DIO 5–6 and V+ (JAFWAK enable).
- **Agilent 34401A:** VOUT in the DC tests, and the `iq` measurement. On RS-232 through a USB-serial adapter.
- **Agilent 54621A** (optional): an independent capture of every transient edge.
- **Load banks:** binary-weighted resistors switched by FETs from AD2 DIO 0–4. JAFWAK has one on board (`load_cell.kicad_sch`); the demo board uses `Switched_R_Load_Bank`.
- **Spectrum analyzer:** coming, for PSRR past what the AD2 can resolve.

## Quick start

1. **Install** [uv](https://docs.astral.sh/uv/) and the [Digilent WaveForms](https://digilent.com/reference/software/waveforms/waveforms-3/start) runtime. On Windows, also install NI-VISA.
2. **Set up the environment:**
   ```
   cd ldo_bench
   uv sync
   uv run poe verify         # the gate: lint, both configs and 297 tests, no hardware needed
   ```
3. **Try it on the simulator** (no hardware; prints a SIMULATED report and saves nothing):
   ```
   uv run ldo-bench load-tran -c configs/demo_board.toml --sim
   ```
4. **Switch on the instruments.** Set the supply's I/O to GPIB and the DMM's to RS-232 (see [instruments.md](docs/instruments.md)). Close the WaveForms GUI, then:
   ```
   uv run ldo-bench discover -c configs/demo_board.toml
   ```
5. **First session on a board**, in this order:
   1. `psu-check`
   2. `loadbank-check`
   3. `smoke`
   4. `load-reg`
   5. `line-reg`
   6. `load-tran`
   7. then the rest

```
uv run ldo-bench --help
uv run ldo-bench check-config -c configs/jafwak.toml      # validate a config, touch nothing

uv run ldo-bench psu-check      -c configs/demo_board.toml   # supply setpoint/readback vs DMM
uv run ldo-bench loadbank-check -c configs/demo_board.toml   # load bank straight on the supply: DIO -> J0..J4
uv run ldo-bench smoke          -c configs/demo_board.toml   # power up, VOUT at no / full load
uv run ldo-bench load-reg       -c configs/demo_board.toml   # --vin 2.0
uv run ldo-bench line-reg       -c configs/demo_board.toml   # --load 30mA --vin-from psu
uv run ldo-bench load-tran      -c configs/demo_board.toml   # --from 4mA --to 67mA --avg 32 --plot-us 100
uv run ldo-bench line-tran      -c configs/jafwak.toml       # --low 1.75 --high 1.85 --load 19mA
uv run ldo-bench psrr           -c configs/jafwak.toml       # --fstart 100 --fstop 1e6 --amp-mv 20
uv run ldo-bench iq             -c configs/jafwak.toml       # --method series --rail bgr
uv run ldo-bench enable-sine    -c configs/jafwak.toml       # --freq 1e3
uv run ldo-bench enable-startup -c configs/jafwak.toml       # --ch2 iin
uv run ldo-bench load-reg -c configs/jafwak.toml --vref ext  # JP2 removed: PSU OUT2 on VREF_IN
uv run ldo-bench compare results/load-reg/<bgr run> results/load-reg/<extref run>
```

Every command also takes:
- `--sim`: no hardware
- `--save-sim`: keep a simulated run, marked SIM
- `--yes`: skip the wiring prompt
- `-v`: debug log
- `--psu` / `--ad-serial`: pick an instrument when several are connected

The DC, transient, psrr and iq tests also take `--vref ext`. It drives VREF_IN from the supply's second output in place of the BGR; see [tests.md](docs/tests.md#external-reference-bgr-vs---vref-ext-jafwak).

## Configs

There is one TOML file per board in `configs/`:
- **`jafwak.toml`:** our LDO. VOUT 1.2 V, VIN nominal 1.8 V and **max 1.9 V**, load **max 20 mA** (codes 0–4).
- **`demo_board.toml`:** TPS7A7001 evaluation board, VOUT 1.0 V, plus the Switched_R_Load_Bank.

A config holds everything board-specific: the limits (`[dut]`, `[psu]`), the wiring (`[ad2]`, `[loadbank]`, `[enable]`), the instruments (`[dmm]`, `[xscope]`) and one section per test. It's validated before any instrument is touched:
- a misspelt key is an error
- no test can be configured to exceed `dut.vin_max`, `psu.v_max` or `dut.i_max`

Command-line options go through the same checks.

## Safety

- **Voltage limits:** the PSU and W1 can only reach the DUT through methods that enforce `dut.vin_max` and `psu.v_max`. The load bank refuses any code over `dut.i_max`.
- **Supply setup:** the current limit and OVP are set before the supply output turns on, and voltage changes are ramped.
- **Buffer interlock:** line-tran and psrr power the BUF634 rail above the LDO's VIN limit. Before doing that, they check at 1 V that the supply isn't on VIN, then, with the rail at `dut.vin_max`, that VIN follows W1 (which also proves the VIN probe is on VIN).
- **VIN margin:** with VIN driven through the buffer, every limit check keeps the AD2's measurement error (about 20 mV at 1.9 V, an unverified datasheet figure) below `dut.vin_max`.
- **OVP:** if the supply rejects the OVP setting, the run stops.
- **psrr peak:** psrr stops if the measured VIN peak goes over that margin.
- **External reference (`--vref ext`):**
  - **Prompt:** it asks every run whether JP2 is removed, even with `--yes`, since the BGR mustn't be back-driven.
  - **Turn-on:** the reference comes on only after VIN, then the run proves VOUT follows a 20 mV step (it stops if not: JP2 is still in).
  - **Limits:** it's never more than 0.3 V above VIN, and it goes to 0 V before VIN does.
- **Shutdown:** however a run ends (finished, error, Ctrl-C), everything is switched off in order: external reference, wavegen, AD2 V+, load bank, DIO, supply.

## Results: real data only

**Nothing saved or plotted is filled in or assumed.** Each test checks that it really measured the board: VOUT near nominal, the trigger channel actually stepping by the commanded amount, the stimulus reaching VIN, and the meter seeing the current.

When a check fails, the test still finishes and saves the real data, so the problem is obvious rather than hidden:
- the folder name ends in `_INVALID`
- the plot shows a red **SETUP PROBLEM** box saying what to check
- `summary.json` has `"valid": false`
- the terminal lists the problems
- the command exits with code 3

Readings below an instrument's resolution are reported as bounds ("< 4 µA", "≥ 72 dB").

Each run writes a folder under `results/<test>/`, e.g. `results/load-tran/20261106-164746_TPS7A7001-demo-board_load-tran/`:
- `data.csv`: every point
- `summary.json`: the headline numbers
- `meta.json`: config, instrument IDs, git commit, package versions
- `plot.png`: plus `plot_full.png` and `waveform_*.csv` for transients

`results/latest.png` is always the newest plot: keep it open in VS Code and it updates after each run. `results/` isn't in git, so runs stay on the machine that made them.

## Status (2026-11-06)

| | Hardware-tested |
|---|---|
| psu-check, loadbank-check, load-reg, line-reg, load-tran (demo board) | yes, valid results in [bench-log.md](docs/bench-log.md) |
| E3648A over GPIB (UsbGpib), 34401A over RS-232, AD2 DC / DIO / triggered capture | yes |
| line-tran, psrr, iq, enable-sine, enable-startup | simulator only |
| `--vref ext` (PSU OUT2 on VREF_IN) and `compare` | simulator only |
| 54621A, supply over RS-232, Prologix adapters | simulator / fake instruments only |
| any JAFWAK measurement | not yet |

## Development

```
uv run poe verify            # the gate: format check, lint, both configs, tests (no hardware)
uv run poe format            # format the code
uv run poe                   # list all tasks
```

Repo rules for people and AI agents are in [`AGENTS.md`](../AGENTS.md) at the repo root.

- **Simulator:** every test runs end to end on it (`instruments/sim.py`), including deliberately miswired cases. Drivers are tested against fake serial / VISA links.
- **Git:** work happens on the `ldo-bench` branch, one commit per step.

**macOS:** keep the repo and its `.venv` out of iCloud-synced folders (`~/Documents`, `~/Desktop`). iCloud hides files inside `.venv`, which breaks the install with `ModuleNotFoundError: No module named 'ldo_bench'`. It can also evict the repo's files when the disk is full.
