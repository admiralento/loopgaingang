# Instruments: setup and connections

| Instrument | Role | How we connect it | Hardware-tested |
|---|---|---|---|
| Agilent E3648A (or E3638A) | VIN, or the BUF634 rail in line-tran / psrr; OUT2 on VREF_IN with `--vref ext` | GPIB through a XyphroLabs UsbGpib adapter | yes (GPIB); OUT2 as the reference: not yet |
| Digilent Analog Discovery 2 | scope (VOUT, VIN / trigger), W1 stimulus, DIO load bank and enable, V+ | USB (WaveForms runtime) | DC, DIO, triggered capture: yes. Digital trigger, sine, V+: not yet |
| Agilent 34401A | VOUT in the DC tests; Iq | RS-232 through an FTDI USB-serial adapter | yes (RS-232, DC volts) |
| Agilent 54621A | optional cross-check of every transient edge | RS-232 or GPIB | not yet |
| Spectrum analyzer | PSRR past the AD2's floor | to be decided (part number pending) | — |

None of the code is tied to a particular unit. Instruments are found by model (`*IDN?`) on whatever interface they're on, so leave `resource = ""` in the config unless you need to pin one down.

## Software setup

1. Install [uv](https://docs.astral.sh/uv/).
2. Install the [Digilent WaveForms](https://digilent.com/reference/software/waveforms/waveforms-3/start) runtime. It provides the AD2's driver library (`dwf.dll` / `dwf.framework`).
3. **Windows only:** install NI-VISA. On macOS and Linux nothing more is needed: pyvisa-py talks to the UsbGpib adapter through the bundled libusb.
4. In `ldo_bench/`: `uv sync`.
5. With the instruments on: `uv run ldo-bench discover -c configs/jafwak.toml` (`discover` is the one command that only uses a config when given `-c`).

Close the WaveForms GUI before running tests, because only one program can open the AD2.

## Interfaces: GPIB or RS-232, for every instrument

The supply, DMM and 54621A each work over any of these:

| Connection | `resource` | Notes |
|---|---|---|
| VISA GPIB (GPIB card, Keysight 82357B, NI GPIB-USB) | `GPIB0::5::INSTR` | needs NI-VISA / Keysight IO Libraries |
| XyphroLabs UsbGpib | `USB0::0x03EB::0x2065::…::INSTR` | one instrument per adapter; works with pyvisa-py on macOS |
| Prologix-style USB-GPIB | `prologix:COM5:5` | several instruments on one GPIB bus |
| The instrument's own RS-232 port | `serial:COM3`, `serial:/dev/cu.usbserial-…` | USB-serial adapter + null-modem cable |

**Searching** (`resource = ""`) goes in this order:
1. VISA
2. RS-232 ports, using that instrument's `baud` / `parity` / `stopbits` / `flow`. The supply and DMM are also tried at the other parity settings, and the result tells you which setting matched.
3. Prologix adapters, at every GPIB address.

Each scan runs once per run, and a port that one instrument has claimed is never probed for another. Progress is printed as it goes, so a slow search isn't silent.

**Built-in serial ports:** automatic search only probes USB serial adapters, so a PC's built-in COM port is never searched; name it explicitly (`resource = "serial:COM1"`).

**RS-232 specifics:**
- The serial settings are ignored on GPIB.
- On RS-232, the supply and DMM are put in remote mode for the run (`SYST:REM`) and handed back to the front panel afterwards (`SYST:LOC`).
- HP instruments pace RS-232 with DTR/DSR: `flow = "dsr-pacing"` waits for the instrument's DSR before each command, with a short gap. If DSR never comes (cable without the DTR/DSR pair), it prints a warning and carries on without the handshake.
- An HP instrument's RS-232 needs a **full-handshake null-modem** cable: TX↔RX and DTR↔DSR crossed (e.g. Agilent 34398A). A straight-through or 3-wire cable won't work, because the instrument won't transmit until it sees our DTR.

## E3648A / E3638A supply

- **Front panel:** set the I/O config to **GPIB / HP-IB** (not RS-232). The UsbGpib adapter finds the GPIB address by itself.
- **The adapter only appears on USB when the supply is on with GPIB enabled.** Its LED shows the state:
  - **On:** connected to the instrument and visible on USB.
  - **Blinking:** the supply is off or its GPIB port is disabled. Invisible on USB.
  - **Off:** no USB power.
- **E3648A outputs:** it's a dual-output supply. `psu.channel` picks OUT1 or OUT2, and the other output is held at 0 V. With `--vref ext` the other output drives JAFWAK's VREF_IN (`[vref]`), with its own 5 mA limit and OVP. `OUTP` switches both outputs together, so it sits at 0 V until the Bench turns the reference on.
- **Before the output turns on:** the current limit (`psu.current_limit`) and OVP (`psu.ovp`, which must be above `psu.v_max`) are set first.
- **Accuracy** (measured with `psu-check` on 2026-11-06):
  - **Output:** within 1.2 mV of the setpoint over 0.5–5 V.
  - **Readback:** reads 1–2.7 mV high. That's within spec, but as big as an LDO's whole load-regulation change. So VOUT always comes from the DMM or the AD2 at the board, never from the supply.
- **RS-232 on the supply:** the driver supports it (HP style: 2 stop bits, DTR/DSR), but it hasn't been tried on the bench.

## 34401A DMM

On the meter's front panel (**Shift > I/O MENU**), set:
- **INTERFACE:** **RS-232**
- **BAUD RATE:** **9600** (or match `dmm.baud`)
- **PARITY:** to match `dmm.parity`. Our meter is on **EVEN: 7 BITS**, so both configs say `parity = "even"`. NONE: 8 BITS would be `"none"`.

If the parity in the config is wrong, discovery still finds the meter and tells you which setting to use. A garbled reply is reported as a baud/parity mismatch instead of being read as a number.

How each test uses it:
- **DC tests:** the meter measures VOUT, at 10 NPLC, autozero on, >10 GΩ input. It autoranges, and load-reg / line-reg record its range at every point (`dmm_range_v`). A note appears if it switched range inside a fit. JAFWAK's 1.2 V sits at the top of the 1 V range (1 µV resolution, reads up to 1.2 V), so VOUT can cross into the 10 V range (10 µV). Set `dmm.range` to fix the range if that matters more than resolution. The AD2's own VOUT reading is saved next to it as `vout_ad2_v`. Use `--no-dmm` or `dmm.enabled = false` to take VOUT from the AD2.
- **`iq`, shunt method:** DC volts on the 100 mV range, across R13.
- **`iq`, series method:** DC amps (the I and LO jacks). Ranges 10 mA / 100 mA / 1 A / 3 A, or auto. An overload (+9.9E+37) is reported as such, not as a number.

## Analog Discovery 2

How the tests use each part of it:
- **Scope 1:** VOUT.
- **Scope 2:** the reference: VIN at the board, the DIO trigger line in load-tran, TP3 / R13 / R1 in enable-startup, or EN_AIN in enable-sine.
  - Ranges: 5 V (±2.5 V, the fine one) or 50 V.
  - DC accuracy: taken as ±10 mV ± 0.5 % on the 5 V range (`ad2.vin_offset_error`, `vin_gain_error`), recalled from the datasheet and **not yet checked**. It sets the margin kept below `dut.vin_max` when the wavegen drives VIN.
  - Samples: 8192 per channel, at up to 100 MS/s. The rate is rounded to 100 MHz / an integer, and the analysis always uses the rate the AD2 reports.
- **W1:**
  - DC through the BUF634 for line-tran
  - a sine for psrr (via the BUF634) and enable-sine (into EN_AIN)
  - limited to ±5 V
- **DIO 0–4:** load bank bits B0–B4, LSB first.
  - **Released pins** are tri-stated, not driven low. The load banks' 10 kΩ gate pull-downs then hold the load off.
  - **AD2 GND must go to the board / load bank ground.**
- **DIO 5 / 6:** EN_CTRL_33 and ~EN_CTRL_33 on JAFWAK's J13.
- **V+:** 3.3 V to V_33 (J13.14) for the enable level shifters. It's switched off at the end of every run.
- **Digital In trigger detector:** lets the scope trigger on the AD2's own DIO edge (enable-startup), so both analog channels are free for signals.

## Agilent 54621A cross-check scope

Optional: configure `[xscope]`, or skip it for a run with `--no-xscope`. If it's configured but can't be found, the run goes ahead on the AD2 alone and says so.

**Scope setup:**
- **Interface:** on the scope, **Utility > I/O**. On GPIB, note the address. On RS-232, set the baud rate to `xscope.baud` (57600 is fastest) and the handshake to **DTR** (`flow = "dsrdtr"`) or XON/XOFF. The port is fixed at 8N1; use a null-modem cable.
- **Ch1:** on VOUT, at the same point as AD2 scope 1.
- **Ch2:** on the same trigger signal as AD2 scope 2: the DIO pin for load-tran, VIN for line-tran.

**How a capture works:**
- **Arming:** for each edge the scope is armed first. Its timebase matches the AD2 window, ch1 is at `vout_range` centred on the nominal VOUT, it edge-triggers on ch2, and it averages as many triggers as the AD2 takes captures.
- **Collection:** the AD2 steps the load as usual, and every step also triggers the scope. If the scope missed triggers, the step is repeated until its average completes. Both channels are then read back as 16-bit WORD data.
- **Results:** on the VOUT plot as "54621A" (the model it found), with its peak and settling time compared in the report, and `waveform_xcheck_*.csv`.
  - **Own axis:** it's drawn against a right-hand axis at the same V/div as the AD2. That axis is offset by the difference between the two scopes' pre-step VOUT, so the traces line up. Neither scope is DC-accurate to a few mV at 20 mV/div (on the bench the 54624A read ~14 mV above the AD2, and both differed from the DMM), so take absolute VOUT from the DMM. The CSVs hold each scope's readings unchanged.
  - **Time window:** `:TIMebase:REFerence LEFT` is one division in from the left edge on this series; the driver allows for it so the scope's window starts `pre_trigger` before the step, like the AD2's.

**When the scope has a problem:**
- **It never affects the AD2 result's validity**, whether the scope is missing, on the wrong signal, or never completes its average. The problem is listed under the cross-check only.
- **Clipped VOUT:** if any VOUT sample is within 1 % of full scale of the top or bottom of its screen (`vout_nominal ± xscope.vout_range / 2`), the cross-check is flagged as clipped and gives no peak or settling time. This covers VOUT being off screen (flat on the edge; on the bench, VOUT at 0 V read as a flat 0.92 V, inside the ±20 % VOUT tolerance) and a response bigger than the screen.
- **If it never completes,** it's left waiting for triggers. Press **Run/Stop** to free it.

The driver follows the Agilent 54620-Series Programmer's Guide (pub. 54622-97038), but hasn't been run against a real scope yet.

## Spectrum analyzer (coming)

The part number isn't known yet. The PSRR test is ready for it: VOUT is measured through a `PsrrMeter` interface in `procedures/psrr.py`, and [architecture.md](architecture.md#recipes) describes what a spectrum analyzer version needs to do.
