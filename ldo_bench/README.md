# ldo-bench

Bench automation for characterizing the LDO on the JAFWAK board. It covers load regulation, line regulation, load transient and line transient.

Hardware:
- Agilent E3648A (dual output) or E3638A power supply over GPIB (XyphroLabs UsbGpib adapter)
- Digilent Analog Discovery 2

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

```
uv run ldo-bench --help
uv run ldo-bench check-config -c configs/jafwak.toml       # custom LDO (1.2 V)
uv run ldo-bench check-config -c configs/demo_board.toml   # TPS7A7001 demo board (1.0 V)
```

There is one config per board. Each holds the wiring, load-bank pins and resistors, voltage limits, and test conditions.

### macOS: `ModuleNotFoundError: No module named 'ldo_bench'`

If the repo is in an iCloud-synced folder (e.g. `~/Documents`), macOS marks files inside `.venv` as hidden, and Python 3.12+ skips hidden `.pth` files. Keep the virtualenv outside the synced folder:

```
export UV_PROJECT_ENVIRONMENT="$HOME/.venvs/ldo-bench"   # add to ~/.zshrc
```

Windows is not affected.

## Tests

All commands take `-c <config>`. They also accept:
- `--sim`: no hardware
- `--psu` / `--ad-serial`: pick an instrument when several are connected
- `--yes`: skip the wiring prompt
- `-v`: debug log

```
uv run ldo-bench smoke     -c configs/demo_board.toml   # power up, read VOUT at no / full load
uv run ldo-bench load-reg  -c configs/demo_board.toml   # load regulation (all 32 load codes)
```

Each run writes a folder under `results/`:
- `data.csv`: every point
- `summary.json`: the headline numbers
- `meta.json`: config, instrument IDs, git commit, package versions
- `plot.png`

### Load regulation

The PSU drives VIN directly. The AD2 measures VOUT and VIN at the board, averaged over 0.1 s (a whole number of 50/60 Hz cycles), so drop in the supply cables doesn't count against the LDO.

Load current is estimated as VOUT / R from the bank resistors. The PSU current readback is also recorded; it adds the LDO's quiescent current. That readback costs about 0.8 s per point over GPIB. Set `load_reg.psu_readback = false` to skip it.

The result is the slope dVOUT/dI_load (mV/mA, equal to the output resistance) and the total ΔVOUT (mV and %).

Work in progress: line regulation and the transient tests are next.
