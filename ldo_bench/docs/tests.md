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
- `--enable static|dio` (smoke, load-reg, line-reg, load-tran, vref-line, temp): how the LDO is enabled. `static` (default): tied on with jumpers. `dio`: driven from the AD2; see [Enable from the AD2](#enable-from-the-ad2---enable-dio-jafwak).

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
- **Simulator only so far:** `line-tran`, `enable-startup`, `iq` and `psrr` (on the split-supply U2 too); `buffer-check`; `vref-line`, `temp`, `enable-sine --shape square`, `enable-current` and `--enable dio` (added 2026-11-13); `enable-sine --output vout`, `line-reg --split-rails`, `--ext-load-ohm` and vref-line's DMM loading check (added 2026-11-14). `enable-sine --shape triangle` ran on JAFWAK on 2026-11-12.

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
  - PSU OUT1 + -> load bank VIN (J5), PSU - -> load bank GND (J6)
  - AD2 DIO [0, 1, 2, 3, 4] -> load bank J0..J4
  - AD2 GND -> load bank GND (the FET gates are driven relative to it)
- **Does:** at `loadbank_check.voltage` (1.0 V by default; 0.8 V on JAFWAK so B4's 15.4 Ω draws 52 mA, under its 60 mA limit), it reads the supply current with every bit off. Then it turns on each bit alone and compares the extra current with V/R for that bit.
  - The no-load current is subtracted first, which cancels the supply's current-readback offset.
  - `dut.i_max` doesn't apply here, since no DUT is connected.
- **Diagnoses:**
  - a bit that draws nothing (open wire, missing ground, bad solder)
  - a DIO pin that seems to be wired to a different J header
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
- **Higher limit:** a test whose section has `high_current = true` uses `psu.high_current_limit` instead (60 mA on JAFWAK, approved 2026-11-13). The tests that can opt in are `load-reg`, `line-reg`, `load-tran`, `smoke` and `loadbank-check`; on JAFWAK all five do, since they switch in up to 19.2 mA (loadbank-check: 52 mA).
- **Config check:** no test may be configured to draw at or above the limit it runs with: its heaviest load-bank code at `vout_nominal` (loadbank-check: `voltage` over the smallest resistor). This estimate leaves out the LDO's own few hundred µA. `line-tran`, `psrr` and `buffer-check` are separate: they power the buffer rail with `buffer.rail_current_limit`.

### `buffer-check`: the reworked U2 on its own (JAFWAK)
First bring-up plan, steps I.1–I.5. **Run it before `line-tran` or `psrr`, with the chip disconnected.** Needs `[buffer]`.
- **Wiring:**
  - CHIP DISCONNECTED: JP4 open (no jumper); nothing on VIN_DC (J7)
  - 100 Ohm dummy load from JP4 pin 1 (U2 output) to GND (TP8): ~18 mA at 1.8 V, about what the chip draws at 20 mA
  - PSU OUT1 + -> VS_BUFF (J4), OUT1 - -> GND (J11)
  - PSU OUT2 REVERSED: OUT2 + -> GND, OUT2 - -> U2 pin 4 (-5 V)
  - AD2 W1 -> VIN_FG (J3); Scope 2+ -> JP4 pin 1, 2- -> GND (TP8)
- **Does:**
  1. W1 to DC 0 V, then the split supply up ([below](#split-supply-u2-jafwak-buffer)), with JP4 pin 1 read after every step.
  2. The output at W1 = 0 V: U2's offset (up to ~65 mV, first plan).
  3. Calibrates W1 for each of `check_levels` (1.70, 1.80, 1.85 V). The hard stop is `check_peak_max` (1.95 V) since the chip is off; W1 itself stays within `dut.vin_max`. That's why 1.90 V isn't a default: with a negative offset it needs W1 above 1.9 V.
  4. DC gain across the levels (the first plan expects 0.93–0.99; outside that is a note).
  5. A step between the lowest and highest levels, both ways: peak and 10–90 % time (C9 and U2's ~7 Ohm give tens of µs; within two samples, 2 µs, it's reported as "under 2 µs").
  6. A sine, 1.80 V ± 50 mV at 10 kHz: output amplitude against W1's, and its peak.
  7. Rails down with W1 at 0 V, W1 off last.
- **Not ready (exit 3)** if a level isn't reached, anything on JP4 pin 1 goes above 1.95 V, or the output doesn't follow W1 (probe not on JP4 pin 1). The report prints the W1 setting for each level, a cross-check for line-tran's and psrr's own calibration.
- **Not tested:** the first plan's I.4 (what W1 does when stopped). The scripts never stop W1 while U2's rails are up.
- **Config:** `[buffer]` (`check_levels`, `check_peak_max`, `check_sine_*`, `v_plus`).

---

## DC tests

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
  - `--ext-load-ohm 1200` (e.g. 1 mA with `--load 0`)
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
  - PSU -> VIN_DC (JP4 to DC); nothing on the load bank (it stays off)
  - DMM V (HI) -> J13.1 (VIN), COM -> J13.2 (INB): across R13. The DMM floats, so no ground lead; nothing else on the V terminals
  - J1: every rail you want powered (BGR_VDD 1-3, VDD 5-7, EN_VDD 9-11, OTA_VDD 13-15)
  - Scope 1+ -> J13 pin 4 (OUTA), Scope 1- -> J13 pin 6 (GND) (VOUT check; draws ~1 uA)
  - Load check (shunt, enabled): separate 5.0 V supply -> VS_BUFF (J4), on first, then fit J10 "LOAD"; AD2 DIO 0-4 -> J13 pins 7, 9, 11, 13, 15 (B0-B4; the check uses B0)
  - Enable path: J6 7-8 (EN_CTRL_18 -> EN_AIN), J8 1-2 (EN_OUT -> EN_IN), J8 9-10 (~EN_OUT -> ~EN_IN)
  - AD2 DIO 5 -> J13.12 (EN_CTRL_33), DIO 6 -> J13.10 (~EN_CTRL_33); AD2 V+ -> J13.14 (V_33)
  - AD2 GND -> board GND
- **Two methods** (`iq.method`, `--method`):
  - **`shunt`:** DMM DC volts across R13 (0.1 Ω) on the 100 mV range. This is everything drawn from VIN: the LDO, BGR, enable block, the level shifters' low side, and ~1.2 µA into the AD2 scope on VOUT. One count is 1 µA, which is fine for Iq but too coarse for shutdown current.
  - **`series`:** DMM DC amps (I and LO jacks) in place of one J1 jumper. The I jack goes on the INB pin (J1 3, 7, 15) and LO on the rail pin (1, 5, 13): current flows from INB into the rail, so the other way round reads negative (an issue). Until 2026-11-14 the wiring printout had them the other way round. Rails: `--rail bgr|vdd|ota` (in place of that J1 jumper; EN_VDD's ~0.3 nA is under the 10 nA count, so `--rail en` is refused: use `enable-current`), or between the PSU and VIN_DC (`--rail total`). It uses a fixed range, 10 mA unless `dmm_range` says otherwise, so the zero and every point are read on the same range. One count is 10 nA, so this resolves shutdown current. The meter's burden (~5 Ω on the 10 mA range) drops a little of that rail's voltage.
- **Does:**
  1. Switches the supply output off (it may have been left on), then reads the DMM: the zero (offset, thermal EMF). It's subtracted from every point. The spread of those readings (at least 3) sets the floor, never below one DMM count at the chosen NPLC (1 µV on 100 mV at 10 NPLC; coarser at shorter integration times).
  2. Shunt method: switches in a known load (`check_code`, 4.8 mA) once, to prove the DMM sees the current through R13.
  3. Sweeps VIN (1.4–1.9 V) with the LDO enabled, then disabled through the enable path.
- **Reports:** current per point. Points under the floor are shown as "< floor", not as a number.
- **Plot:** current against VIN (left) and VOUT at OUTA against VIN (right), one row per state. Enabled, VOUT at nominal shows the LDO regulated, so the rail under test was powered (in the series method, through the meter: an open meter leaves that rail unpowered). Disabled, VOUT near 0 V shows the enable turned it off. What it can't show is a meter shorted out by a J1 jumper left fitted: the rail is still powered and the meter reads ~0.
- **Setup checks:**
  - the load check misses by more than 15 %. If the DMM reads the LDO's own current before the load goes in but the load adds nothing, the issue names the load wiring (J10, VS_BUFF, DIO 0 on B0); otherwise the DMM leads
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
- **54621A cross-check:** optional `[xscope]`. Every edge is also captured on the 54621A, overlaid as "54621A", and its peak and settling time are compared. `--no-xscope` skips it. See [instruments.md](instruments.md#agilent-54621a-cross-check-scope).
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
After the rework, U2's V− (pin 4) is on −5 V, so it can drive VIN down to 0 V and across 1.7–1.9 V. With a `[buffer]` section in the config, `line-tran`, `psrr` and `buffer-check` run it on a split supply:
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
  - DIO [0, 1, 2, 3, 4] -> load bank bits B0..B4, AD2 GND -> board GND
- **Does:**
  1. **Interlock, stage 1:** powers the rail at only 1.0 V with W1 off, and checks that VIN stays low. If VIN follows the supply, the leads are on VIN: stop.
  2. **Interlock, stage 2:** stage 1 can't tell a correct bench from a VIN probe that's off VIN (both read ~0 V). So the rail goes to `dut.vin_max` (safe even if the supply were on VIN) and W1 steps between the two `proof_levels` (0.1 and 0.3 V). VIN must follow W1. If it doesn't (supply on VIN, probe off, buffer not driving), the test stops before the rail goes above `dut.vin_max`.
     - **Not yet tried on hardware:** the BUF634 must be able to drive the proof levels with its rail at 1.9 V. If the first real run stops here with the wiring right, change `proof_levels`.
  3. **Calibration:** raises the rail to `psu_voltage` and calibrates out the buffer's offset from VIN measured at the board (on the AD2's 5 V range). The steps land on `vin_low` / `vin_high` within 5 mV. The W1 correction is capped at 250 mV.
     - **Hard stop:** any VIN reading above `dut.vin_max` less the AD2's error margin (about 20 mV at 1.9 V).
  4. **Steps:** steps W1 between the two levels, triggering on VIN itself.
- **Computes:** everything from the transient analysis, plus the peak VOUT deviation per volt of VIN step (mV/V).
- **Setup check:** a buffer that can't reach a level (headroom, JP4, W1 wiring).
- **Options:**
  - `--low 1.75 --high 1.85`
  - `--load 19mA`
  - `--avg`, `--window-ms`, `--plot-us`, `--no-xscope`
- **Config:** `[line_tran]`.

### `psrr`: power-supply rejection vs frequency
- **Wiring** (single-rail buffer; JAFWAK with `[buffer]`: see [Split-supply U2](#split-supply-u2-jafwak-buffer)):
  - PSU OUT1 -> buffer rail VS_BUFF / VDD_BUF and GND. NOT on VIN: the rail goes above the LDO's VIN limit
  - AD2 W1 -> buffer input SMA (VIN_FG / V_SIG); buffer output onto VIN (JP4)
  - Scope 1+ -> VOUT at the output pins, 1- -> GND (short ground lead)
  - Scope 2+ -> VIN at the LDO input pins, 2- -> GND
  - DIO [0, 1, 2, 3, 4] -> load bank bits B0..B4, AD2 GND -> board GND
- **Does:**
  1. **Setup:** the same two-stage interlock and DC calibration as line-tran, at `vin`.
  2. **Sine:** at each frequency (`f_start` to `f_stop`, `points_per_decade`), W1 plays `vin ± amplitude`. The peak must be within `dut.vin_max` less the AD2's error margin: the config refuses a higher setting, the amplitude is capped from the *measured* DC level, and the run stops if the measured peak goes over.
  3. **Measurement:** several free-running AD2 captures of VIN and VOUT. Each is sampled at the rate that fits at least `cycles` periods into 8192 samples, up to 100 MS/s.
- **Analysis:**
  - **Sine fit:** a least-squares fit at the injected frequency on each channel (a lock-in). VOUT/VIN is averaged coherently over the captures, which keeps the phase and averages noise down.
  - **Levelling:** W1's amplitude is re-levelled if VIN's amplitude is off by more than 15 % (the buffer and board roll off at high frequency).
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
- **Spectrum analyzer:** dropped for now (2026-11-14). The AD2 resolves PSRR to about 70 dB at 50 mV, against a 40 dB spec at 10 kHz and 55 dB from PEX. Floor-limited points say "≥"; a spectrum-analyzer `PsrrMeter` would replace them once its driver exists.
- **Options:**
  - `--fstart`, `--fstop`, `--ppd`
  - `--amp-mv 20`
  - `--vin`
  - `--load`
  - `--avg`
- **Config:** `[psrr]`.

---

## Enable block (JAFWAK)

### `enable-sine`: enable comparator thresholds
- **Wiring:**
  - PSU -> VIN_DC (JP4 to DC); J1 9-11: EN_VDD from INB (other J1 rails as you like)
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
  - Remove the J1 9-11 jumper (EN_VDD); DMM I (3A/mA jack) -> J1 pin 9, DMM LO (COM) -> J1 pin 11
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
  - PSU -> VIN_DC (JP4 to DC); J1: each rail you want powered (BGR_VDD 1-3, VDD 5-7, EN_VDD 9-11, OTA_VDD 13-15)
  - Enable path: J6 7-8 (EN_CTRL_18 -> EN_AIN), J8 1-2 (EN_OUT -> EN_IN), J8 9-10 (~EN_OUT -> ~EN_IN)
  - AD2 DIO 5 -> J13.12 (EN_CTRL_33), DIO 6 -> J13.10 (~EN_CTRL_33)
  - AD2 V+ -> J13.14 (V_33), AD2 GND -> board GND
  - Scope 1+ -> J13 pin 4 (OUTA), Scope 1- -> J13 pin 6 (GND)
  - Scope 2+ -> TP3 (VREF, the BGR output), 2- -> GND; JP2 closed
- **Enable path:** DIO 5 → EN_CTRL_33 → FXLP34 level shifter → EN_CTRL_18 → enable block → EN_OUT → EN_IN. DIO 6 drives ~EN_CTRL_33 inverted. The AD2's V+ supplies the level shifters' 3.3 V side.
- **Does:** steps the enable line. The AD2 triggers on its own DIO edge (the Digital In trigger detector), so both scope channels are free. Each edge (enable, disable) is averaged.
- **Scope 2** (`--ch2`) is one of:
  - `vref`: TP3, the BGR output
  - `iin`: across R13, the input current (inrush)
  - `iout`: across R1, the output current
- **Computes:** 10 % / 90 % times from the edge, which include the level shifter and the enable block. Also rise or fall time, settling within 2 %, and overshoot. For currents: the levels before and after, and the peak.
- **Current resolution:** across the 0.1 Ω sense resistors on the AD2's 5 V range, one count is about 3 mA. A current step under 4 counts (about 12 mA) is reported as "under what the AD2 resolves", with no timings. That includes the quiescent current, so use `iq` for DC current. Inrush and load steps of 20 mA or more are measurable.
- **Setup checks:**
  - VOUT already up before enabling (the enable path isn't controlling the LDO)
  - VOUT not reaching nominal
  - VOUT or VREF not settled by the end of the capture (lengthen `--window-ms`)
  - VOUT still above 20 % of nominal at the end of a disable edge
  - VREF off `vref_nominal`, if set
- **Options:**
  - `--ch2 iin`
  - `--avg`
  - `--window-ms`
  - `--load`
- **Config:** `[startup]`, `[enable]`.
- **First hardware run:** the AD2's digital trigger needs checking.

---

## Temperature (JAFWAK)

### `temp`: temperature log
- **Wiring:**
  - PSU + -> VIN_DC (J7), PSU - -> GND (J11); JP4 2-3 "DC"; JP2 closed
  - VS_BUFF (J4) at 5.0 V from a separate supply (powers the TMP235 on TEMP (TP7)); J10 "LOAD" only after VS_BUFF is up
  - Scope 2+ -> J13 pin 5 (TMP), Scope 1+ -> J13 pin 4 (OUTA); both - -> J13 pin 6 (GND)
  - DMM V / COM -> VREF (TP3) / GND (TP2), Hi-Z if the meter has it
  - LDO enabled (J6 1-2 "HI", J8 1-2 and 9-10 "LDO") or `--enable dio`
  - Optional 1 mA load: 1.2 kOhm from VOUT (J5) to GND (J12)
- **Does:** with the LDO on, samples every `interval` (2 s) for `duration` (30 min, at most 2 h): T from the TMP235 on the AD2's 5 V range, T = (V − 0.500 V) / 0.010 V/C; VREF on the DMM; VOUT on the AD2; the supply current. Ctrl-C ends the log early, keeps the data and still shuts down. First bring-up plan, Test 3.10: start at room temperature, then put ice in a sealed bag on U1.
- **Computes:**
  - **Plateaus:** a sample is stable when T held within `stable_delta` (0.5 C) for `stable_window` (30 s). Each run of stable samples is averaged into one point (T, VREF, VOUT).
  - **Tempco:** with two or more plateaus at least `min_spread` (2 C) apart, a line through the plateau means gives VREF and VOUT in ppm/C. It works on temperature differences, so the AD2's offset (~10 mV ≈ 1 C, unverified) mostly cancels. Simulated: 58.9 ppm/C, so room to ~5 C moves VREF by only ~1.4 mV.
- **Setup checks:** every T within `temp_min`..`temp_max` (−20 to 80 C: scope 2 on J13 pin 5, VS_BUFF powered); VOUT near nominal; VREF within 20 % of `vref_nominal` (DMM on TP3).
- **Notes:** stopped with Ctrl-C; no tempco (fewer than two plateaus far enough apart); the supply current rising by more than 20 % (condensation on the board).
- **Options:** `--minutes 30`, `--interval 2`, `--vin`, `--no-dmm` (no VREF), `--enable dio`.
- **Config:** `[temp]`.

## External load: `--ext-load-ohm`

A fixed resistor on VOUT (e.g. 1.2 kOhm on VOUT (J5) to GND (J12), ~1 mA) is part of the load the
LDO sees. `--ext-load-ohm 1200` on `load-reg`, `line-reg`, `load-tran`, `enable-startup` and `temp`
(or `loadbank.external_ohm`) adds VOUT / R to every load estimate, and the wiring lists it.
`dut.i_max` is checked with it included for the codes the run uses, before anything powers up: with
1.2 kOhm, code 4 (19.2 mA) becomes 20.2 mA and is refused (use `--load 3`, a bigger resistor, or
none). `load-reg`'s default sweep ("every code within dut.i_max") leaves such codes out and says so.

## Enable from the AD2: `--enable dio` (JAFWAK)

`smoke`, `load-reg`, `line-reg`, `load-tran`, `vref-line` and `temp` don't need the enable line: by default the LDO is tied on with jumpers (J6 1-2 "HI", EN_AIN = VIN, with J8 1-2 and 9-10 "LDO"), so it turns on as VIN rises. With `--enable dio` the run drives it from the AD2 through `[enable]` instead, the same path `iq` and `enable-startup` use.

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
