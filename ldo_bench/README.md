# ldo-bench

Bench automation for characterizing our LDO (the JAFWAK board) and the TPS7A7001 demo board. One command per test: it checks the config, tells you how to wire the bench, runs the measurement, then saves the data, a plot and a summary.

| Test | Command | Measures |
|---|---|---|
| Load regulation | `load-reg` | VOUT vs load current → output resistance |
| Line regulation | `line-reg` | VOUT vs VIN → mV/V and the dropout voltage |
| VREF vs VIN | `vref-line` | the BGR output (TP3) vs VIN → mV/V and the lowest VIN it works at (JAFWAK) |
| Load transient | `load-tran` | VOUT after a load step → peak deviation, settling time |
| Analyzer path calibration | `sa-path-cal` | the gain from U4's input to the 8591E (J10 out, W2 100 mV, LDO off) vs frequency, for `psrr --meter sa` |
| Spectrum analyzer check | `sa-check` | the HP 8591E on its own CAL OUT: commands, amplitude reading, narrowest RBW, noise floor. Analyzer only |
| Line transient | `line-tran` | VOUT after a VIN step (W1 a 20 Hz square through the BUF634; `--dc-steps` for DC steps) |
| PSRR | `psrr` | VIN ripple rejection, 100 Hz – 1 MHz |
| Quiescent current | `iq` | supply current enabled / disabled vs VIN |
| Enable thresholds | `enable-sine` | enable comparator VT+, VT-, hysteresis; `--shape square`: EN_AIN → EN_OUT propagation delay (JAFWAK) |
| Enable switching current | `enable-current` | EN_VDD current (DMM in place of J1 9-11) vs the frequency of a square on EN_AIN, 10 Hz – 1 MHz → charge per cycle (JAFWAK) |
| Startup | `enable-startup` | VOUT and EN_IN (AD2), input current from the 54621A's INA − INB across R13, after the enable edge (JAFWAK) |
| Temperature | `temp`, `temp --heater`, `temp-curve` | TMP235 (J13 pin 5), VREF and VOUT over time: a cold run (ice) and a hot run (the run ramps a heater on PSU OUT2) → VREF's curve against T (`curve.csv`, parabola fit), joined by `temp-curve` (JAFWAK) |
| Bench checks | `discover`, `psu-check`, `loadbank-check`, `smoke` | instruments found, supply accuracy, load bank wiring, quick power-up |
| Supply control | `psu-hold`, `psu-sweep` | hold a voltage and current limit until Ctrl-C (prints V / I, saves nothing); step the voltage and save V / I |
| BGR vs external reference | any DC, transient, psrr or iq test with `--vref ext`, then `compare` | the same test with 1.204 V on VREF_IN in place of the BGR (JP2 removed), side by side with the BGR run |

**Docs:**
- [docs/tests.md](docs/tests.md): each test's wiring, what it does, what it computes, its options.
- [docs/temperature.md](docs/temperature.md): how to run the temperature test (cold run, hot run, temp-curve), how long each takes, and how to get clean data.
- [docs/instruments.md](docs/instruments.md): setting up the supply, DMM, AD2 and 54621A, and the GPIB / RS-232 connections.
- [docs/architecture.md](docs/architecture.md): how the code is organised, the safety layer, the simulator, adding a test or an instrument.
- [docs/bench-log.md](docs/bench-log.md): what we built and measured on 2026-11-06, and the problems we hit.

## Hardware

- **Agilent E3648A** (dual output; an E3638A works too): VIN, or the BUF634 rail for line-tran / psrr. On GPIB through a XyphroLabs UsbGpib adapter.
- **Digilent Analog Discovery 2:** scope (VOUT, VIN / trigger, the TMP235 for `temp`), W1 (line-tran, psrr and enable-sine stimulus), DIO 0–4 (load bank), DIO 5–6 and V+ (JAFWAK enable).
- **Agilent 34401A:** VOUT in the DC tests, VREF (TP3) in `vref-line` and `temp`, and the `iq` measurement. On RS-232 through a USB-serial adapter.
- **Agilent 54621A** (optional): an independent capture of every transient edge.
- **Load banks:** binary-weighted resistors switched by FETs from AD2 DIO 0–4. JAFWAK has one on board (`load_cell.kicad_sch`); the demo board uses `Switched_R_Load_Bank`.
- **Spectrum analyzer:** coming, for PSRR past what the AD2 can resolve.

## Quick start

1. **Install** [uv](https://docs.astral.sh/uv/) and the [Digilent WaveForms](https://digilent.com/reference/software/waveforms/waveforms-3/start) runtime. On Windows, also install NI-VISA.
2. **Set up the environment:**
   ```
   cd ldo_bench
   uv sync
   uv run poe verify         # the gate: lint, both configs and the whole test suite, no hardware needed
   uv run poe sim-all        # every command end to end on the simulator, both boards
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
uv run ldo-bench check-config                            # validate the config, touch nothing

uv run ldo-bench psu-check      -c configs/demo_board.toml   # supply setpoint/readback vs DMM
uv run ldo-bench loadbank-check -c configs/demo_board.toml   # load bank straight on the supply: DIO -> J0..J4
uv run ldo-bench smoke          -c configs/demo_board.toml   # power up, VOUT at no / full load
uv run ldo-bench psu-hold --v 1.8 --ilim 5mA             # hold VIN until Ctrl-C (60 min hard timeout)
uv run ldo-bench psu-sweep --stop 1.8 --step 0.05        # --updown, --dwell 1, --to bench
uv run ldo-bench load-reg       -c configs/demo_board.toml   # --vin 2.0
uv run ldo-bench line-reg       -c configs/demo_board.toml   # --load 30mA --vin-from psu
uv run ldo-bench load-tran      -c configs/demo_board.toml   # --from 4mA --to 67mA --avg 32 --plot-us 100
uv run ldo-bench line-tran                               # --low 1.75 --high 1.85 --load 19mA
uv run ldo-bench psrr                                    # --fstart 100 --fstop 1e6 --amp-mv 20
uv run ldo-bench iq                                      # --method series --rail bgr
uv run ldo-bench vref-line                               # --high 1.9 --low 1.4 --step 0.02
uv run ldo-bench enable-sine                             # --freq 1e3, or --shape triangle --freq 1 (slow ramp)
uv run ldo-bench enable-sine --shape square --freq 1e5     # propagation delay, rise / fall at 100 MS/s
uv run ldo-bench enable-sine --output vout --shape triangle --freq 0.1   # VOUT tracking EN_AIN (J8 on LDO)
uv run ldo-bench line-reg --split-rails                  # dropout Method B: BGR_VDD / OTA_VDD on PSU OUT2
uv run ldo-bench enable-current                          # --fstart 10 --fstop 1e6 (EN_VDD current vs frequency)
uv run ldo-bench temp                                    # cold run (ice): --minutes 30 --interval 2 (Ctrl-C ends it, data kept)
uv run ldo-bench temp --heater                           # hot run: heater on PSU OUT2, 1 C/min to 60 C and back (~80 min)
uv run ldo-bench temp-curve results/temp/<cold run> results/temp/<hot run>   # curve.csv in a new folder
uv run ldo-bench enable-startup                          # --window-ms 10 --load 2 (needs the 54621A)
uv run ldo-bench load-reg --vref ext                     # JP2 removed: PSU OUT2 on VREF_IN
uv run ldo-bench load-reg --enable dio                   # EN driven from the AD2, not tied on
uv run ldo-bench compare results/load-reg/<bgr run> results/load-reg/<extref run>
```

Commands use `configs/jafwak.toml` unless `-c` (or `LDO_BENCH_CONFIG`) names another config, so the demo-board commands above pass `-c configs/demo_board.toml`. Every command prints the config it loaded first. Every command also takes:
- `--sim`: no hardware
- `--save-sim`: keep a simulated run, marked SIM
- `--yes`: skip the wiring prompt
- `-v`: debug log
- `--psu` / `--ad-serial`: pick an instrument when several are connected

The DC, transient, psrr and iq tests also take `--vref ext`. It drives VREF_IN from the supply's second output in place of the BGR; see [tests.md](docs/tests.md#external-reference-bgr-vs---vref-ext-jafwak).

`load-reg`, `line-reg`, `load-tran`, `enable-startup` and `temp` take `--ext-load-ohm 1200`: a fixed resistor on VOUT whose current is added to every load estimate (and to the `dut.i_max` check). See [tests.md](docs/tests.md#external-load---ext-load-ohm).

`smoke`, `load-reg`, `line-reg`, `load-tran`, `line-tran`, `vref-line` and `temp` also take `--enable dio`. The run then drives the LDO's enable from the AD2 (J6 7-8, J13 pins 12 / 10 / 14) instead of it being tied on with jumpers (J6 1-2): EN is low while VIN rises, high once VIN is up, and low again before anything turns off. See [tests.md](docs/tests.md#enable-from-the-ad2---enable-dio-jafwak).

## Configs

There is one TOML file per board in `configs/`:
- **`jafwak.toml`:** our LDO. VOUT 1.2 V, VIN nominal 1.8 V and **max 1.9 V**, load **max 21 mA** (codes 0–22: 1 mA steps from a load cell refitted 2026-11-14, top code 20.04 mA). Supply limit **10 mA**, 60 mA for the load tests (`high_current = true`).
- **`demo_board.toml`:** TPS7A7001 evaluation board, VOUT 1.0 V, plus the Switched_R_Load_Bank.

A config holds everything board-specific: the limits (`[dut]`, `[psu]`), the wiring (`[ad2]`, `[loadbank]`, `[enable]`), the instruments (`[dmm]`, `[xscope]`) and one section per test. It's validated before any instrument is touched:
- a misspelt key is an error
- no test can be configured to exceed `dut.vin_max`, `psu.v_max` or `dut.i_max`

Command-line options go through the same checks.

## Safety

- **Voltage limits:** the PSU and W1 can only reach the DUT through methods that enforce `dut.vin_max` and `psu.v_max`. The load bank refuses any code over `dut.i_max`.
- **Supply setup:** the current limit and OVP are set before the supply output turns on, and voltage changes are ramped.
- **Current limit:** `psu.current_limit` (JAFWAK: 10 mA) for every run. Only tests whose section has `high_current = true` (load-reg, line-reg, load-tran, smoke, loadbank-check) use `psu.high_current_limit` (JAFWAK: 60 mA). The config refuses any test whose load would reach the limit it runs with. `psu-hold` / `psu-sweep --ilim` can only go lower than `psu.current_limit`.
- **psu-hold:** stops on its own after 60 minutes at most (`psu_hold.max_minutes`), even without Ctrl-C.
- **Buffer interlock:** line-tran and psrr power the BUF634 rail above the LDO's VIN limit. Before doing that, they check at 1 V that the supply isn't on VIN, then, with the rail at `dut.vin_max`, that VIN follows W1 (which also proves the VIN probe is on VIN).
- **Split-supply U2** (`[buffer]`, JAFWAK after the U2 rework): U2's V− is on −5 V from PSU OUT2 wired reversed. W1 holds U2's input at DC 0 V before either rail comes up and is never turned off while they are up (a floating input sends U2's output to a rail). VS_BUFF and V− ramp together in 0.1 V steps with VIN read after each one; outside −0.1…+0.3 V both come down and the run stops. Shutdown keeps W1 at 0 V until both rails are off. `--vref ext` is refused for line-tran / psrr (it also needs OUT2).
- **VIN margin:** with VIN driven through the buffer, every limit check keeps the AD2's measurement error (about 20 mV at 1.9 V, an unverified datasheet figure) below `dut.vin_max`. W1 itself stays within `dut.vin_max` too, except in line-tran, which may take it up to `line_tran.w1_max` (≤ `dut.vin_max` + 0.2 V) to make up U2's drop under load; VIN keeps its limit.
- **OVP:** if the supply rejects the OVP setting, the run stops.
- **psrr peak:** psrr stops if the measured VIN peak goes over that margin.
- **External reference (`--vref ext`):**
  - **Prompt:** it asks every run whether JP2 is removed, even with `--yes`, since the BGR mustn't be back-driven.
  - **Turn-on:** the reference comes on only after VIN, then the run proves VOUT follows a 20 mV step (it stops if not: JP2 is still in).
  - **Limits:** it's never more than 0.3 V above VIN, and it goes to 0 V before VIN does.
  - **line-tran `--vref ad2`:** the AD2's V+ in place of PSU OUT2 (which is U2's −5 V), read back on the DMM on VREF_IN; `--vref-v` sets the level (default 1.204 V). V+ comes on only once VIN is up and measured, and goes off before VIN and U2's rails come down.
- **Shutdown:** however a run ends (finished, error, Ctrl-C), everything is switched off in order: heater, external reference, split rails and VS_BUFF (PSU OUT2), LDO enable (with `--enable dio`), wavegen, AD2 V+, load bank, DIO, supply output.
- **Supply after a run:** the output is off. The setpoint is `psu.idle_voltage` at `psu.current_limit`, so pressing Output On gives the normal VIN (JAFWAK: 1.8 V, 10 mA); without `idle_voltage` it is 0 V (demo board). The idle setpoint is programmed only once the output is confirmed off.
- **Temperature log:** `temp` runs for at most 2 hours (`temp.duration`); Ctrl-C ends it early, keeps the data and still shuts down. With `temp.vs_buff_source = "psu"` it powers VS_BUFF from PSU OUT2 (5.0 V, 100 mA limit, 5.5 V OVP; more than 30 mA stops the run); JAFWAK uses a separate supply.
- **Heater (`temp --heater`):** PSU OUT2 on a 1 Ohm power resistor: its resistance is checked at 0.2 V first (0.5-2 Ohm, else the run stops), then at most 9 W / 3.0 V (3.2 A limit, 3.5 V OVP), following 1 C/min to 60 C and back. Above 70 C, with the sensor lost, or with no rise while heating, the heater goes to 0 and the run stops. The heater can't be on without VIN (the outputs switch together), and every shutdown sets it to 0 V first.

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
- `plot.png`: plus `plot_zoom.png` (each edge on its own span), `plot_full.png` and `waveform_*.csv` for transients

`results/latest.png` is always the newest plot: keep it open in VS Code and it updates after each run. `results/` isn't in git, so runs stay on the machine that made them.

## Status (2026-11-14)

| | Hardware-tested |
|---|---|
| psu-check, loadbank-check, load-reg, line-reg, load-tran (demo board) | yes, valid results in [bench-log.md](docs/bench-log.md); load-reg's AD2 / DMM agreement check (2026-11-10) has only flagged on the simulator |
| E3648A over GPIB (UsbGpib), 34401A over RS-232, AD2 DC / DIO / triggered capture | yes |
| enable-sine `--shape triangle` (JAFWAK) | yes (2026-11-12), valid; the 1 kHz sine hit an AD2 arm race, fixed but not yet re-run |
| line-tran, psrr, iq, enable-startup (input current from the 54621A since 2026-11-14) | simulator only (line-tran / psrr on the split-supply U2 since 2026-11-13); line-tran on JAFWAK: valid runs 2026-11-19 with the trigger fix and the square (1 mA at 1.75-1.85 V; 18.3 mA only at 1.65-1.75 V, U2's drop puts 1.85 V out of reach), on a modified board (R6 lifted, U2 BW to V−); see bench-log |
| `--vref ext` (PSU OUT2 on VREF_IN) and `compare` | simulator only |
| `line-tran --vref ad2`, `--vref-v` (added 2026-11-20) | simulator only |
| HP 8591E: `ID?` / `REV?` / `SER?` (2026-11-20) | yes, over the UsbGpib adapter |
| `psrr --meter sa` (8591E on VOUT through U4, added 2026-11-20) | ran 2026-11-20, invalid (in-run path gain); now uses `sa-path-cal`'s curve: simulator only |
| `sa-path-cal` (added 2026-11-20) | simulator only |
| `sa-check`, the 8591E driver's other commands (added 2026-11-20) | yes (2026-11-20): every command accepted; calibrator −20.52 dBm at 300.003 MHz (−0.52 dB) |
| `vref-line`, `temp`, `enable-sine --shape square`, `--enable dio` (added 2026-11-13) | simulator only |
| `temp`: VS_BUFF from PSU OUT2 instead of a separate supply (added 2026-11-21) | simulator only; JAFWAK now uses `vs_buff_source = "external"` |
| `temp --heater`, the curve (`curve.csv`), `temp-curve` (added 2026-11-21) | yes (2026-11-21 evening): cold and hot runs valid, heater within 0.7 C of its target with the simulator's gains; results in `char_results/`, see bench-log |
| `line-tran --enable dio` (added 2026-11-17) | simulator only |
| `enable-current` (added 2026-11-13; replaces `iq --rail en`) | simulator only |
| `enable-sine --output vout`, `line-reg --split-rails`, `--ext-load-ohm`, vref-line's 10 GOhm / 10 MOhm check (added 2026-11-14) | simulator only |
| `psu-hold`, `psu-sweep`, the 10 mA / 60 mA split current limit (added 2026-11-13) | simulator only |
| JAFWAK load cell refitted (1200 / 680 / 330 / 150 / 82 Ω, 2026-11-14) | config updated; run `loadbank-check` before trusting the load estimates |
| 54624A over RS-232: load-tran cross-check (averaged capture, readback, time window), demo board | yes (2026-11-10), peaks within 0.8 mV of the AD2 |
| load-tran supply current check (VIN reaching the board), 54621A/54624A clipping check | yes (2026-11-10): passing, and failing with the supply leads unplugged |
| supply over RS-232, Prologix adapters | simulator / fake instruments only |
| any JAFWAK measurement | not yet |

## Development

```
uv run poe verify                        # the gate: format check, lint, both configs, tests (no hardware)
uv run poe sim-all                       # every command on the simulator, both boards
uv run poe test tests/test_vref.py -k jp2   # one file or test (extra arguments go to pytest)
uv run poe test-last                     # rerun only what failed last time
uv run poe format                        # format the code
uv run poe                               # list all tasks
```

`discover` isn't a task: it searches the real buses. The test suite can't touch hardware (any real bus search fails the test).

Repo rules for people and AI agents are in [`AGENTS.md`](../AGENTS.md) at the repo root.

- **Simulator:** every test runs end to end on it (`instruments/sim.py`), including deliberately miswired cases. Drivers are tested against fake serial / VISA links.
- **Git:** work happens on the `ldo-bench` branch, one commit per step.

**macOS:** keep the repo and its `.venv` out of iCloud-synced folders (`~/Documents`, `~/Desktop`). iCloud hides files inside `.venv`, which breaks the install with `ModuleNotFoundError: No module named 'ldo_bench'`. It can also evict the repo's files when the disk is full.
