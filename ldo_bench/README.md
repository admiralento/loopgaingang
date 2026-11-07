# ldo-bench

Bench automation for characterizing the LDO on the JAFWAK board. It covers load regulation, line regulation, load transient and line transient.

Hardware:
- Agilent E3648A (dual output) or E3638A power supply over GPIB (XyphroLabs UsbGpib adapter)
- Digilent Analog Discovery 2
- Agilent 34401A DMM (optional) on its RS-232 port through a USB-serial adapter. It measures VOUT in the DC tests.

## Setup

1. Install [uv](https://docs.astral.sh/uv/).
2. Install the [Digilent WaveForms](https://digilent.com/reference/software/waveforms/waveforms-3/start) runtime. It provides the AD2 driver library.
3. Run `uv sync` in this folder.
4. GPIB adapter (XyphroLabs UsbGpib, a USBTMC device):
   - **macOS / Linux:** nothing to install. pyvisa-py talks to it through the bundled libusb.
   - **Windows:** install NI-VISA, which supports USBTMC natively.
5. Run `uv run ldo-bench discover` to check that both instruments are found.

### UsbGpib adapter

The adapter only appears on USB when the power supply is **on** with its **GPIB interface enabled**. On the supply, set the I/O config on the front panel to GPIB/HP-IB, not RS-232.

The LED shows the adapter's state:
- **On:** connected to the instrument and visible on USB.
- **Blinking:** the instrument is off or its GPIB port is disabled. The adapter is not visible on USB.
- **Off:** no USB power.

The adapter finds the instrument's GPIB address on its own.

Close the WaveForms GUI before running tests, because only one program can open the AD2.

### 34401A DMM (RS-232)

On the meter's front panel (**Shift > I/O MENU**), set:
- INTERFACE: **RS-232**
- BAUD RATE: **9600** (or match `dmm.baud`)
- PARITY: to match `dmm.parity`. Our bench meter is on **EVEN: 7 BITS**, so the configs say `parity = "even"`. NONE: 8 BITS would be `"none"`.

The cable must be a **full-handshake null-modem** cable (TX/RX and DTR/DSR crossed, e.g. Agilent 34398A). A straight-through or 3-wire cable won't work: the meter won't transmit until it sees our DTR.

If the parity in the config is wrong, auto-discovery still finds the meter and tells you which setting to use. The script puts the meter in remote mode while it runs and returns it to local afterwards, so the front panel works again.

When `[dmm]` is enabled, the DC tests read VOUT from the DMM (10 NPLC, autozero, >10 GΩ input) and VIN from the AD2. The AD2's own VOUT reading is still saved as `vout_ad2_v` for comparison. Use `--no-dmm` or `dmm.enabled = false` to use the AD2 for VOUT instead.

```
uv run ldo-bench --help
uv run ldo-bench check-config -c configs/jafwak.toml       # custom LDO (1.2 V)
uv run ldo-bench check-config -c configs/demo_board.toml   # TPS7A7001 demo board (1.0 V)
```

There is one config per board. Each holds the wiring, load-bank pins and resistors, voltage limits, and test conditions.

### macOS: `ModuleNotFoundError: No module named 'ldo_bench'`

If the repo is in an iCloud-synced folder (e.g. `~/Documents`), macOS marks files inside `.venv` as hidden, and Python 3.12+ skips hidden `.pth` files. Fix it once by keeping the virtualenv outside the synced folder, with `.venv` as a symlink to it:

```
cd ldo_bench
rm -rf .venv
uv venv --python 3.12 ~/.venvs/ldo-bench
ln -s ~/.venvs/ldo-bench .venv
uv sync
```

After that, `uv run ldo-bench ...` works as usual. Windows is not affected.

## Tests

All commands take `-c <config>`. They also accept:
- `--sim`: no hardware
- `--psu` / `--ad-serial`: pick an instrument when several are connected
- `--yes`: skip the wiring prompt
- `-v`: debug log

```
uv run ldo-bench smoke     -c configs/demo_board.toml   # power up, read VOUT at no / full load
uv run ldo-bench psu-check -c configs/demo_board.toml   # PSU setpoint/readback vs DMM (DMM across the PSU only)
uv run ldo-bench loadbank-check -c configs/demo_board.toml  # load bank straight on the PSU: checks DIO -> J0..J4 wiring
uv run ldo-bench load-reg  -c configs/demo_board.toml   # load regulation (all 32 load codes)
uv run ldo-bench line-reg  -c configs/demo_board.toml   # line regulation + dropout (--load 30mA)
uv run ldo-bench load-tran -c configs/demo_board.toml   # load steps: peak + settling (--from 4mA --to 67mA --avg 32)
uv run ldo-bench line-tran -c configs/jafwak.toml       # VIN steps via W1 + BUF634 (--low 1.8 --high 2.0 --load 19mA)
```

**Everything saved and plotted is what was measured: nothing is filled in or assumed.** Each test runs to the end and checks that it really measured the board:
- **VOUT is near the board's nominal output:** within `dut.vout_tolerance`, 20 %, checked at every point.
- **For transients:** the trigger channel shows a step, and its swing is the commanded stimulus.
- **If the scope never triggers:** one untriggered capture records what is actually on the channels.

**When a check fails:**
- **Plot:** the problem is obvious there. VOUT is plotted in absolute volts against a red dashed "expected VOUT" line, with a red **SETUP PROBLEM** box saying what to check. Peak, band, settling and fit markers are left off, so nothing looks like a result.
- **Saved data:** the measured data is still saved. The folder name ends in `_INVALID`, `summary.json` has `"valid": false` plus the list of issues, the terminal prints them in red, and the command exits with code 3.

Safety limits still stop a run immediately: the line-transient interlock, `dut.i_max` and `dut.vin_max`.

`--sim` runs print the report marked SIMULATED and save nothing. With `--save-sim`, the folder ends in `_SIM` and the plot carries a SIMULATED banner.

Each run writes a folder under `results/<test>/`, e.g. `results/load-tran/20261106-143714_TPS7A7001-demo-board_load-tran/`:
- `data.csv`: every point
- `summary.json`: the headline numbers
- `meta.json`: config, instrument IDs, git commit, package versions
- `plot.png`

### Load bank check

Run this before load regulation. Connect the load bank **directly** to the supply: OUT1 + to J5, − to J6. Wire the AD2 DIO 0–4 to J0–J4, plus **AD2 GND to J6**. The J0–J4 headers carry only the gate signal, with no ground.

At 1.0 V, each bit is switched on alone and its extra current is compared with V/R. The no-load current is subtracted first, which cancels the supply's readback offset. The report says which bit draws nothing (open wire or missing ground), or which J header a DIO pin really seems to be wired to.

### Load regulation

The PSU drives VIN directly. The AD2 measures VOUT and VIN at the board, averaged over 0.1 s (a whole number of 50/60 Hz cycles), so drop in the supply cables doesn't count against the LDO.

Load current is estimated as VOUT / R from the bank resistors. The PSU current readback is also recorded; it adds the LDO's quiescent current. That readback costs about 0.8 s per point over GPIB. Set `load_reg.psu_readback = false` to skip it.

The result is the slope dVOUT/dI_load (mV/mA, equal to the output resistance) and the total ΔVOUT (mV and %).

### Line regulation

At a fixed load, VIN sweeps upward from `vin_start` to `vin_stop`. Starting below VOUT + dropout means the same run captures the dropout knee as well as the regulation slope.

- **Measured VIN:** the analysis uses measured VIN, never the setpoint. Where it comes from is set by `line_reg.vin_source` (or `--vin-from`):
  - `"ad2"`: scope ref channel at the board. This excludes cable drop.
  - `"psu"`: the supply's own readback. Only the DMM on VOUT is needed, no scope probes. It includes cable drop and the supply's ~1–3 mV readback error, which is negligible for the slope. The demo board config uses this.
- **Dropout:** the point where VOUT falls `dropout_fraction` (1 %) below its value at the highest VIN, interpolated between points.
- **Slope:** dVOUT/dVIN in mV/V and %/V, fitted from 3 steps above the knee (or from `fit_vin_min`).

### Load transient

The load bank steps between `code_low` and `code_high`. The AD2 triggers on scope channel 2, wired to a DIO pin that changes between the two codes (`trigger_pin`; on the demo board that's DIO 4 / J4, as in the WaveForms workspaces). Each edge is captured `averages` times. The captures are lined up on the trigger and averaged; the response repeats on every step, so this removes noise without blurring it.

The analysis is a Python port of `AD_workspaces/load_tran_script.js`, using the same `[analysis]` settings, so the numbers match the WaveForms custom measurements:
- **Nominal:** VOUT before the step.
- **Peak:** the largest dip or overshoot within 20 µs.
- **Settling band:** ±10 % of the peak deviation.
- **Settling time:** after the last 1 µs-smoothed sample outside the band.
- **Noise:** should be at most band / 3, or noise sets the settling time.

If VOUT's final level after the step is outside the band (DC load regulation), it can never settle back to the pre-step level. The report says so and suggests `settle_reference = "post"`.

The run folder also contains `waveform_rising.csv` and `waveform_falling.csv` (averaged and smoothed VOUT, plus the trigger channel).

### Line transient

VIN is driven by the AD2 wavegen through the board's BUF634 buffer:
- **Signal path:** W1 → VIN_FG / V_SIG SMA → BUF634 → VIN (JP4 set to the buffer).
- **Power:** the supply powers the **buffer rail** (VS_BUFF / VDD_BUF), not VIN. The rail must sit about 2 V above `vin_high`, which can be above the LDO's own VIN limit.
- **Interlock:** before raising the rail, the test powers it at only 1.0 V with W1 off and checks that VIN stays low. If VIN follows the supply, the leads are on VIN. The test then stops before applying the full rail voltage.
- **Calibration:** the buffer's offset is calibrated out from VIN measured at the board (scope ch2), so the steps land on `vin_low` / `vin_high` within 5 mV. If the buffer can't reach a level, the test says to check headroom, JP4 and the W1 wiring.
- **Measurement:** the scope triggers on VIN itself. Each edge is averaged and analysed like load transient. The report also gives the peak VOUT deviation per volt of VIN step (mV/V).

### Load current limit

`dut.i_max` caps the load any test may switch in. It's estimated at `vout_nominal`, and both the config check and the load bank enforce it. JAFWAK is limited to 20 mA (codes 0–4). `loadbank-check` lifts the limit, because no DUT is connected in that test.
