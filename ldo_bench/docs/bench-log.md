# Bench log

What was built, what was measured, and what went wrong along the way. Newest session first.

## 2026-11-07: review panel and fixes

The five-reviewer panel (`.claude/reviewer-panel/`: manager, critic, algorithms, domain, test engineer) plus a refuter went over the whole branch. 58 of its 68 findings stood; all were fixed, with a test for each that failed before the fix. No hardware was run.

**Safety** (commit `4641522`):
- **Interlock:** the line-tran / psrr interlock could pass with the PSU on VIN *and* the VIN probe off, since both read ~0 V. It now also proves VIN follows W1, with the rail held at `dut.vin_max`, before raising it further.
- **VIN reads and margins:** VIN through the buffer is read on the AD2's 5 V range, and every limit keeps a margin for the AD2's DC error below `vin_max`.
- **`enable-sine`:** goes through a Bench method with a runtime limit.
- **OVP:** a rejected OVP setting stops the run.

**Results that could be wrong while looking right:**
- **Supply and links:** setpoints are read back and `OUTP OFF` is confirmed. Failed shutdown steps exit 1. Cut-off serial replies are rejected.
- **DC tests:**
  - VIN is checked against its setpoint
  - load-reg checks the load current against the supply's current
  - line-reg's knee no longer moves on one bad reading
  - fits with too few points are issues
- **PSRR:** the floor now sees mains hum and nearby tones.
- **Startup:** currents under the AD2's resolution are bounds, and unsettled edges are issues.
- **`iq`:**
  - the supply is switched off before the zero
  - reversed leads are an issue, not flipped
  - the floor follows the NPLC
  - the series method uses a fixed range
- **DMM range:** load-reg and line-reg record its range at each point and note an autorange switch inside the fit. That keeps the 1 µV range where it fits; JAFWAK's 1.2 V sits at the top of the 1 V range.

**Answers from the team:**
- **R1 on JAFWAK** is an output current-sense resistor, and the LDO has no feedback loop. VOUT measured at J13.3 is after R1, so the output resistance measured there includes R1's 0.1 Ω (1.9 mV at 19.2 mA); probe at OUTA (J13.4) to exclude it.
- **Reversed `iq` leads** are recorded as an issue (no sign flip).

**Figures to check against datasheets** (recalled, not verified):
- the AD2's DC accuracy (`ad2.vin_offset_error`, `vin_gain_error`: ±10 mV ± 0.5 %)
- the 34401A resolution by NPLC (`procedures/iq.py` RESOLUTION)
- the TPS7A7001's minimum VIN

**Still open:**
- **Interlock proof on hardware:** can the BUF634 drive the proof levels (0.1 / 0.3 V) with its rail at 1.9 V? First real line-tran / psrr run will tell; adjust `proof_levels` if not.
- **BUF634 low side:** only its upper headroom is checked; V− looks like GND in the schematic (unconfirmed).
- **Demo-board switch resistance:** set `loadbank.switch_resistance = 0.3`, as `loadbank-check` measured? Load currents currently read about 1.3 % high.
- **Coherent pickup in PSRR:** AD2 crosstalk is invisible to the floor; needs the spectrum analyzer or a shorted-input run.

## 2026-11-06: first session (`ldo-bench` branch)

### What we built

`ldo_bench/` went from nothing to the full tool in one day. In order, with the commits:

1. **Skeleton** (`515fc31`–`3518107`): uv project, TOML config schema, CLI, both board configs, load bank mapping, simulator, the Bench safety layer, and the E36xx and AD2 drivers.
2. **Getting the supply talking** (`5c32c00`–`8a3d153`):
   - UsbGpib (USBTMC) support with no drivers on macOS
   - E3648A dual-output support
   - the "set the supply to GPIB" hints
3. **DC tests and the DMM** (`c43d59f`–`ddfae16`):
   - load regulation and line regulation
   - the 34401A over RS-232, including the parity setting and DSR pacing
   - `psu-check`
   - line-reg can take VIN from the supply readback
4. **Load bank** (`5912e14`, `6f758f1`): `loadbank-check`, and demo-board load regulation at VIN = 2.0 V.
5. **Transients** (`340091a`–`f27c961`):
   - the WaveForms settling script ported to Python
   - load transient and line transient
   - the per-DUT load limit `dut.i_max` (JAFWAK: 20 mA)
   - the scope 2 trace under every transient plot
6. **Real data only** (`4196c47`–`1c598cc`): the validity model. Tests run to the end and save what they measured, but setup problems mark the run `_INVALID`. One results folder per test. See below for why.
7. **Usability** (`25911eb`–`62eee88`):
   - JAFWAK VIN max 1.9 V (nominal 1.8 V)
   - progress messages during discovery
   - full-capture plot and `--window-ms`
   - `results/latest.png`, kept up to date for VS Code
   - the 54621A cross-check
   - a common time span on transient plots and `--plot-us`
8. **Any interface** (`5db04ec`): every instrument works over VISA, UsbGpib, Prologix or its own RS-232 port.
9. **Enable block** (`fe546b7`): `enable-sine` and `enable-startup` for JAFWAK.
10. **Iq and PSRR** (`9c06b3f`): `iq` and `psrr`, ready for the spectrum analyzer.

### Measured: TPS7A7001 demo board

All runs below were valid (no setup issues). The run folders are on Alek's machine, under `results/`, which isn't in git.

**Supply check** (`psu-check`, DMM across OUT1; 5 of its 8 points shown):

| Set (V) | PSU readback (V) | DMM (V) | DMM − set (mV) | readback − DMM (mV) |
|---|---|---|---|---|
| 0.50 | 0.50012 | 0.499107 | −0.89 | +1.01 |
| 1.00 | 1.00069 | 0.999269 | −0.73 | +1.42 |
| 2.00 | 2.00159 | 1.999808 | −0.19 | +1.78 |
| 3.00 | 3.00253 | 3.000406 | +0.41 | +2.13 |
| 5.00 | 5.00389 | 5.001205 | +1.21 | +2.68 |

- **Output:** within spec (±(0.05 % + 10 mV)).
- **Readback:** reads 1–2.7 mV high. That's also within spec, but it's why VOUT never comes from the supply.

**Load bank** (`loadbank-check`, 1.0 V): passed after the bit 0 fix described below. Every bit is within tolerance of V/R, with a 0.23 mA baseline. Bits 3 and 4 read 1–2 % low, which fits about 0.3 Ω of FET plus trace resistance.

**Load regulation** (`load-reg`, VIN 2.0 V, all 32 codes, VOUT from the DMM):
- VOUT 0.99487 V at no load and 0.99453 V at 124 mA: ΔVOUT = 0.34 mV (0.035 %).
- Output resistance (the slope) is 2.7 mΩ (r² = 0.997).

**Line regulation** (`line-reg`, no load, VIN from the supply readback, VOUT from the DMM, 80 points up to 5.0 V):
- **Knee:** VOUT falls 1 % below its value at 5 V at VIN = 1.40 V (the tool reports 0.415 V "dropout"). At no load that is almost certainly the TPS7A7001's minimum operating VIN (about 1.4 V, to check against the datasheet), not dropout, which scales with load current. Measure dropout at a heavy load.
- **Line regulation:** 0.021 mV/V (0.002 %/V), fitted from 1.55 to 5.0 V.

**Load transient** (`load-tran`, VIN 2.0 V, 4 ↔ 69 mA, 16 averages):

| Edge | Peak deviation | Settling time |
|---|---|---|
| 4 → 69 mA | −16.4 mV | 21 µs |
| 69 → 4 mA | +14.6 mV | 207 µs |

### Measured: JAFWAK

Nothing valid yet. Two load-transient runs are marked `_INVALID`: the scope wasn't on VOUT (see "The plot that looked real" below). An earlier run labelled JAFWAK reads 0.994 V, which is the demo board's output, so it was the demo board on the JAFWAK config. Ignore it.

### Problems we hit, and what fixed them

**The supply didn't show up on USB.** The XyphroLabs UsbGpib adapter only enumerates when the instrument is **on with GPIB enabled**. A blinking LED means it can't see the instrument. Fix: set the supply's I/O config to GPIB / HP-IB. The tool now prints this hint whenever it can't find a supply.

**The DMM didn't answer, then answered in garbage.** There were three separate causes:
1. **Cable:** the meter needs a full-handshake null-modem cable. It won't transmit until it sees DTR.
2. **Parity mismatch:** the meter was on **EVEN: 7 BITS** while we spoke 8N1. Its replies decoded as junk (`H\xc5\xd7\xcc…` is `HEWLETT-PACKARD,34401A…` with the parity bit set), and its error log showed an RS-232 framing error. Fix: `dmm.parity = "even"`. Discovery now tries the other parity settings and tells you which one matched.
3. **Dropped commands:** even at the right parity, about half the commands got no reply when sent back to back. Fix: wait for the meter's DSR before each command, plus a 50 ms gap (`flow = "dsr-pacing"`).

**The load bank drew nothing at all.** The J0–J4 headers carry only the gate signal (both pins go to the gate). With no ground wire from the AD2, the gates had no reference. Fix: AD2 GND to J6. For any bit that draws nothing, `loadbank-check` now says to check that bit's gate wire and the AD2 ground.

**Bit 0 drew nothing.** Bits 1–4 were fine; bit 0 (DIO 0 → J0 → Q0, 250 Ω) wasn't.
- **Narrowing it down:** a "hold" script kept DIO 0 high on the bench while we probed, logging every change in supply current.
- **What the log showed:** bit 0 came on several times at exactly V/R (+4.0 mA), so R1 and Q0 were fine. The fault was an intermittent connection between DIO 0 and the Q0 gate: a solder joint.
- **After resoldering,** `loadbank-check` passed.

**The plot that looked real.** A JAFWAK load-transient run with nothing on VOUT produced a convincing transient plot.
- **What it really was:** scope 1 was floating. The "response" was the 3.3 V DIO edge coupling into the open input through stray capacitance.
- **Why nothing caught it:** a fallback in the code ("if VOUT reads ~0 V, use the nominal 1.0 V") hid the one reading that would have shown the probe wasn't on VOUT.
- **What we changed:**
  - the fallback is gone, and those runs were deleted
  - the rule now is that **nothing saved or plotted is filled in or assumed**
  - every test checks VOUT is near nominal, and that the trigger channel really stepped by the commanded amount
  - a run that fails these checks still finishes and plots the real data, but is marked `_INVALID` with a red SETUP PROBLEM box, so a bad setup is obvious

**The transient time axis looked wrong.** Each edge was zoomed to its own settling time, so the panels had different spans. The time base itself was right (10 MS/s × 8192 samples = 819 µs). Fix: one span for every edge, `--plot-us` to set it, and a `plot_full.png` of the whole capture.

**The repo lived in iCloud Drive.** Two problems came from that:
- macOS hid files inside `.venv`, and Python 3.12+ skips hidden `.pth` files, giving `ModuleNotFoundError: No module named 'ldo_bench'`.
- When the disk filled up, iCloud evicted the repo's files, `.git` included.

Fix: the repo now lives outside iCloud (`~/coding_projects/loopgaingang`), and the branch is pushed to GitHub. **Don't keep a git repo or a virtualenv in an iCloud-synced folder.**

### Still to do

- **Hardware runs not yet done:** line-tran, enable-sine, enable-startup (the AD2 digital trigger especially), iq, psrr, the 54621A, and the supply over RS-232.
- **Any valid JAFWAK measurement:** start with `smoke`, then `load-reg`.
- **Spectrum analyzer:** a driver once the part number is known, as a `PsrrMeter`.
- **Open questions about JAFWAK:**
  - Is the assumed enable signal path right?
  - What is the expected BGR VREF (for `startup.vref_nominal`)?
  - Are DIO 5 / 6 and AD2 V+ the right choice for the enable lines?
