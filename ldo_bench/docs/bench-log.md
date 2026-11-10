# Bench log

What was built, what was measured, and what went wrong along the way. Newest session first.

## 2026-11-10: 54624A comms

First contact with the scope on the bench, an **Agilent 54624A** (4-channel; the driver covers it). It's on RS-232 at `/dev/cu.usbserial-ABB83HZJ`, alongside the 34401A (`/dev/cu.usbserial-ABB83W3S`, 9600 7E2) and the AD2. Nothing was powered and no DUT was connected.

**Measured:**
- **`discover`:** it found `AGILENT TECHNOLOGIES,54624A,MY40003464,A.02.20` at **9600 baud** 8N1. The config expects 57600.
- **Read-only check** (repo transport, `dsrdtr` flow): `:SYSTem:ERRor?`, timebase, channel and trigger queries all answered.
- **Waveform:** a 2000-point WORD `:WAVeform:DATA?` block came back whole, 4000 bytes as the preamble said. It took **5.1 s at 9600 baud**, so with two channels each transient edge spends about 10 s reading back.

**Problems:**
- **Leftover error:** the HP-style discovery probe sends `SYST:REM` / `SYST:LOC`, which the scope rejects with -113 "Undefined header". The driver's `*CLS` on open clears it, so it does no harm.
- **Supply missing at first:** the E3648A didn't appear because the UsbGpib wasn't on USB at all (its LED wasn't solid). Once the LED went solid, `discover` found all four instruments: the E3648A at `USB0::1003::8293::...::INSTR`, the 34401A, the 54624A and the AD2. It doesn't matter whether the adapter is plugged into the hub or straight into USB-C.
- **Config:** `xscope.baud` set to 9600 in both configs to match the scope (57600 on the scope would read back about 6x faster).

**First load-tran with both scopes** (demo board, VIN 2.0 V, codes 1 → 17 = 4 → 69 mA, 16 averages; `results/load-tran/20261110-105258_TPS7A7001-demo-board_load-tran`):
- **AD2: valid.**
  - **Step up:** peak −24.50 mV from 0.98632 V.
  - **Step down:** peak +21.13 mV from 0.97864 V.
  - **Noise:** 0.16–0.20 mV rms.
- **Settling: not measured.** VOUT moves ~7.7 mV between the two loads (load regulation), so it can't return to the ±10 % band around the pre-step level. `analysis.settle_reference` is now `"post"` in `demo_board.toml`.
- **54624A: no data.** It answers `*OPC?` with `+1` (checked scope-only: idle, after `:DIGitize`, and after a 4-average `:DIGitize`), and the driver accepted only `1`. Its first, correct "done" reply was rejected, the scope was marked busy, and the falling edge skipped it. The driver now accepts any numeric 1, and a reply it doesn't recognise gets its own error quoting what the scope said.

**Load-tran again after the fix** (same setup, `settle_reference = "post"`; `results/load-tran/20261110-110331_TPS7A7001-demo-board_load-tran`): valid, and both scopes captured both edges.

| | AD2 | 54624A |
|---|---|---|
| step up 4 → 69 mA: peak / settling | −25.17 mV / 15.60 µs | −23.51 mV / 15.97 µs |
| step down 69 → 4 mA: peak / settling | +21.31 mV / 164.10 µs | +19.34 mV / 164.21 µs |

- **Agreement:** settling times agree within 0.4 µs.
- **Lower peaks on the 54624A:** about 7–9 % lower. A guess: it has 2000 points across the 819 µs window (~410 ns/pt against the AD2's 100 ns). Not checked yet; a shorter `--window-ms` would test it.
- **Nominal VOUT:** 0.9862 V at 4 mA, 0.9784 V at 69 mA.
- **Slow step down:** it settles ~10× slower than step up. A guess: on unloading the LDO can't sink current, and only the 4 mA load discharges the output capacitor. Not checked against the board.
- **Wiring:** 54624A on plain BNC (1:1 probe setting, DC coupled), ch1 VOUT, ch2 DIO 4.
- **DC levels disagree, and with the DMM:**
  - **Pre-step VOUT:** 0.9862 V on the AD2 against 0.9996 V on the 54624A, while `load-reg` on 2026-11-06 had 0.9949 V on the DMM. With this offset the 54624A trace was mostly off the plot; it now has its own same-scale axis.
  - **Load shift:** both scopes see VOUT drop ~7 mV from 4 to 69 mA, where the DMM's 2.7 mΩ predicts ~0.2 mV. A guess: the scope grounds share the load-bank return path (about 0.1 Ω). If so, the "post" settling reference is hiding a wiring problem. To check: scope grounds at the GND pin by VOUT, or the DMM on VOUT at codes 1 and 17.
- **Window shift:** the 54624A's window started 82 µs (10 %) early. `REFerence LEFT` is 1 division in from the edge; the driver now allows for it.

**Load-tran with the VOUT grounds moved** to the GND next to VOUT (AD2 1− and the 54624A ch1 shield; `results/load-tran/20261110-111829_TPS7A7001-demo-board_load-tran`): valid.

| | AD2 | 54624A |
|---|---|---|
| step up 4 → 69 mA: peak / settling | −23.46 mV / 16.00 µs | −23.70 mV / 15.97 µs |
| step down 69 → 4 mA: peak / settling | +19.96 mV / 163.70 µs | +19.20 mV / 165.03 µs |
| VOUT at 4 mA / 69 mA | 0.99369 / 0.98705 V | 0.99946 / 0.99281 V |

- **What improved:**
  - **Noise:** halved (0.072 mV rms).
  - **Peaks:** the scopes now agree within 0.8 mV.
  - **AD2 DC level:** rose 7.5 mV, to within ~1 mV of the DMM's 0.9949 V.
- **Load shift still there:** both scopes see the same 6.6 mV between the two loads, so it's real at the probe points, not mainly the ground leads. It still disagrees with the DMM's 2.7 mΩ from 2026-11-06. Perhaps the DMM was on a different point. Next: `load-reg` with the DMM at the scope probe points.
- **Window fix confirmed:** the 54624A capture now starts at −163.8 µs, like the AD2.
- **Plot fix confirmed:** its trace overlays the AD2's on its own axis.

**Load regulation with the DMM at the scope probe points** (`load-reg`, VIN 2.0 V, codes 0–31; DMM, AD2 and 54624A each as close to the same VOUT / GND points as their connections allow; AD2 scope 2 moved to VIN; `results/load-reg/20261110-112647_TPS7A7001-demo-board_load-reg`): valid.
- **Readings:** VOUT 0.99488 V at no load to 0.98222 V at 122.4 mA. The slope is **103.0 mΩ** (R² 1.0000), 6.7 mV across 4 → 69 mA.
- **Shift explained:** this matches the 6.6 mV both scopes saw in load-tran, so the load shift is real at these points. No-load VOUT matches 2026-11-06 (0.99487 V) to 10 µV, but the slope there was 2.7 mΩ. So ~100 mΩ sits between where the regulator holds VOUT and today's probe points. A guess: the wiring to the load bank, with the probes on the load side of it.
- **Next:** probe at the demo board's output pins, before the load-bank wiring. Expect ~0.2 mV of shift, then `settle_reference = "pre"` again. The transient peaks so far include the ~0.1 Ω.
- **AD2 scope 1 off VOUT:** it read ~16 mV at every code, so it wasn't on VOUT this run (possibly disturbed when scope 2 was moved). The run is still valid because VOUT came from the DMM, but nothing flagged the 0.98 V disagreement between the AD2 and the DMM. Now caught: load-reg and line-reg mark a run invalid when the AD2's VOUT is more than 2 % + 50 mV from the DMM's.

**Load-reg again, scope 1 reconnected** (`results/load-reg/20261110-113126_TPS7A7001-demo-board_load-reg`): valid.
- **DMM:** 0.99486 → 0.98224 V over 0–122.4 mA, **102.9 mΩ** (R² 1.0000). It matches the previous run.
- **AD2 scope 1:** within 0.5–0.9 mV of the DMM at every code, slope 99.9 mΩ. The new agreement check passed on real data. It has been seen to flag a loose probe only on the simulator.
- **VIN at the board:** 1.985–1.991 V.
- **Conclusion:** ~0.1 Ω sits between the regulator's output and the probe points, and all three instruments agree on it. The regulator itself measured 2.7 mΩ on 2026-11-06.

## 2026-11-07: external reference (BGR vs 1.204 V)

The BGR on JAFWAK measures 1.17 V against 1.204 V in simulation. JP2 lets VREF_IN be driven from outside, so every test except the enable ones can now run with PSU OUT2 on VREF_IN (`--vref ext`). `ldo-bench compare` then lines that run up against the BGR run. See [tests.md](tests.md#external-reference-bgr-vs---vref-ext-jafwak). Simulator only so far.

**Decisions from the team:**
- **Source:** PSU OUT2.
- **Tests:** all but the two enable tests.
- **Gain:** VOUT = VREF.
- **Protection:** the BGR mustn't be back-driven, and the team chose no series resistor. So JP2 is confirmed by a person every run (`--yes` doesn't skip it), and the proof step stops the run if VOUT doesn't follow the reference.

**To check on the bench:**
- **JP2 pins:** which pin is VREF_IN and which is VREF_OUT, and which side TP3 is on. These were read from the schematic.
- **OUT2 limits:** whether the E3648A accepts a 5 mA limit and 1.5 V OVP on OUT2. If it refuses, the run stops; adjust `[vref]`.
- **Back-drive risk:** whether OUT2 at 0 V can sink current from the BGR. That matters only if JP2 is left in by mistake.
- **VREF_IN:** what else is on it (an RC filter?). It sets how long `vref.settle_time` must be.

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
- **Demo-board load bank** stays on its ideal resistor values (`loadbank.switch_resistance` unset), although `loadbank-check` measured about 0.3 Ω of switch resistance. The load-reg fit uses the estimated current (VOUT / R), so at the larger codes the currents read about 1.3 % high and the output resistance about 1.3 % low (2.7 mΩ is really about 2.74 mΩ). That's inside what the fit resolves, and it's the team's choice.

**Figures to check against datasheets** (recalled, not verified):
- the AD2's DC accuracy (`ad2.vin_offset_error`, `vin_gain_error`: ±10 mV ± 0.5 %)
- the 34401A resolution by NPLC (`procedures/iq.py` RESOLUTION)
- the TPS7A7001's minimum VIN

**Still open:**
- **Interlock proof on hardware:** can the BUF634 drive the proof levels (0.1 / 0.3 V) with its rail at 1.9 V? First real line-tran / psrr run will tell; adjust `proof_levels` if not.
- **BUF634 low side:** only its upper headroom is checked; V− looks like GND in the schematic (unconfirmed).
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
