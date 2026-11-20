# The tests

Every test is one command:

```
uv run ldo-bench <command> -c configs/<board>.toml [options]
```

Every command also takes:
- `--sim`: no hardware, the simulator. Saves nothing unless `--save-sim`.
- `--yes`: skip the wiring prompt.
- `-v`: debug log.
- `-o <folder>`: results folder (default `results/`).
- `--psu` / `--ad-serial`: pick an instrument when there are several.
- `--vref bgr|ext`: the reference VOUT follows. `ext` uses the supply's second output in place of the BGR (JAFWAK, JP2 removed). Not on the enable or bench-check tests; see [External reference](#external-reference-bgr-vs---vref-ext-jafwak).
- `--enable static|dio` (smoke, load-reg, line-reg, load-tran, line-tran, vref-line, temp): how the LDO is enabled. `static` (default): tied on with jumpers. `dio`: driven from the AD2; see [Enable from the AD2](#enable-from-the-ad2---enable-dio-jafwak).

Before powering anything up, each command prints the wiring it expects and asks you to confirm it: one connection per line, scope 1+ and 1− on one line, scope 2 on its own, named by the board's silkscreen label and connector pin (`[board]` in the config). The lists below summarise it; run a command with `--sim` to see exactly what it prints. Option values in the examples are just examples; defaults come from the board's config section, which is named under each test.

How a run decides it measured the board, and what `_INVALID` means, is covered in [architecture.md](architecture.md#real-data-only-the-validity-model).

**Hardware status (2026-11-06):**
- **Run on real hardware, demo board:**
  - `psu-check`
  - `loadbank-check`
  - `load-reg`
  - `line-reg`
  - `load-tran`
- **JAFWAK:** no valid run yet. Its load-transient runs had the scope off VOUT (see [bench-log.md](bench-log.md)).
- **Simulator only so far:** `line-tran`, `enable-startup`, `iq` and `psrr` (on the split-supply U2 too); `vref-line`, `temp`, `enable-sine --shape square`, `enable-current` and `--enable dio` (added 2026-11-13); `enable-sine --output vout`, `line-reg --split-rails`, `--ext-load-ohm` and vref-line's DMM loading check (added 2026-11-14). `enable-sine --shape triangle` ran on JAFWAK on 2026-11-12.

Suggested order for a first session on a board:
1. `discover`
2. `psu-check`
3. `loadbank-check`
4. `smoke`
5. `load-reg`
6. `line-reg`
7. `load-tran`
8. `line-tran`, then the rest

---

## Bench checks

### `discover`
Lists every instrument it can reach over VISA (GPIB, USB), RS-232 and Prologix adapters, plus any Analog Discovery 2. Pass `-c <config>` to probe RS-232 ports with that config's serial settings. Nothing is powered or changed.

### `psu-check`: is the supply telling the truth?
- **Wiring:**
  - DMM V+ / COM -> PSU OUT1 + / -, directly
  - Nothing else connected to the PSU output
- **Does:** steps the output through `psu_check.points` (0.5–5 V). At each point it compares the setpoint, the supply's own readback and the DMM. The errors are judged against the E3648A's published accuracy.
- **Why:** the regulation tests care about millivolts. Today's run showed the output is within 1.2 mV of the setpoint, but the readback reads 1–2.7 mV high. That's why VOUT is never taken from the supply.
- **Setup check:** a DMM reading nowhere near the setpoint means the DMM isn't across the output.
- **Config:** `[psu_check]`.

### `loadbank-check`: does each DIO pin switch the right resistor?
- **Wiring:** the load bank straight on the supply, no LDO.
  - `--current-from dmm` (default): PSU OUT1 + -> DMM I (3A/mA jack), DMM LO (COM) -> load bank VIN (J5); PSU - -> load bank GND (J6)
  - `--current-from psu`: PSU OUT1 + -> load bank VIN (J5), PSU - -> load bank GND (J6)
  - AD2 DIO [0, 1, 2, 3, 4] -> load bank J0..J4
  - AD2 GND -> load bank GND (the FET gates are driven relative to it)
- **Does:** at `loadbank_check.voltage` (1.0 V by default; 1.2 V on JAFWAK, where B4's 82 Ω draws 14.6 mA, over the 10 mA normal limit, so it uses the 60 mA high limit), it reads the current with every bit off. Then it turns on each bit alone and compares the extra current with V/R for that bit.
  - **Current from the DMM** (default): the 34401A on DC amps in series, on its 1 A range. I believe that range's burden is ~0.1 Ω, versus ~5 Ω on the 10 / 100 mA ranges, which would drop ~70 mV at 15 mA; both figures are from memory, not checked. The supply's readback is recorded beside it, and a note says if it is more than 5 % from the DMM. `--current-from psu` uses the supply's readback alone.
  - The no-load current is subtracted first, which cancels the meter's offset.
  - **Pass:** within 10 % of V/R, plus 0.05 mA with the DMM or 0.3 mA with the supply's readback (its repeats scattered ~0.05 mA on JAFWAK). That is small enough that a dead 1 mA B0 fails; the old 1.5 mA let it pass.
  - `dut.i_max` doesn't apply here, since no DUT is connected.
- **Diagnoses:**
  - a bit that draws nothing (open wire, missing ground, bad solder)
  - a DIO pin that seems to be wired to a different J header
  - every bit off by the same factor (within 5 %): not wiring but the voltage at the bank or the current reading. JAFWAK read 64–65 % on every bit on 2026-11-14, which the old diagnosis reported as swapped wires
- **Note:** J0–J4 carry only the gate signal, with no ground. Without the AD2 GND wire nothing switches.
- **Config:** `[loadbank_check]` (`high_current = true` on JAFWAK: see [Supply current limit](#supply-current-limit)).

### `smoke`
Powers VIN up (default `load_reg.vin`), then reads VOUT, VIN and the supply's V and I at no load and at the largest allowed load, and powers down. Prints a table; saves nothing. Use `--vin` to set VIN. `[smoke] high_current = true` runs it on `psu.high_current_limit`.

### `psu-hold`: the supply at a set voltage until Ctrl-C
```
uv run ldo-bench psu-hold --v 1.8 --ilim 5mA            # --to bench, --interval 2, --minutes 30
```
- **Wiring:** `--to vin` (default): PSU OUT1 on the board's DC input, as in the DC tests. `--to bench`: nothing but your own load or meter on the supply.
- **Does:** sets the current limit (`--ilim`, at most `psu.current_limit`; default `psu.current_limit`) and OVP, turns the output on at 0 V, ramps to `--v` and holds it. Every `--interval` seconds it prints the supply's own V and I readback. Ctrl-C ends it and the output turns off; so does the hard timeout, `--minutes` / `psu_hold.max_minutes` (at most 60).
- **Ceiling:** `--to vin`: min(`dut.vin_max`, `psu.v_max`), 1.9 V on JAFWAK. `--to bench`: `psu.v_max`.
- **Saves nothing:** it prints the readings and a summary (V and I min / mean / max).
- **Flags:** a current at or above `near_limit` (90 %) of the limit is printed NEAR LIMIT: the supply may be in constant current, below the setpoint. This is judged from the readback; the supply's CV / CC mode isn't queried. A voltage readback more than `v_tolerance` (50 mV) off the setpoint while the current isn't near the limit is a setup problem (exit 3): OVP tripped, output off from the front panel, or the wrong output.
- **Config:** `[psu_hold]`.

### `psu-sweep`: step the supply, read V and I
```
uv run ldo-bench psu-sweep --start 0 --stop 1.8 --step 0.05      # --updown, --dwell 1, --ilim 5mA, --to bench
```
- **Wiring, ceiling, current limit, flags:** as `psu-hold`.
- **Does:** steps from `--start` to `--stop` (down if stop is below start), and back with `--updown`. At each setpoint it waits `--dwell` seconds and reads the supply's V and I. Ctrl-C stops it and keeps the points so far (`summary.json`: `complete: false`).
- **Saves:** `data.csv` (direction, setpoint, V and I readback, near_limit), `summary.json`, and a plot of I against setpoint with readback minus setpoint below it.
- **Config:** `[psu_sweep]`.

### Supply current limit
- **Every run** sets `psu.current_limit` before the output first turns on (10 mA on JAFWAK). The limit is printed at the start of the run and saved in `meta.json` as `psu_current_limit_a`.
- **Higher limit:** a test whose section has `high_current = true` uses `psu.high_current_limit` instead (60 mA on JAFWAK, approved 2026-11-13). The tests that can opt in are `load-reg`, `line-reg`, `load-tran`, `smoke` and `loadbank-check`; on JAFWAK all five do, since they switch in up to 19.3 mA (loadbank-check: 14.6 mA for B4 alone).
- **Config check:** no test may be configured to draw at or above the limit it runs with: its heaviest load-bank code at `vout_nominal` (loadbank-check: `voltage` over the smallest resistor). This estimate leaves out the LDO's own few hundred µA. `line-tran` and `psrr` are separate: they power the buffer rail with `buffer.rail_current_limit`.

---

## DC tests

On JAFWAK every wiring printout with the chip powered also lists the J1 ON jumpers (1-3, 5-7, 9-11, 13-15) and JP2 (`[board] standard_jumpers`), except a jumper the test changes itself: the DMM in its place (`iq --method series`, `enable-current`), Method B (`line-reg --split-rails`), or JP2 removed (`--vref ext`). The wiring lists below leave them out.

### `load-reg`: load regulation
- **Wiring (demo board):**
  - PSU -> VIN (DC input, not through the buffer)
  - Scope 1+ -> VOUT at the output pins, 1- -> GND; DMM V+/COM -> VOUT / GND at the output pins (DMM is the VOUT reference)
  - Scope 2+ -> VIN at the board, 2- -> GND
  - DIO [0, 1, 2, 3, 4] -> load bank bits B0..B4
- **Does:** holds VIN at `load_reg.vin` and steps the load bank through every allowed code. At each code it reads:
  - VOUT from the DMM (10 NPLC; the AD2 if there's no DMM)
  - VIN at the board from AD2 scope 2, averaged over 0.1 s (a whole number of 50/60 Hz cycles)
  - optionally, the supply's V/I readback (about 0.8 s per point)
- **Computes:**
  - **Load current:** estimated as VOUT / R from the bank resistors. On JAFWAK the probes are at OUTA (J13 pin 4, TP4), before R1, so R1 isn't in the result. VOUT measured at J13 pin 3 (OUTB) would be after R1 (the 0.1 Ω output current-sense resistor; there's no feedback loop), and the output resistance would include R1's 0.1 Ω. On the demo board, R5 (100 mΩ, 2512) is in series between `/LDO_VOUT` (U1's output, where the feedback divider connects) and `/VOUT`. TP4 "OUT_S2" is on `/LDO_VOUT` (excludes R5); TP3 "OUT_S1", J8 (SMA) and J2 are on `/VOUT` (include it). Read from the PCB file on 2026-11-10; confirm on the board.
  - **Output resistance:** the slope dVOUT/dI (mV/mA, equal to the output resistance).
  - **Total change:** ΔVOUT over the range (mV and %).
- **Setup checks:**
  - VOUT within `dut.vout_tolerance` (20 %) of nominal at every point
  - VIN at the board within 2 % + 20 mV of the setpoint (probe on the wrong node, supply in current limit)
  - with the DMM: AD2 scope 1 within 2 % + 50 mV of the DMM at every point (scope 1 off VOUT; it read 16 mV on the bench while the DMM read 0.99 V)
  - with `psu_readback` on: the supply's current must rise by what each code should draw, within `loadbank_check`'s tolerances. A dead bit or a missing ground would otherwise look like perfect regulation. With it off, the report notes that the load current is only the bank estimate.
- **Options:**
  - `--vin 2.0`
  - `--no-dmm`
  - `--ext-load-ohm 1200` (see [External load](#external-load---ext-load-ohm))
- **Config:** `[load_reg]`.

### `line-reg`: line regulation and dropout
- **Wiring (demo board, `vin_source = "psu"`):**
  - PSU -> VIN (DC input, not through the buffer)
  - DMM V+/COM -> VOUT / GND at the output pins (no scope probes needed)
  - DIO [0, 1, 2, 3, 4] -> load bank bits B0..B4, AD2 GND -> board GND
- **Does:** at a fixed load, sweeps VIN upward from `vin_start` to `vin_stop`. Starting below VOUT + dropout means one run captures both the dropout knee and the regulation slope.
- **Measured VIN:** the analysis always uses measured VIN, never the setpoint. Where it comes from is set by `vin_source` (or `--vin-from`):
  - `"ad2"`: scope 2 at the board, which excludes cable drop.
  - `"psu"`: the supply's own readback. It needs no scope probes, but includes cable drop and the 1–3 mV readback error. That's negligible for the slope.
- **Computes:**
  - **Dropout:** where VOUT, coming up from the bottom of the sweep, first rises to within `dropout_fraction` (1 %) of its value at the highest VIN, interpolated between points. At light load this knee can be the part's minimum operating VIN rather than true dropout (the demo board's 0.415 V at no load is likely the TPS7A7001's VIN minimum, about 1.4 V, not yet checked against the datasheet); measure dropout at a heavy load.
  - **Line regulation:** dVOUT/dVIN in mV/V and %/V, fitted from 3 steps above the knee (or from `fit_vin_min`).
- **Setup checks:**
  - VOUT near nominal wherever there's headroom
  - measured VIN within 2 % + 20 mV of the setpoint
  - with the DMM and `vin_source = "ad2"`: AD2 scope 1 within 2 % + 50 mV of the DMM (with `"psu"` the AD2 isn't read)
  - a VOUT reading below the dropout threshold *above* the knee (a bad reading: the knee is taken from the bottom of the sweep, so it doesn't move)
  - fewer than 2 points to fit, or `fit_vin_min` below the dropout point
- **Is it the BGR?** When a knee is found the report points to `vref-line`'s BGR minimum VIN: a knee at or above it means the BGR, not the pass device, sets the dropout. `--split-rails` takes the BGR out of it.
- **Dropout Method B (`--split-rails`, JAFWAK, `[split_rails]`):** BGR_VDD and OTA_VDD come off INB and onto the PSU's other output at 1.8 V, so as VIN sweeps only the pass device drops out (VDD and EN_VDD stay on VIN). Wiring on top of the usual:
  - Remove the J1 1-3 jumper (BGR_VDD) and the J1 13-15 jumper (OTA_VDD)
  - PSU OUT2 + -> J1 pin 1 (BGR_VDD); PSU OUT2 + -> J1 pin 13 (OTA_VDD); PSU OUT2 - -> GND (J11)
  - J1 5-7 (VDD) and 9-11 (EN_VDD) stay fitted
  - Order and limits: VIN comes up first; the rails follow; VIN never goes below `vin_floor` (1.0 V) while they are up; they come down before VIN at the end and in every shutdown, so BGR / OTA are never powered without VDD. More than `max_current` (1 mA) from OUT2 once it is up means a J1 1-3 / 13-15 jumper is still fitted (OUT2 fighting VIN through INB): the rails come down and the run stops. Refused with `--vref ext` (same output). J1's pin nets (1-2 BGR_VDD, 3 INB, 13-14 OTA_VDD, 15 INB) are from the KiCad file: check them on the board.
- **Options:**
  - `--load 30mA` (or a code)
  - `--vin-from psu`
  - `--no-dmm`
  - `--split-rails` (Method B)
  - `--ext-load-ohm` (on JAFWAK, `--load 1` already gives 1 mA)
- **Config:** `[line_reg]`, `[split_rails]`.

### `vref-line`: VREF against VIN (JAFWAK)
- **Wiring:**
  - PSU + -> VIN_DC (J7), PSU - -> GND (J11); JP4 2-3 "DC"; JP2 closed (BGR drives VREF_IN)
  - DMM V / COM -> VREF (TP3) / GND (TP2); Hi-Z input (10 GOhm) if the meter has it
  - Scope 1+ -> J13 pin 4 (OUTA), Scope 2+ -> J13 pin 1 (INA); both - -> J13 pin 6 (GND)
  - LDO enabled: J6 1-2 "HI" (EN_AIN = VIN), J8 1-2 and 9-10 "LDO" (or `--enable dio`)
- **Does:** with the LDO on, steps VIN from `vin_high` (1.90 V) down to `vin_low` (1.40 V) in `vin_step` (20 mV), waits `settle_time`, and reads VREF on the DMM, VOUT and VIN on the AD2. This is the first bring-up plan's Test 2.7.
- **Computes** (the line-reg analysis, applied to VREF):
  - **Line sensitivity:** dVREF/dVIN (mV/V), fitted from 3 steps above the knee to the top.
  - **BGR minimum VIN:** where VREF first falls `drop_fraction` (1 %) below its value at `vin_high`. Simulated ~1.56 V. Below it the BGR, not the pass device, can set the LDO's dropout (compare with `line-reg`).
  - **VOUT − VREF** at the top: the regulator follows VREF at unity gain. VOUT is on the AD2, so its DC error (~10 mV, unverified) is in this number.
  - **DMM loading** (bring-up plan Test 3, "VREF level"): before the sweep, at `vin_high`, VREF is read with the DMM's input at >10 GOhm and again at 10 MOhm (`INP:IMP:AUTO`). The difference gives the BGR's output resistance; more than 0.5 mV is a note (the sweep's 10 GOhm readings are the ones to trust). The input goes back to 10 GOhm for the sweep.
- **Setup checks:**
  - VREF at `vin_high` within `vref_tolerance` (20 %) of `vref_nominal` (1.204 V): DMM on TP3, JP2 closed, LDO on
  - VOUT on the AD2 near nominal at `vin_high` (LDO enabled, scope 1 on OUTA)
  - measured VIN within 2 % + 20 mV of each setpoint
  - VREF below the knee threshold above the knee (a bad reading), or too few points to fit
- **Needs the DMM** (`[dmm]` enabled): it stops before anything powers up otherwise.
- **Options:** `--high 1.9 --low 1.4 --step 0.02`, `--vin-from psu`, `--enable dio`.
- **Config:** `[vref_line]`.

### `iq`: quiescent and shutdown current
- **Wiring (JAFWAK, shunt method):**
  - PSU -> VIN_DC (JP4 to DC); JP2 fitted
  - DMM V (HI) -> J13.1 (VIN), COM -> J13.2 (INB): across R13. The DMM floats, so no ground lead; nothing else on the V terminals
  - J1 ON column, all four (BGR_VDD 1-3, VDD 5-7, EN_VDD 9-11, OTA_VDD 13-15)
  - Scope 1+ -> J13 pin 4 (OUTA), Scope 1- -> J13 pin 6 (GND) (VOUT check; draws ~1 uA)
  - Load check (shunt, enabled): fit J10 "LOAD"; AD2 DIO 0-4 -> J13 pins 7, 9, 11, 13, 15 (B0-B4). The load cell doesn't need VS_BUFF
  - Enable path: J6 7-8 (EN_CTRL_18 -> EN_AIN), J8 1-2 (EN_OUT -> EN_IN), J8 9-10 (~EN_OUT -> ~EN_IN)
  - AD2 DIO 5 -> J13.12 (EN_CTRL_33), DIO 6 -> J13.10 (~EN_CTRL_33); AD2 V+ -> J13.14 (V_33)
  - AD2 GND -> board GND
- **Two methods** (`iq.method`, `--method`):
  - **`shunt`:** DMM DC volts across R13 (0.1 Ω) on the 100 mV range. This is everything drawn from VIN: the LDO, BGR, enable block, the level shifters' low side, and ~1.2 µA into the AD2 scope on VOUT. One count is 1 µA, which is fine for Iq but too coarse for shutdown current.
  - **`series`:** DMM DC amps (I and LO jacks) in place of one J1 jumper. The I jack goes on the INB pin (J1 3, 7, 15) and LO on the rail pin (1, 5, 13): current flows from INB into the rail, so the other way round reads negative (an issue). Until 2026-11-14 the wiring printout had them the other way round. Rails: `--rail bgr|vdd|ota` (in place of that J1 jumper; there is no EN_VDD rail: its ~0.3 nA is under the 10 nA count, so `enable-current` measures it), or between the PSU and VIN_DC (`--rail total`). It uses a fixed range, 10 mA unless `dmm_range` says otherwise, so the zero and every point are read on the same range. One count is 10 nA, so this resolves shutdown current. The meter's burden (~5 Ω on the 10 mA range) drops a little of that rail's voltage.
- **Does:**
  1. Switches the supply output off (it may have been left on), then reads the DMM: the zero (offset, thermal EMF). It's subtracted from every point. The spread of those readings (at least 3) sets the floor, never below one DMM count at the chosen NPLC (1 µV on 100 mV at 10 NPLC; coarser at shorter integration times).
  2. Shunt method: switches in a known load (`check_code` 5 on JAFWAK, 4.6 mA) once, to prove the DMM sees the current through R13.
  3. Sweeps VIN (1.4–1.9 V) with the LDO enabled, then disabled through the enable path.
- **Reports:** current per point. Points under the floor are shown as "< floor", not as a number.
- **Plot:** current against VIN (left) and VOUT at OUTA against VIN (right), one row per state. Enabled, VOUT at nominal shows the LDO regulated, so the rail under test was powered (in the series method, through the meter: an open meter leaves that rail unpowered). Disabled, VOUT near 0 V shows the enable turned it off. What it can't show is a meter shorted out by a J1 jumper left fitted: the rail is still powered and the meter reads ~0.
- **Setup checks:**
  - the load check misses by more than 15 %. If the DMM reads the LDO's own current before the load goes in but the load adds nothing, the issue names the load wiring (J10, the DIO pins on the bank's gates); otherwise the DMM leads
  - an enabled current under the floor (meter not across R13 / not in series, or the jumper still shorting it)
  - VOUT off nominal when enabled, or still up when disabled (enable path not working)
  - meter overload
  - reversed leads (an issue: the currents are saved as measured, not flipped)
  - an overload during the zero (every current is then NaN) or the load check
- **Options:**
  - `--method series --rail bgr`
  - `--vin 1.8` (one point)
  - `--states enabled`
- **Config:** `[iq]`.

---

## Transient tests

### How a transient capture works (load-tran, line-tran)
- **Settings:** the AD2 captures 8192 samples at 10 MS/s (819 µs), with 20 % before the trigger.
- **Averaging:** each edge (rising, falling) is captured `averages` times. The captures are lined up on the trigger and averaged; the response repeats on every step, so this removes noise without blurring it.
- **Analysis:** a Python port of `AD_workspaces/load_tran_script.js` with the same `[analysis]` settings, so the numbers match the WaveForms custom measurements:
  - **Nominal:** VOUT before the step.
  - **Peak:** the largest dip or overshoot within 20 µs.
  - **Settling band:** ±10 % of the peak deviation.
  - **Settling time:** after the last 1 µs-smoothed sample outside the band.
  - **Noise:** should be at most band / 3, or noise sets the settling time.
- **DC shift after the step:** if VOUT's final level is outside the band (DC load regulation), it can never settle back to the pre-step level. The report says so and suggests `settle_reference = "post"`.
- **Setup checks:**
  - VOUT near nominal
  - the trigger channel actually steps, with the commanded swing
  - the step's edge is found in the data
  - if the scope never triggers, one untriggered capture is saved to show what's on the channels
- **Plots:**
  - `plot.png` zooms in on the step, with every edge on the same time span. The default is 2.5 × the slowest settling time + 10 µs, or `--plot-us 100` for a scope at 10 µs/div.
  - `plot_zoom.png` zooms each edge on its own span (2.5 × its own settling time + 10 µs), so a fast edge isn't squeezed next to a slow one. The time scales differ between panels, and the title says so.
  - `plot_full.png` shows the whole capture.
  - All three have the scope 2 (trigger) trace in a small panel on top and VOUT below. The 54621A, if used, is on a right-hand VOUT axis. One legend for the figure sits below the panels.
  - **Lines on the VOUT panel:**
    - **Averaged** (thin, light): the AD2's `averages` captures lined up on the trigger and averaged, as measured.
    - **Smoothed** (thick): the averaged trace with the switching glitch (±`blank_time` around the step) bridged by a straight line, then a `smooth_time` (1 µs) moving average. The peak, settling band and settling time are all measured on it, as in the WaveForms script.
    - **54621A:** the cross-check scope's own averaged trace.
- **Longer window:** `--window-ms 5` lowers the sample rate to fit 5 ms into the same 8192 samples. Fast details get coarser at lower rates.
- **54621A cross-check:** optional `[xscope]`. Every edge is also captured on the 54621A, overlaid as "54621A", and its peak and settling time are compared. `--no-xscope` skips it. When the run finds it, the wiring printout lists both of its channels (on the same points as the AD2's); when it isn't found, it isn't listed and the run goes on without it. See [instruments.md](instruments.md#agilent-54621a-cross-check-scope).
- **Extra files:** `waveform_rising.csv` / `waveform_falling.csv`.

### `load-tran`: load transient
- **Wiring:**
  - PSU -> VIN
  - Scope 1+ -> VOUT at the output pins, 1- -> GND
  - Scope 2+ -> DIO 4 (load bank bit B4, trigger), 2- -> GND. On JAFWAK, the trigger is DIO 2 (B2).
  - DIO [0, 1, 2, 3, 4] -> load bank bits B0..B4, AD2 GND -> board GND
- **Does:** steps the load bank between `code_low` and `code_high` at fixed VIN. It triggers on a DIO pin that changes between the two codes (`trigger_pin`, default: the highest bit that changes).
- **Supply current check:** before the captures it reads the supply's current at `code_low` and `code_high` (0.6 s). The rise must match what the bank should draw between them at nominal VOUT, within `loadbank_check`'s tolerances (10 % or 1.5 mA). If it doesn't, VIN isn't reaching the board or the load bank isn't wired. That's a setup problem, so the run is `_INVALID`. The readings go into `summary.json` as `supply_current`. On the bench (2026-11-10) the rise was 63.1–63.2 mA against 64.9 mA expected.
- **Options:**
  - `--from 4mA --to 67mA` (codes or currents)
  - `--avg 32`
  - `--window-ms`
  - `--plot-us`
  - `--no-xscope`
- **Config:** `[load_tran]`, `[analysis]`.

### Split-supply U2 (JAFWAK, `[buffer]`)
After the rework, U2's V− (pin 4) is on −5 V, so it can drive VIN down to 0 V and across 1.7–1.9 V. With a `[buffer]` section in the config, `line-tran` and `psrr` run it on a split supply:
- **Supplies:** PSU OUT1 -> VS_BUFF (J4), `line_tran.psu_voltage` / `psrr.psu_voltage` (5.0 V), limit `buffer.rail_current_limit` (100 mA). PSU OUT2 wired reversed (OUT2 + -> GND, OUT2 - -> U2 pin 4), `buffer.v_minus` (5.0 V), limit `buffer.current_limit` (20 mA).
- **W1 first, and always:** W1 holds U2's input (VIN_FG, J3) at DC 0 V before either rail comes up, and is never turned off while they are up. A floating input sends U2's output to a rail (up to ~3.4 V on VIN, first plan). Where the single-rail path turned W1 off, the split path sets it to 0 V.
- **Ramp:** VS_BUFF and V− ramp together, `buffer.ramp_step` (0.1 V) at a time, and VIN is read after each step. If it leaves `buffer.vin_window` (−0.1 to +0.3 V), both rails come down and the run stops, saying which case it looks like:
  - VIN follows VS_BUFF: the PSU is on VIN_DC / VIN, not on VS_BUFF
  - VIN goes negative: OUT2 is on VIN rather than U2 pin 4
  - VIN not held near 0 V: V− missing, JP4, or W1 not on VIN_FG
- **Interlock** (line-tran, psrr): rails up to VS_BUFF = `dut.vin_max` that way, then the 0.1 / 0.3 V proof (VIN must follow W1; catches a VIN probe that's off VIN), W1 back to 0 V, then VS_BUFF up to `psu_voltage`.
- **End and shutdown:** W1 to 0 V, rails down together, outputs off, W1 off last. A run that stops for any reason shuts down in the same order (W1 to 0 V, PSU outputs off, V− to 0 V, then the wavegen).
- **Not with `--vref ext`:** both need OUT2 (exit 2; the Bench refuses it too).
- **Wiring** (what the commands print): PSU OUT1 + -> VS_BUFF (J4), OUT1 - -> GND (J11); PSU OUT2 reversed onto U2 pin 4; AD2 W1 -> VIN_FG (J3); JP4 1-2 "FG"; nothing on VIN_DC (J7); Scope 1+ -> J13 pin 4 (OUTA), Scope 2+ -> J13 pin 1 (INA); both - -> J13 pin 6 (GND).

Without `[buffer]` (the demo board) line-tran and psrr use the single-rail path described under each test.

### `line-tran`: line transient
- **Wiring** (single-rail buffer; JAFWAK with `[buffer]`: see [above](#split-supply-u2-jafwak-buffer)):
  - PSU OUT1 -> buffer rail VS_BUFF / VDD_BUF and GND. NOT on VIN: the rail goes above the LDO's VIN limit
  - AD2 W1 -> buffer input SMA (VIN_FG / V_SIG); buffer output onto VIN (JP4)
  - Scope 1+ -> VOUT at the output pins, 1- -> GND
  - Scope 2+ -> VIN at the LDO input (trigger + VIN level), 2- -> GND
  - LDO enabled with jumpers (JAFWAK: J6 1-2 "HI", J8 1-2 and 9-10 "LDO"). EN_AIN = VIN, so EN steps with VIN; `--enable dio` holds it from the AD2 instead
  - DIO [0, 1, 2, 3, 4] -> load bank bits B0..B4, AD2 GND -> board GND
- **Does:**
  1. **Interlock, stage 1:** powers the rail at only 1.0 V with W1 off, and checks that VIN stays low. If VIN follows the supply, the leads are on VIN: stop.
  2. **Interlock, stage 2:** stage 1 can't tell a correct bench from a VIN probe that's off VIN (both read ~0 V). So the rail goes to `dut.vin_max` (safe even if the supply were on VIN) and W1 steps between the two `proof_levels` (0.1 and 0.3 V). VIN must follow W1. If it doesn't (supply on VIN, probe off, buffer not driving), the test stops before the rail goes above `dut.vin_max`.
     - **Not yet tried on hardware:** the BUF634 must be able to drive the proof levels with its rail at 1.9 V. If the first real run stops here with the wiring right, change `proof_levels`.
  3. **Calibration:** raises the rail to `psu_voltage` and calibrates out the buffer's offset from VIN measured at the board (on the AD2's 5 V range). The steps land on `vin_low` / `vin_high` within 5 mV. The W1 correction is capped at 250 mV.
     - **From below:** W1 starts 250 mV under each target and comes up to it, so a buffer that reads high can't overshoot. On 2026-11-17 W1 started at the target, U2 was +65 mV high with no load, and VIN reached 1.915 V (over `dut.vin_max`) before the hard stop.
     - **Hard stop:** any VIN reading above `dut.vin_max` less the AD2's error margin (about 20 mV at 1.9 V).
     - **W1's cap:** `dut.vin_max`, or `line_tran.w1_max` (JAFWAK: 1.95 V, approved 2026-11-19) once the interlock has passed and until the run ends. U2 sits about 5 Ω × the LDO's input current below W1 (58 mV at 18.3 mA), so 1.85 V at 18-20 mA needs W1 above 1.9 V. VIN keeps the hard stop above; psrr and EN_AIN keep `dut.vin_max`. Bounds: `dut.vin_max` to `dut.vin_max` + 0.2 V.
     - **Out of reach:** a level calibration can't reach is a setup issue, and the run stops there: rails down, nothing captured, an `_INVALID` folder with the issue (exit 3).
  4. **Square (the default, `square_hz` = 20 Hz):** W1 runs as a free-running square between the two calibrated setpoints. The AD2's DC level moves slowly (τ ≈ 2 ms on 2026-11-18 and 2026-11-19: a 10–90 % edge of about 4 ms); its square switches in about 0.2–0.35 ms (Joseph's raw capture, 2026-11-18). After a few periods, one untriggered capture over 1.5 periods checks VIN's highest sample against the same hard stop as calibration, before any edge is captured; over it, W1 goes to idle and the run stops. If the two setpoints come out equal (a buffer that can't reach the levels, already an issue), the run uses DC steps instead and says so. W1 goes back to DC before the rails come down. `--dc-steps` gives one DC step per capture, as before.
  5. **Steps:** each capture takes the square's next edge (or one DC step), triggering on VIN itself. Each edge is captured twice, each averaged: the fast capture (`sample_rate`, 0.8 ms at 10 MS/s) for the kick, and a long one over `settle_window` (20 ms; same 8192 samples, so about 0.4 MS/s) for VOUT reaching its new level. Half a square period must be longer than both. VIN is recorded on the AD2's fine 5 V range, and the trigger hysteresis is a quarter of the step (25 mV). The scope's default (1 % of range) is 0.5 V on the 50 V range of `[ad2] ref`, which a 100 mV step never clears: that's why line-tran never triggered on 2026-11-17 and 2026-11-19.
- **Computes:** from the fast capture, the peak (the kick) and its mV per V of VIN step. From the long capture, VOUT's new level (VOUT doesn't come back: line regulation), and how long it takes to stay within a band around it. The band is `analysis.settle_fraction` (10 %) of the level shift, but never tighter than 5 × the pre-step noise of the long capture, which is first smoothed over 1/200 of its window (100 µs at 20 ms; this delays the settling time by about half that). **Flat:** VOUT's change over about the last 20 % of the window (mean of the last 5 % against 75–80 %) is within the band. If it isn't, there is no settling time and the report says to lengthen `--settle-ms`; that's a note, not a setup problem.
- **Plots:** `plot.png` is the fast capture, 50 µs after the step (`--plot-us` changes it): the kick. `plot_full.png` is the long capture, with the band around the new level and the time VOUT stays in it. Without a long capture (`settle_window` unset) the plots are as in load-tran.
- **Setup check:** a buffer that can't reach a level (headroom, JP4, W1 wiring).
- **Options:**
  - `--low 1.75 --high 1.85`
  - `--load 19mA`
  - `--enable dio`: EN from the AD2 (DIO 5 / 6, AD2 V+ on VIO) instead of tied to VIN, so EN doesn't step with VIN. EN stays low through the interlock; W1 goes to `vin_low`, then EN goes high before calibration (so the buffer is calibrated with the LDO's load on it), and low again before the rails come down
  - `--square-hz 20`: the square's frequency. Half a period must be longer than the capture window (`--window-ms`), so the capture sees one edge: 20 Hz allows up to 25 ms
  - `--dc-steps`: one W1 DC step per capture instead of the square (slow: the AD2's DC level)
  - `--settle-ms 20`: the long capture's length. It must fit in half a square period: up to 25 ms at 20 Hz, 50 ms with `--square-hz 10`
  - `--raw`: one capture per edge, no averaging (`--avg` refused with it); the plots show only the measured VOUT samples, no smoothed trace. The reported numbers still come from the analysis (which smooths), so they match a normal run's
  - `--avg`, `--window-ms`, `--plot-us`, `--no-xscope`
- **Config:** `[line_tran]`.

### `sa-check`: HP 8591E on its calibrator
Talks to the analyzer only: no board, PSU or AD2. Run it before anything relies on the analyzer.
- **Wiring:** the 8591E's CAL OUT → its INPUT (50 Ω), with a short cable and nothing else on the input.
- **Does:**
  1. Preset, single-sweep mode.
  2. **Calibrator:** 300 MHz, 1 MHz span, 3 kHz RBW, one sweep, marker to the peak. It must be CAL OUT (within 1/100 of the span), at −20 dBm ± `sa.cal_tolerance_db` (1 dB; not from the datasheet yet).
  3. **Narrowest RBW:** asks for 30 Hz and reads back what was set: 30 Hz means Option 130. The bench unit gives 300 Hz (2026-11-20).
  4. **Noise floor:** the marker level at 1 MHz (no tone there) with that RBW, in dBm and µV.
  5. Preset (continuous sweep), leaving the front panel normal.
- **Not valid (exit 3):** no calibrator tone (CAL OUT not cabled), wrong frequency, or the level outside the tolerance.
- **Stops (exit 1):** any command the analyzer rejects (`ERR?`), or a reply that isn't a reading.
- **Saves:** `data.csv` (calibrator and floor), `summary.json` (with `option_130`), `meta.json`. No plot.
- **Config:** `[sa]`; `--sa` overrides the resource.

### `sa-path-cal`: the 8591E's path gain, for `psrr --meter sa`
The LDO stays unpowered. W2 feeds U4's input directly with a known 100 mV tone, so the path from U4's input to the analyzer (U4, R14, J9, DC block, coax) is measured with plenty of signal, at each frequency.
- **Wiring:** U2 and U4 on the split rails as for psrr; W1 → VIN_FG (holds U2's input at 0 V, so VIN stays off). **J10 removed.** W2 → J10 pin 1 (U4's input). Scope 1 → J10 pin 1; scope 2 → OUTA (VOUT). J9 → DC block → coax → 8591E (the same cable and block as psrr will use). Nothing on VIN_DC.
- **Does:**
  1. Rails up with W1 at 0 V. Scope 2 is on VOUT here, so the rails' ramp check reads VOUT, not VIN. If the PSU were on VIN, the LDO would start regulating and VOUT would leave the window by VIN ≈ 1.3 V, under `dut.vin_max`. A weaker guard than usual. VOUT must then read under 0.15 V, else stop.
  2. **J10 proof** (`Bench.prove_j10_open`): W2 at 0.3 then 0.6 V DC. J10 pin 1 must follow; VOUT must not, or J10 is still in and W2 would be driving the LDO's output, so stop. W2 can't go above 0.6 V before this passes (the Bench refuses), nor ever above 1.4 V or below 0 V.
  3. At 6 frequencies per decade from `psrr.sa_from` to `psrr.f_stop`: W2 plays 1.2 V + 100 mV. The AD2 fits it on J10 pin 1 (16 captures) and the 8591E reads it on J9 with psrr's exact settings. Gain = analyzer ÷ AD2.
  4. **Linearity:** at the first, middle and last frequency, the same with 10 mV and 1 mV tones (psrr's tones are far smaller). The gain must hold within 0.5 dB, else a setup issue.
  5. W2 to 0 V, rails down; the shutdown also holds W2 at 0 V until the rails are off.
- **Not valid:** either reading under 20 dB above its floor, or the linearity check failing. psrr won't use an `_INVALID` or `_SIM` run.
- **Saves:** `data.csv` (`kind` = cal / linearity, both readings and margins, `gain_db`), `summary.json`, `plot.png` (gain vs frequency).
- **Not run on the board yet.**

### `psrr`: power-supply rejection vs frequency
- **Wiring** (single-rail buffer; JAFWAK with `[buffer]`: see [Split-supply U2](#split-supply-u2-jafwak-buffer)):
  - PSU OUT1 -> buffer rail VS_BUFF / VDD_BUF and GND. NOT on VIN: the rail goes above the LDO's VIN limit
  - AD2 W1 -> buffer input SMA (VIN_FG / V_SIG); buffer output onto VIN (JP4)
  - Scope 1+ -> VOUT at the output pins, 1- -> GND (short ground lead)
  - Scope 2+ -> VIN at the LDO input pins, 2- -> GND
  - LDO enabled with jumpers (JAFWAK: J6 1-2 "HI", J8 1-2 and 9-10 "LDO"). EN_AIN = VIN, so EN moves with the tone
  - DIO [0, 1, 2, 3, 4] -> load bank bits B0..B4, AD2 GND -> board GND
- **Does:**
  1. **Setup:** the same two-stage interlock and DC calibration as line-tran, at `vin`.
  2. **Sine:** at each frequency (`f_start` to `f_stop`, `points_per_decade`), W1 plays `vin ± amplitude`. The peak must be within `dut.vin_max` less the AD2's error margin: the config refuses a higher setting, the amplitude is capped from the *measured* DC level, and the run stops if the measured peak goes over.
  3. **Measurement:** several free-running AD2 captures of VIN and VOUT. Each is sampled at the rate that fits at least `cycles` periods into 8192 samples, up to 100 MS/s.
- **Analysis:**
  - **Sine fit:** a least-squares fit at the injected frequency on each channel (a lock-in). VOUT/VIN is averaged coherently over the captures, which keeps the phase and averages noise down.
  - **Levelling:** if VIN's amplitude at INA is off by more than 15 %, W1's is re-levelled, in up to 6 passes per frequency. Each pass at most 1.5 × the last, and only as far as the VIN peak, predicted from the *measured* VIN (VIN scales with W1), stays under `dut.vin_max` less the AD2 margin. W1's own peak stays within `dut.vin_max`.
    - **Why:** on 2026-11-20 VIN rolled off above ~50 kHz (43 mV at 100 kHz, 4 mV at 1 MHz), most likely U2's ~5 Ω into the ~0.5 µF left on VIN / INB with R6 lifted (a corner near 60 kHz, the same RC as line-tran's 5–6 µs edge). The old single step stopped W1 at 81 mV, because it limited VIN DC + W1's amplitude as if the buffer passed the tone at full size.
    - **Carry-over:** the next frequency starts from the last amplitude, limited so that even with the buffer's gain 1.5 × the last measured one, VIN stays under the limit. The measured-peak hard stop backs it up.
    - **What it can't reach:** 50 mV at ~1 MHz would need W1 near 1 V (over its 1.9 V cap) and ±150 mA from U2, which sinks half of that from the −5 V rail (20 mA limit). So W1 stops at its cap there and VIN carries less.
  - **Small tones on the analyzer's points:** with `--meter sa`, from `sa_from` up, a VIN tone under 30 % of the target is fine (PSRR is a ratio, and the 8591E's floor is ~1 µV). Only under 2 mV is it an issue. AD2 points keep the 30 % rule.
  - **PSRR:** PSRR (dB) = 20·log10(|VIN| / |VOUT|), positive = rejection.
- **Floor:** the smallest VOUT ripple the AD2 resolves at that frequency, from the largest of three noise estimates:
  - the fit's leftover noise
  - how much VOUT/VIN scatters from capture to capture
  - tones fitted 3 frequency steps either side of the test frequency

  The last two catch mains hum and 1/f noise near the test frequency, which the first alone would let through as a measured PSRR. A point needs VOUT above 4σ of that noise to count as measured; points under it are drawn hollow and reported as "≥ X dB". That's roughly 70 dB at 50 mV in a quiet setup.
- **What the floor can't see:** pickup locked to VIN, such as crosstalk between the AD2's channels or ground coupling. That needs a spectrum analyzer on VOUT, or a calibration run with scope 1 shorted at the VOUT ground point (see [architecture.md](architecture.md#recipes)).
- **Setup checks:**
  - VIN not carrying the sine (buffer not driving VIN)
  - VOUT off nominal
  - a note if VOUT follows VIN at low frequency (dropout, or scope 1 on VIN)
- **Plot:** PSRR vs frequency, and the VIN / VOUT amplitudes with the VOUT floor.
- **No scope on VREF:** the BGR's own PSRR (`--measure vref`, a scope on TP3 through a 10x probe) was removed on 2026-11-14. A scope on VREF loads the BGR and pulls VREF, and VOUT with it, down (seen in enable-startup). VREF is read only with the DMM at 10 GOhm (vref-line, temp).
- **Spectrum analyzer (`--meter sa`, added 2026-11-20, simulator only):** VIN still comes from the AD2; from `sa_from` (30 kHz) up, VOUT comes from the HP 8591E.
  - **Path:** VOUT → J10 → U4 (on ±5 V; V− by jumper wires, JP3 open) → R14 47 Ω → J9 → DC block → coax → 8591E INPUT.
  - **Each point:** RBW = f/30 within 300 Hz–3 kHz, span 20 RBWs, reference −20 dBm. Marker to the peak (or at f, if the peak isn't within 2 RBWs). The floor is read 6 RBWs **above** the tone, + 6 dB; below it sits the analyzer's LO feedthrough near 0 Hz, which swamped the floor on 2026-11-20.
  - **Path gain from `sa-path-cal`** (below), never assumed: `--sa-cal <run folder>`, by default the newest valid one under `--out`. Interpolated in dB on a log-frequency axis; a sweep reaching outside the curve is refused before anything powers up. `summary.json` records the curve's folder and age.
  - **Checked in the run:** from `sa_from` to `sa_cal_to` (100 kHz) the AD2 also measures VOUT, with `sa_cal_averages` (128) captures. Where both meters read VOUT `sa_cal_margin_db` (20 dB) above their floors, they must agree within `sa_path_tolerance_db` (1 dB). If not, the curve doesn't describe today's path (coax, DC block or U4 changed) and the analyzer's points are invalid. With no such point there is a note.
  - **Saved either way:** `data.csv` has the analyzer's raw reading at every point it read (`sa_dbm`, `sa_vpeak_v`, `sa_floor_vpeak_v`, `sa_margin_db`, `sa_rbw_hz`, `sa_peak_frequency_hz`), at its input, before the path gain.
  - **Below `sa_from`** VOUT is the AD2's. Analyzer points have no phase. The report shows which meter measured VOUT, and the AD2's own reading next to the analyzer's; the plot marks the AD2's as ×.
  - **−5 V rail:** U2 (wide-BW, 8.5–12 mA) plus U4 (low-quiescent, 1.5–2.3 mA), against `buffer.current_limit` (20 mA).
  - **Not explained yet (2026-11-20):** above ~200 kHz the analyzer read about 20 × the AD2's VOUT. Possibly pickup of W1's tone; the pickup check (planned) will tell.
- **Options:**
  - `--fstart`, `--fstop`, `--ppd`
  - `--amp-mv 20`
  - `--vin`
  - `--load`
  - `--avg`
  - `--meter sa` (or `psrr.meter = "sa"`): VOUT from the 8591E from `sa_from` up
  - `--sa-cal <folder>`: the `sa-path-cal` run whose curve converts the analyzer's readings (default: the newest valid one)
- **Config:** `[psrr]` (`meter`, `sa_from`, `sa_cal_to`, `sa_cal_averages`, `sa_cal_margin_db`, `sa_path_tolerance_db`), `[sa]`.

---

## Enable block (JAFWAK)

### `enable-sine`: enable comparator thresholds
- **Wiring:**
  - PSU -> VIN_DC (JP4 to DC); J1 ON column, all four (1-3, 5-7, 9-11, 13-15): VDD (J1 5-7) must be up in every test
  - AD2 W1 -> EN_FG SMA (J2); J6 5-6: EN_FG -> EN_AIN
  - Scope 2+ -> TP6 (EN_AIN) (trigger), Scope 1+ -> J8.2 (EN_OUT); both - -> GND
  - AD2 GND -> board GND
- **Does:** a slow wave (0 V to EN_VDD; the bench refuses a top above `dut.vin_max`) into EN_AIN sweeps the input through both thresholds every cycle. The capture is repeated `captures` times to get the spread.
- **Shape** (`--shape`, `en_sine.shape`):
  - `sine` (default), at 1 kHz: about 5.6 V/ms through the midpoint.
  - `triangle`: a linear ramp up and down at 2 (high − low) f V/s. With `--freq 1` that's 3.6 V/s, about 1500× slower than the 1 kHz sine, and the same slope through both thresholds. Comparing the two runs shows how much the comparator's delay moves the thresholds.
  - `square`: jumps through both thresholds, so no thresholds are measured. It gives the **propagation delay** instead (first bring-up plan, Test 1.8): from EN_AIN's midpoint crossing to the output's next midpoint crossing, for each output edge (rising / falling), and whether the output is inverted (Scope 1 on J8 pin 10, ~EN_OUT). A delay within two sample intervals is reported as "under" that. Use `--freq 1e5` so the capture runs at the AD2's 100 MS/s (10 ns per sample); at 1 kHz it's 2 MS/s and any delay under ~1 µs is only a bound.
  - Each capture spans `cycles` periods in 8192 samples, at most 100 MS/s, so at 1 Hz a capture takes 4 s, one sample every 488 µs, and resolves the threshold to about 2 mV. The trigger wait is `timeout` plus that window.
- **Computes:**
  - **Thresholds:** the input voltage at each output crossing gives VT+ and VT-, and their difference is the hysteresis.
  - **Output levels and edges:** VOH / VOL, 10–90 % rise and fall times and duty cycle. An edge within two sample intervals is reported as "under" that (977 ns at 1 kHz, 977 µs at 1 Hz), not as a time: on the bench a fast edge measured 1.3–1.6 intervals at both rates. So the real rise and fall times come from the square run at `--freq 1e5` (100 MS/s, 20 ns); a sine or triangle run whose edges are only bounds says so and points to it.
  - **Chatter:** output edges per input cycle. More than 1 means it chatters.
- **Plot:** input and output against time, the transfer curve (output against input: the hysteresis loop), and one rising and one falling output edge close up, with every sample marked and the 10 % / 90 % levels drawn, so the edge's shape and the sample spacing are visible (on a square run the input edge is in view too, so the delay shows).
- **Setup checks**, on every capture (a probe can slip mid-run):
  - the input swing isn't the wave's (probe not on TP6, J6 wrong)
  - EN_OUT doesn't switch (probe not on J8.2, EN_VDD unpowered)
- **VOUT tracking EN_AIN (`--output vout`, bring-up plan Test 5):** scope 1 on OUTA (J13 pin 4) instead of EN_OUT, J8 1-2 / 9-10 "LDO" fitted, so the LDO follows EN_AIN through the whole enable path. The report gives the EN_AIN levels where VOUT turns on and off. They include the LDO's startup and shutdown time (~6 / ~170 ms), so use a slow triangle: `--shape triangle --freq 0.1` (0.36 V/s, ~60 mV of shift on the turn-off level; the report says how much at the slope used). VOUT not switching, or its high level off nominal, is a setup issue. A square is refused here: VOUT's timing after an enable edge is `enable-startup`'s job.
- **Options:**
  - `--shape triangle` or `--shape square`
  - `--freq 1e3`
  - `--captures 8`
  - `--output vout`
- **Config:** `[en_sine]`.

### `enable-current`: the enable block's current against switching frequency
- **Why:** EN_VDD's static current (~0.3 nA, PEX) is under the DMM's 10 nA count, and the ~1.6 mA spike at each threshold crossing is far too short for it. But each input cycle moves about the same charge Q through the block, so its average current is I_static + Q f, a DC current the DMM reads. The slope gives Q.
- **Wiring:**
  - PSU -> VIN_DC (JP4 to DC)
  - Remove the J1 9-11 jumper (EN_VDD); DMM I (3A/mA jack) -> J1 pin 11 (INB), DMM LO (COM) -> J1 pin 9 (EN_VDD): current flows from INB into the rail
  - J1 1-3, 5-7, 13-15 fitted. **VDD (J1 5-7) must be powered in every test:** only the VDD ring has ESD clamps, and a driven pin with VDD off forward-biases them
  - J8 5-6 (EN_IN = GND; misprinted "HI") and J8 13-14 "HI" (~EN_IN = VIN): the LDO is held off and EN_OUT drives nothing
  - J6 5-6 "FG"; AD2 W1 -> EN_FG (J2); Scope 2+ -> EN_AIN (TP6), Scope 2- -> GND
  - **Nothing on EN_OUT (J8 pin 2) or ~EN_OUT (J8 pin 10):** a scope's ~24 pF charged to 1.8 V at 1 MHz is ~43 µA from EN_VDD, more than the block
- **Does:**
  1. The DMM zero with the supply off (subtracted) and the floor (3 × its spread, at least one count: 10 nA on 10 mA at 10 NPLC).
  2. W1 to DC 0 V (EN_AIN never floats), VIN up: the static point.
  3. A 0-VIN square at 10 Hz to 1 MHz, 1-2-5 per decade (16 points): at each, the DMM current and EN_AIN's swing (scope).
  4. W1 back to DC 0 V; fits I = I0 + Q f to the points above the floor.
- **Reports:** current per frequency ("< floor" under it), Q in pC per cycle and the equivalent capacitance Q / VIN.
- **Plot:** current against frequency, log-log, with the fit and the floor band (points under it are bounds); EN_AIN's swing against frequency.
- **Setup checks:**
  - EN_AIN not swinging like the square (scope not on TP6, J6 not at 5-6, W1 not on EN_FG)
  - the current at the top frequency not above the static point by more than the floor (DMM not in the path, or J1 9-11 left fitted beside it)
  - a DMM overload (at the zero: every current is NaN)
- **Square only, VOUT not read:** the LDO's startup is far slower than the square, and it is held off anyway.
- **Options:** `--fstart 10 --fstop 1e6` (at most 1 MHz).
- **Config:** `[en_current]`.

### `enable-startup`: turn-on and turn-off through the enable path
- **Wiring:**
  - PSU -> VIN_DC (JP4 to DC); J1 ON column, all four (BGR_VDD 1-3, VDD 5-7, EN_VDD 9-11, OTA_VDD 13-15); JP2 fitted
  - Enable path: J6 7-8 (EN_CTRL_18 -> EN_AIN), J8 1-2 (EN_OUT -> EN_IN), J8 9-10 (~EN_OUT -> ~EN_IN)
  - AD2 DIO 5 -> J13.12 (EN_CTRL_33), DIO 6 -> J13.10 (~EN_CTRL_33)
  - AD2 V+ -> J13.14 (V_33), AD2 GND -> board GND
  - AD2 scope 1+ -> J13 pin 4 (OUTA): VOUT
  - AD2 scope 2+ -> EN_IN (the LDO's enable after the enable block; `board.enable_ldo_in`, **pin not confirmed**)
  - 54621A ch1+ -> J13 pin 1 (INA), ch2+ -> J13 pin 2 (INB); both ground clips on board GND
  - 54621A Ext Trig -> EN_AIN (TP6): 1.8 V logic, the 54621A's trigger
  - **54621A grounds:** all of its grounds are common with the AD2's (continuity, 2026-11-14), so a ground clip on INB would short R13 to ground. Ground clips go on GND only.
  - **No scope on VREF (TP3).** A scope input loads the BGR and pulled VREF, and VOUT with it, to about 0.98 V (2026-11-14). `--ch2 vref` was removed.
- **Enable path:** DIO 5 → EN_CTRL_33 → FXLP34 level shifter → EN_CTRL_18 → EN_AIN → enable block → EN_OUT → EN_IN. DIO 6 drives ~EN_CTRL_33 inverted. The AD2's V+ supplies the level shifters' 3.3 V side.
- **Does:**
  - steps the enable line; the AD2 triggers on its own DIO edge (the Digital In trigger detector);
  - averages each edge (enable, disable);
  - arms the 54621A's averaged acquisition first, so it sees the same edges, triggered on EN_AIN through Ext Trig at half of `xscope_trigger_high` (0.9 V).
- **Input current:**
  - **The difference:** I_in = (INA − INB − zero) / R13. The 54621A's channels are ground-referenced, hence the difference.
  - **Both channels on the same zoomed range,** `xscope_shunt_range` (0.16 V full scale = 20 mV/div, centred on `startup.vin`).
  - **The zero:** the channels' offset and gain differ by much more than the millivolts across R13, so the zero is INA − INB while the LDO is disabled (before the enable edge, after the disable edge). The shutdown current there is far under the resolution. The report and `summary.json` give the zero.
  - **Time axes:** I_in's t = 0 is EN_AIN crossing 0.9 V. VOUT's and EN_IN's t = 0 is the AD2's DIO edge, slightly earlier.
- **Computes:**
  - VOUT: 10 % / 90 % times from the edge (they include the level shifter and the enable block), rise or fall time, settling within 2 %, overshoot;
  - I_in: the levels before and after, and the peak. The inrush peak is reported even when the level change is under the resolution.
- **Current resolution:**
  - one 8-bit count of the 54621A at 0.16 V full scale is 0.6 mV, so 6 mA across 0.1 Ω;
  - a step under 4 counts (25 mA) is reported as a bound, with no timings. That includes the quiescent current and the load steps (use `iq` for DC current);
  - averaging helps only as far as noise dithers the signal;
  - a smaller `xscope_shunt_range` resolves more but clips sooner when VIN sags.
- **Setup checks:**
  - VOUT already up before enabling (the enable path isn't controlling the LDO)
  - VOUT not reaching nominal
  - VOUT not settled by the end of the capture (lengthen `--window-ms`; JAFWAK's default window is 10 ms, since 1 ms was too short)
  - VOUT still above 20 % of nominal at the end of a disable edge
  - INA or INB clipped at the edge of the 54621A's screen (VIN out of the zoomed window)
  - no data from the 54621A. With no `[xscope]` configured, the command is refused before anything powers up.
- **Saves:** `waveform_<edge>.csv` (AD2: t, VOUT, EN_IN) and `waveform_<edge>_iin.csv` (54621A: t, INA, INB, I_in).
- **Load:** `startup.load_code = 5` on JAFWAK (4.6 mA, B0 + B2), so there is a turn-on current to see. The load bank needs J10 "LOAD" and DIO 0-4 (no VS_BUFF). `--load 0` runs unloaded.
- **Supply limit:** enable-startup runs on `psu.current_limit` (10 mA on JAFWAK); 4.6 mA plus the LDO's own current stays under it. The inrush charging the output capacitors can be far above that, so the supply may limit the startup; VIN sagging then shows up as INA / INB clipping.
- **Options:**
  - `--avg`
  - `--window-ms`
  - `--load`
- **Config:** `[startup]`, `[enable]`.
- **First hardware run:** check the AD2's digital trigger, the 54621A's Ext Trig on EN_AIN, and the EN_IN probe point.

---

## Temperature (JAFWAK)

### `temp`: temperature log
- **Wiring:**
  - PSU + -> VIN_DC (J7), PSU - -> GND (J11); JP4 2-3 "DC"; JP2 closed
  - VS_BUFF (J4) at 5.0 V from a separate supply: it powers the TMP235 on TEMP (TP7). The load bank itself doesn't need it
  - Scope 2+ -> J13 pin 5 (TMP), Scope 1+ -> J13 pin 4 (OUTA); both - -> J13 pin 6 (GND)
  - DMM V / COM -> VREF (TP3) / GND (TP2), Hi-Z if the meter has it
  - LDO enabled (J6 1-2 "HI", J8 1-2 and 9-10 "LDO") or `--enable dio`
  - Load: `temp.load_code = 1` (1.0 mA through B0, 1.2 kOhm), the 1 mA of the first plan; J10 "LOAD" and DIO 0-4
- **Does:** with the LDO on, samples every `interval` (2 s) for `duration` (30 min, at most 2 h): T from the TMP235 on the AD2's 5 V range, T = (V − 0.500 V) / 0.010 V/C; VREF on the DMM; VOUT on the AD2; the supply current. Ctrl-C ends the log early, keeps the data and still shuts down. First bring-up plan, Test 3.10: start at room temperature, then put ice in a sealed bag on U1.
- **Computes:**
  - **Plateaus:** a sample is stable when T held within `stable_delta` (0.5 C) for `stable_window` (30 s). Each run of stable samples is averaged into one point (T, VREF, VOUT).
  - **Tempco:** with two or more plateaus at least `min_spread` (2 C) apart, a line through the plateau means gives VREF and VOUT in ppm/C. It works on temperature differences, so the AD2's offset (~10 mV ≈ 1 C, unverified) mostly cancels. Simulated: 58.9 ppm/C, so room to ~5 C moves VREF by only ~1.4 mV.
- **Setup checks:** every T within `temp_min`..`temp_max` (−20 to 80 C: scope 2 on J13 pin 5, VS_BUFF powered); VOUT near nominal; VREF within 20 % of `vref_nominal` (DMM on TP3).
- **Notes:** stopped with Ctrl-C; no tempco (fewer than two plateaus far enough apart); the supply current rising by more than 20 % (condensation on the board).
- **Options:** `--minutes 30`, `--interval 2`, `--vin`, `--no-dmm` (no VREF), `--enable dio`.
- **Config:** `[temp]`.

## External load: `--ext-load-ohm`

A fixed resistor on VOUT (on VOUT (J5) to GND (J12)) is part of the load the LDO sees.
`--ext-load-ohm` on `load-reg`, `line-reg`, `load-tran`, `enable-startup` and `temp`
(or `loadbank.external_ohm`) adds VOUT / R to every load estimate, and the wiring lists it.
JAFWAK's refitted load cell (2026-11-14) gives 1 mA at code 1, so the plan's 1.2 kOhm on J5 is no
longer needed. `dut.i_max` is checked with it included for the codes the run uses, before anything
powers up: with 1.2 kOhm, code 22 (20.04 mA) becomes 21.04 mA and is refused (use `--load 21`, a bigger
resistor, or none). `load-reg`'s default sweep ("every code within dut.i_max") leaves such codes out and says so.

## Enable from the AD2: `--enable dio` (JAFWAK)

`smoke`, `load-reg`, `line-reg`, `load-tran`, `line-tran`, `vref-line` and `temp` don't need the enable line: by default the LDO is tied on with jumpers (J6 1-2 "HI", EN_AIN = VIN, with J8 1-2 and 9-10 "LDO"), so it turns on as VIN rises. With `--enable dio` the run drives it from the AD2 through `[enable]` instead, the same path `iq` and `enable-startup` use.

- **Wiring** (added to the test's own; the run prints it): AD2 DIO 5 -> J13 pin 12 (EN), DIO 6 -> J13 pin 10 (~EN), AD2 V+ -> J13 pin 14 (VIO); J6 7-8 "EN" and nothing else on J6; J8 1-2 and 9-10 "LDO".
- **Order:** EN is driven low before VIN comes up (V_33 on first), high once VIN is up, then the run waits `enable.settle_time` (0.2 s) before measuring. It goes low again when the test ends and, if the run stops for any reason, as the first step of the shutdown after the external reference.
- **Refused** (exit 2) if an enable DIO pin is also a load bank pin.
- **Records:** `meta.json` has `"enable": "dio"` or `"static"`.
- **Not on** `line-tran` or `psrr` yet.

## External reference: BGR vs `--vref ext` (JAFWAK)

The BGR on the board measures 1.17 V; in simulation it was 1.204 V. JP2 joins the BGR's output (VREF_OUT) to the reference the regulator follows (VREF_IN). Removing JP2 and driving VREF_IN from the supply's second output repeats a test with the reference at its intended value. Comparing that run with the BGR run separates what the BGR contributes from what the rest of the regulator does.

**Which tests:** `smoke`, `load-reg`, `line-reg`, `load-tran`, `line-tran`, `psrr` and `iq` take `--vref ext`. The enable tests don't. Without the option (or `--vref bgr`) nothing changes.

**Wiring** (added to each test's own wiring):
- JP2 (VREF) **removed**
- PSU OUT2 + → VREF_IN (JP2's VREF_IN pin), OUT2 − → GND

Pin roles come from the schematic (JP2 between the VREF_OUT and VREF_IN nets) and are unconfirmed until someone checks the board. TP3 is labelled VREF, but which side of JP2 it's on isn't confirmed either.

**What the run does:**
1. **JP2 question:** after the wiring prompt it asks, separately, whether JP2 is removed. This is asked **every run, even with `--yes`**. If JP2 were still fitted, OUT2 would back-drive the BGR's output, and nothing can measure that beforehand.
2. **Hold at 0 V:** OUT2 is set up with the supply (5 mA limit, OVP at `vref.ovp`) and held at 0 V while VIN comes up. The E3648A switches both outputs on together.
3. **Reference on:** with VIN up and the LDO enabled, OUT2 ramps to `vref.external_v` (1.204 V).
4. **Proof:** the reference drops by 20 mV and comes back, and VOUT (AD2 scope 1) must follow by at least 10 mV. If it doesn't, the reference goes to 0 V and the run **stops**. JP2 is probably still in (the BGR holding VREF_IN), or OUT2 isn't on VREF_IN.
5. **The test:** it runs as usual. line-reg and iq turn the reference on at the top of their sweep, then sweep down.
6. **VIN window:** while the reference is on, VIN never goes more than `vref.above_vin` (0.3 V) below it. Before VIN is removed (the supply off, or the wavegen that drives VIN off, and at shutdown), the reference goes to 0 V first.

Even so, the BGR is unprotected from one mistake. With JP2 fitted, OUT2 sits on the BGR's output at 0 V from the moment VIN comes up, until the proof fails and the run stops. A bench supply can't sink much current at 0 V, but that hasn't been checked. Remove JP2 before switching anything on.

**Results:**
- the folder name ends in `_extref`
- `summary.json` and `meta.json` gain a `vref` entry: the source, the applied voltage, `bgr_measured` from the config, and the proof readings
- a BGR run records `"source": "bgr"`

**Checks:** the usual ones. VOUT must be within 20 % of `vout_nominal`, so both 1.17 V and 1.204 V pass.

**`iq` note:** VREF_IN's own current comes from OUT2, so it isn't counted. The BGR is still powered from VIN, so its current is counted.

**Config:** `[vref]` in `jafwak.toml`:
- `external_v`, `bgr_measured`
- `psu_channel`
- `v_max` (1.3 V)
- `current_limit`, `ovp`
- `above_vin`
- `proof_step`

### AD2 V+ as the reference: `line-tran --vref ad2`
line-tran can't take `--vref ext`: PSU OUT2 is U2's −5 V. `--vref ad2` uses the AD2's V+ supply on VREF_IN instead, and reads it back on the DMM. line-tran only, for now.

```
uv run ldo-bench line-tran --vref ad2                 # 1.204 V (vref.external_v)
uv run ldo-bench line-tran --vref ad2 --vref-v 1.15   # another level, at most vref.v_max (1.3 V)
```

- **Wiring** (added to line-tran's own): JP2 **removed**; AD2 V+ → JP2 pin 1 (VREF_IN); DMM V → JP2 pin 1, COM → GND (TP12). The AD2's ground is already on J13 pin 8. TP3 is on the BGR side of JP2 (per the PCB file), so with JP2 out it reads the BGR, not VREF_IN.
- **When V+ is on:** only while the LDO is powered.
  1. After the interlock, U2's rails up, and W1 calibrated with VIN at `vin_low`.
  2. Then VIN is read on the AD2: below the reference, V+ stays off and the run stops ("the LDO is not powered").
  3. V+ ramps up (it can't give less than 0.5 V, so it jumps from off to 0.5 V, then steps by `psu.ramp_step`), and the same 20 mV proof as `--vref ext` runs: VOUT must follow, else V+ goes off and the run stops (JP2 still in?).
  4. The DMM reads VREF_IN: the AD2 has no V+ readback, and its setpoint accuracy isn't known.
  5. At the end, with VIN still up, the DMM reads VREF_IN again and V+ ramps off. Only then do EN and U2's rails come down. On an error or Ctrl-C, the shutdown turns V+ off first.
  - U2's rails coming down always turn the reference off first (`buffer_rails_off`), whatever called it.
  - **V+ off at the start:** when the bench opens, V+ is turned off before anything else, in case an earlier run was killed before its shutdown (or the WaveForms app left it on).
  - **VIN watched while V+ is on:** every capture (fast, long, and the square check) checks VIN's lowest sample against VREF − `vref.above_vin`. If VIN has gone (U2's rails lost, a lead off), V+ goes off at once and the run stops: within one capture, not instantly.
  - **If V+ won't turn off** in the shutdown (the AD2 not responding), the shutdown carries on, since VIN must still come down, and the failure says in red to unplug the V+ lead from JP2 pin 1.
- **Refused:** with `--enable dio` (V+ is then the enable logic's 3.3 V rail; `Bench.enable_logic` refuses it too), without a DMM, on any test but line-tran, and `--vref-v` without `--vref ad2` / `ext`.
- **Records:** `summary.json` / `meta.json` `vref`: `"driver": "AD2 V+"`, `external_v`, the proof readings with `dmm_v` (start), and `dmm_end_v`. The report prints them. The folder name ends in `_extref`.
- **Not run on the board yet.**

### `compare`: two runs side by side

```
uv run ldo-bench load-reg                 # JP2 fitted: the BGR
uv run ldo-bench load-reg --vref ext      # JP2 removed: 1.204 V on VREF_IN
uv run ldo-bench compare results/load-reg/<bgr run> results/load-reg/<extref run>
```

- **Reads:** the two run folders. It touches no instrument and recomputes nothing.
- **Prints:**
  - every number in both summaries, with A, B and B − A
  - which reference each run used
- **Writes:** `results/compare/<stamp>_<dut>_compare_<test>/`, holding `compare.csv`, `summary.json` and `plot.png`.
- **Plots** (both runs overlaid):

  | Test | Plotted |
  |---|---|
  | load-reg | ΔVOUT from no load |
  | line-reg | ΔVOUT from the top VIN |
  | load-tran, line-tran | each edge minus its pre-step level |
  | psrr | dB vs frequency |
  | iq | current vs VIN |

  The reference's DC offset (about 34 mV) is in the legend and the table, not the curves.
- **Refused (exit 2):** runs of different tests or different boards.
- **Invalid (`_INVALID`, exit 3):**
  - either run was invalid
  - the runs differ in anything but the reference: the test's config section, `[dut]`, `[loadbank]`, or a text field such as where VOUT was measured

  Change only JP2 and `--vref` between the two runs.
- **Same reference twice:** works, as a repeatability check, and says so.
- **Supported:** load-reg, line-reg, load-tran, line-tran, psrr, iq.
