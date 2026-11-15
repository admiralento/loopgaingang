# Bench log

What was built, what was measured, and what went wrong along the way. Newest session first.

## Reruns needed (as of 2026-11-14)

Bring-up plan Tests 1-4 have been run on JAFWAK. These changed since, so their runs need repeating:

| Test | Command | Why |
|---|---|---|
| 2 | `enable-current` | New (replaces `iq --rail en`); not run yet |
| - | `enable-startup` | Rebuilt 2026-11-14: VOUT and EN_IN on the AD2, input current from the 54621A's INA − INB (Ext Trig on EN_AIN); no VREF probe. The two runs that morning had VREF loaded by the scope |
| 3 | `vref-line` | Now also reads VREF at 10 GOhm and 10 MOhm (the by-hand "VREF level" step) |
| 3 | `iq --method series --rail bgr` | The wiring printout had the DMM's I and LO the wrong way round on J1 (I belongs on INB, pin 3): a run that followed it read negative and was marked `_INVALID` |
| 4 | `iq` (shunt) | The load-check wiring (VS_BUFF, J10, DIO 0 -> B0) wasn't printed; the 2026-11-13 run failed its load check for that reason |
| 4 | `iq --method series` on `ota` / `vdd` | Same J1 polarity fix as Test 3 (`--rail total` was right) |
| 1 | `enable-sine` | Only if J1 5-7 (VDD) wasn't fitted: VDD must be up in every test |
| - | `loadbank-check` | Load cell refitted 2026-11-14 (1200 / 680 / 330 / 150 / 82 Ω): prove each bit before any load test |

New, for Tests 5 onward (not run yet): `enable-sine --output vout` (Test 5), `line-reg --split-rails`
(Test 7, Method B). The 1 mA the plan got from 1.2 kOhm on J5 is now load code 1, so `--ext-load-ohm`
isn't needed for Tests 6-8; load-tran steps 1 <-> 20.04 mA (codes 1 <-> 22).

## 2026-11-14: `dut.i_max` 21 mA, no `iq --rail en` (no hardware run)

- **`dut.i_max` raised from 20 to 21 mA (approved 2026-11-14)** so the refitted load cell reaches 20 mA: code 22 (B1 + B2 + B4) is 20.04 mA at 1.2 V; code 23 (21.04 mA) and up are refused. `line_reg`, `line_tran` and `psrr` now default to `load_code = 22`, and `load_tran` steps codes 1 <-> 22 (trigger still DIO 4); `load-reg` sweeps codes 0-22.
- **`iq --rail en` removed:** EN_VDD's ~0.3 nA is under the DMM's 10 nA count. `enable-current` measures it.
- **AGENTS.md:** `en_ain_dc`, `split_rails_on` and `split_rails_off` added to the Bench methods that may put a voltage on the DUT.

## 2026-11-14: load cell refitted for a 1-20 mA sweep (no hardware run)

- **What changed on the board:** B0..B4 are now 1200, 680, 330, 150, 82 Ω (1 %), for 1.00, 1.76, 3.64, 8.00, 14.63 mA at 1.2 V. Every bit is more than the sum of those below it, so the current rises with the code: 0 to 19.3 mA in ~1 mA steps over codes 0-21. Code 22 (20.04 mA) and up are refused by `dut.i_max` (20 mA, unchanged). `load_cell.kicad_sch` still shows the old values; it hasn't been touched.
- **Config (`jafwak.toml`):**
  - `[loadbank] resistors = [1200, 680, 330, 150, 82]`;
  - `load_reg` sweeps codes 0-21 (22 points);
  - `line_reg`, `line_tran`, `psrr`: `load_code = 21` (19.3 mA, was 4 = 19.2 mA);
  - `load_tran`: codes 1 <-> 21 (1.0 <-> 19.3 mA), trigger on DIO 4 (B4);
  - `startup.load_code` and `iq.check_code` = 5 (4.6 mA, was code 1 = 4.8 mA);
  - `temp.load_code = 1` (1.0 mA): the plan's 1.2 kOhm on J5 is no longer fitted by hand, and the wiring printout no longer asks for it;
  - `loadbank_check.voltage = 1.2 V` (was 0.8 V): B4 draws 14.6 mA, and B0's 1 mA reads better at the higher voltage.
- **Not verified:** the new values on the board (run `loadbank-check`), and which reference designator holds which value.

## 2026-11-14: `loadbank-check` after the refit, and its fixes (no new hardware run)

**What ran:** four runs from 13:44 on, at 1.2 V, current from the supply's readback.

**What it measured:**
- Every bit drew 64–65 % of V/R: B0 0.69 / 1.00 mA, B1 1.14 / 1.77, B2 2.36 / 3.64, B3 5.16 / 8.01, B4 9.47 / 14.65 mA.
- The 13:46 run also had B2 and B3 swapped, fixed after.

**What went wrong in the check:**
- **False swap reports:** it reported "DIO 3 wired to J2, DIO 4 to J3" for what is a uniform shortfall.
- **The small bits passed only on the 1.5 mA absolute tolerance,** which was bigger than B0's whole 1 mA, so a dead B0 would have passed.
- **Unexplained:** the ratio is the same from 82 to 1200 ohm, so it is not a FET's on-resistance. Either the bank sees about 0.77 V, or the supply's current readback is about 35 % low.

**What changed:**
- The current now comes from the DMM in series (`current_from = "dmm"`, 1 A range), with the supply's readback beside it and a note when they disagree.
- A uniform shortfall now gets one diagnosis.
- Absolute tolerance is 0.05 mA with the DMM, 0.3 mA with the supply.

**Next run:** the DMM shows which explanation is right. If the DMM agrees with V/R, the supply's readback is off; if it also reads 65 %, measure the voltage across a resistor.

## 2026-11-14: the load cell doesn't need VS_BUFF

- **Found at the bench:** JAFWAK's load bank switches without VS_BUFF (J4) powered. The config's note that VS_BUFF powers U4, the gate driver, came from the schematic and was wrong for this board.
- **What changed:**
  - `[board] bank_supply = []`, so no load-bank test (load-reg, line-reg, load-tran, smoke, iq's load check, enable-startup) asks for the separate 5 V supply any more;
  - iq's "load didn't switch in" hint no longer names VS_BUFF;
  - `temp` still asks for VS_BUFF, because the TMP235 runs from it.

## 2026-11-14: `enable-startup` with a load; the supply left at 1.8 V after runs (no hardware run)

- **At the bench:** after fixing the INA probe, there was little inrush to see. `[startup] load_code = 1` (4.8 mA) is now the default.
- **`psu.idle_voltage = 1.8`:** every run now ends with the supply output off and the setpoint at 1.8 V with the 10 mA limit, instead of 0 V, so a manual Output On gives the normal VIN.
  - It is programmed only after the output is confirmed off; otherwise SHUTDOWN FAILED.
  - It must be within the VIN ceiling.
- **Demo board:** unchanged (0 V).

## 2026-11-14: `enable-startup`: input current from the 54621A (no hardware run)

- **Earlier layout, same day:** AD2 across R13 and R1, VOUT on the 54621A. Never run, replaced the same day.
- **Grounds:** a continuity check showed the 54621A's grounds and the AD2's GND are all common.
- **New layout:**
  - AD2 scope 1 on OUTA (VOUT), scope 2 on EN_IN
  - 54621A ch1 on INA and ch2 on INB, both on 20 mV/div centred on VIN
  - the 54621A triggers on EN_AIN through Ext Trig at 0.9 V (1.8 V logic)
  - I_in = (INA − INB − zero) / R13, where the zero is INA − INB while the LDO is disabled (it removes the channels' offset difference)
  - the output current is no longer measured; `--channels` (`iin` / `iout`) is removed
- **Window:** JAFWAK's default window is now 10 ms.
- **Not checked on hardware:**
  - the 54621A's Ext Trig on EN_AIN; the trigger level range on Ext is from memory
  - the EN_IN probe point (`board.enable_ldo_in`: which J8 pin)
  - whether the 10 mA supply limit makes VIN sag at startup (it would show as INA / INB clipped)
  - the 54621A's channel mismatch: it is zeroed out, but only if it stays steady over a capture
- **Resolution:** one 8-bit count is about 6 mA on 0.1 Ω at 20 mV/div, and the run reports steps under 4 counts (25 mA) as bounds. Expect the inrush peak, not the steady current.

## 2026-11-14: `enable-startup` on JAFWAK: a scope on VREF pulls it down

**What ran:** `enable-startup --ch2 vref`, twice, at VIN 1.8 V with no load and the enable driven from the AD2:
- 1 ms window: `_INVALID`, because VOUT hadn't settled
- `--window-ms 10`: marked valid

**What it measured:** VOUT and VREF both settled at about **0.98 V** instead of 1.2 V. VOUT follows VREF, because the LDO has no feedback loop. The day before, `iq` at the same VIN read VOUT 1.196 V with no scope on VREF.

**Cause, found at the bench:** AD2 scope 2 (1 MOhm) on TP3 loads the BGR's output and pulls VREF down, and VOUT with it. The run's checks missed it:
- VOUT's ±20 % window accepted 0.98 V;
- the VREF check was off (`startup.vref_nominal` was unset).

These numbers are not the board's: discard both runs.

**What changed:**
- No test puts a scope on VREF any more.
- `psrr --measure vref` (10x probe on TP3, added the same morning) was removed.
- `enable-startup` measures input and output current instead (see the next entry).
- VREF is read only with the DMM at 10 GOhm.

**First suspect, ruled out:** the new 10 mA supply limit. The board draws about 0.25 mA. The concern that the E3648A's current-limit setting has roughly 10 mA of offset (from memory, not checked) still stands for low limits.

## 2026-11-14: by-hand steps scripted (no hardware run)

- **vref-line:** VREF at vin_high with the DMM at 10 GOhm and at 10 MOhm; the difference gives the BGR's output resistance (a drop over 0.5 mV is a note). New `HP34401A.set_input_z`.
- **psrr --measure vref:** the BGR's rejection at TP3 through a 10x probe. Removed again the same day: a scope on VREF loads the BGR (see "A scope on VREF pulls it down" below).
- **enable-sine --output vout:** VOUT (OUTA) following a slow triangle through the whole enable path (J8 on LDO); the EN_AIN levels where VOUT turns on / off, with a note on how much the shutdown time shifts the turn-off level.
- **line-reg --split-rails:** dropout Method B, BGR_VDD / OTA_VDD on PSU OUT2 (J1 pins 1 and 13, jumpers 1-3 / 13-15 out). VIN up first and above `vin_floor` while the rails are up; OUT2 drawing over 1 mA (a jumper left in) stops the run; shutdown drops the rails before VIN.
- **--ext-load-ohm:** a fixed resistor on VOUT in every load estimate and in the `dut.i_max` check (per run, before power-up). With 1.2 kOhm, load code 4 is 20.2 mA and refused, so "1 to 20 mA" becomes 1 to 15.4 mA (code 3) unless dut.i_max is raised.
- **line-reg:** the report points to vref-line's BGR minimum VIN when a knee is found.
- **J1 series wiring fix:** per the KiCad nets (rail on pins 1-2 / 5-6 / 9-10 / 13-14, INB on 3 / 7 / 11 / 15), current flows INB -> rail, so the DMM's I jack goes on INB. The iq series and enable-current printouts had it on the rail.
- **Dropped from the plan:** the by-hand enable static levels (the enable-sine plots show them), the EN_VDD spike measurement (covered by enable-current's charge per cycle), and the spectrum-analyzer PSRR (the AD2 covers it to ~70 dB; noted for later). Output noise (Test 14) stays by hand until the spectrum-analyzer driver exists.

## 2026-11-13: default config and enable edge plots (no hardware run)

- **Default config:** every command uses `configs/jafwak.toml` unless `-c` or `LDO_BENCH_CONFIG` names another, and prints the config it loaded first. Demo-board runs need `-c configs/demo_board.toml`.
- **enable-current (new):** EN_VDD's current against the frequency of a 0-VIN square on EN_AIN, 10 Hz to 1 MHz, with the DMM in place of J1 9-11; the slope gives the charge per cycle. It replaces `iq --rail en`, whose ~0.3 nA static current is under the DMM's 10 nA count (now refused). EN_OUT is left unprobed: a scope's ~24 pF would add ~43 µA at 1 MHz.
- **VDD always powered:** enable-sine's wiring now asks for all four J1 rails (it said "the others as you like"); VDD (J1 5-7) must be up in every test, as only the VDD ring has ESD clamps.
- **iq load check (bench, 2026-11-13):** a shunt run failed its load check (code 1 should add 4.8 mA, the DMM saw -0.2 uA) because the load cell wasn't wired: the iq wiring printout never listed it. It now lists VS_BUFF, J10 "LOAD" and DIO 0-4 for the shunt method, and when the DMM already reads the LDO's own current the issue names the load wiring instead of the DMM leads.
- **iq plot:** VOUT at OUTA against VIN beside each current panel, so a small current can be told apart from a meter that isn't in the path (the LDO doesn't come up) or an enable that doesn't work.
- **enable-sine edges:** the plot adds one rising and one falling output edge close up, every sample marked, with the 10 % / 90 % levels. A sine or triangle run whose rise / fall times are only bounds (2 kS/s at 1 Hz, 2 MS/s at 1 kHz) says so and points to `--shape square --freq 1e5` (100 MS/s), which resolves them to 20 ns.

## 2026-11-13: code review and clean-up (no hardware run)

Three reviewers went over the whole package; the fixes were made with a test each, and the gate stayed green after every commit.

**Bugs fixed:**
- **The test suite reached real hardware.** `test_missing_scope_is_not_fatal` let `open_bench` scan the real RS-232 ports, so `poe verify` on the bench Mac sent commands to the 34401A. Now any real bus search fails a test.
- **EN high through shutdown after an error** in `iq` and `enable-startup`: they drove the enable without telling the Bench. Shutdown now drives EN low whenever anything drove it.
- **A 34401A DC volts overload** came back as 9.9E+37 "V"; it now reads ±inf.
- **`buffer-check`** recorded a Bench limit refusal as a setup issue; it now stops the run.
- **The single-rail buffer interlock** lost its end-to-end tests when JAFWAK moved to the split supply; they now run on both paths (checked by disabling the proof: four tests fail).
- **Smaller:** `wavegen_off` left VIN marked as driven; `_ensure_psu` ignored a different current limit silently; other exceptions skipped the SHUTDOWN FAILED banner; the `iq` sweep could stop short of `vin_stop`; `compare` refused runs saved under an older config.

**Clean-up:**
- **Wiring:** one line per connection, built from a new `[board]` section; `iq` and `enable-startup` probe VOUT at OUTA (J13 pin 4) like the rest.
- **CLI:** one run helper and one override helper; choice options listed in `--help`; `LDO_BENCH_CONFIG`.
- **Shared code** in the Bench, config, analysis, plotting and drivers; dead code removed.
- **Tests:** shared helpers; every command run on the simulator (`poe sim-all`); regrouped by topic; warnings are errors.

**Left as is:** `summary.json` key names (renaming would break `compare` against saved runs), the load-tran / line-tran edge plumbing (similar but separate), simulator behaviour (scope clipping, the three "off" currents), and the driver Protocols' missing methods (`aux`, `set_vplus`, `read`, used through `hasattr`).

## 2026-11-13: scripts for the bring-up plan's by-hand tests (simulator only)

The bring-up plan was rewritten as "Bring up 2" (Google Doc): each test now says what it is, why, the setup by silkscreen label and connector pin, and the command. Four of its by-hand tests now have scripts. **None has run on hardware.**

- **`vref-line`** (plan Test 2.7): VREF (TP3) on the DMM while VIN steps 1.90 → 1.40 V, LDO on. Line sensitivity and the BGR's lowest working VIN.
- **`enable-sine --shape square`** (plan Test 1.8): EN_AIN → EN_OUT propagation delay per output edge. Use 100 kHz for 10 ns samples.
- **`temp`** (plan Test 3.10): TMP235 on J13 pin 5 (AD2), VREF and VOUT logged for up to 2 h; tempco from stable plateaus.
- **`--enable dio`** on smoke, load-reg, line-reg, load-tran, vref-line and temp: EN from the AD2 (J6 7-8) instead of tied on (J6 1-2).

**Confirmed on the board (Alek):** J8's silkscreen is swapped on the EN_IN half. "HI" is printed over 5–6, which is GND, and "LO" over 7–8, which is VIN. The ~EN_IN half is right. Go by pin numbers.

**Still by hand:** dropout "Method B" (BGR and OTA on a second supply), BGR PSRR, the HP 8591E readings and output noise.

**U2 reworked and tested by hand (Alek, 2026-11-13).** The scripts now run it on its split supply (`[buffer]` in `jafwak.toml`): −5 V on pin 4 from PSU OUT2 wired reversed, VS_BUFF at 5.0 V with a 100 mA limit (approved). Simulator only.
- **Found while planning it:** the old `line-tran` / `psrr` turned W1 off during their interlock, between its stages and at the end, and the shutdown turned the wavegen off before the supply. After the rework that leaves U2's input floating with its rails up, which can put up to ~3.4 V on VIN. Now W1 holds 0 V whenever the rails are up, and the shutdown keeps it there until the outputs are off (shutdown order approved).
- **Also:** V− mustn't reach −5 V while VS_BUFF is low (U2's output can't then rise to 0 V and VIN goes negative), so the two rails ramp together in 0.1 V steps with VIN read after each.
- **New `buffer-check`:** U2 alone with the chip off (first plan I.1–I.5). Run it first on the board.
- **Limit found:** W1 stays within `dut.vin_max` (1.9 V), so with a negative U2 offset the output can't reach 1.90 V. `buffer-check` checks 1.70 / 1.80 / 1.85 V; line-tran and psrr never go above 1.85 V.

## 2026-11-12: JAFWAK enable block, first `enable-sine` runs

Run by hand at the bench. W1 straight onto EN_AIN (not through the EN_FG SMA). AD2 scope 2 on TP6 (EN_AIN), scope 1 on J8.2 (EN_OUT). VIN 1.8 V from the PSU on VIN_DC.

**New:** `--shape triangle` (a linear ramp up and down), for a slow sweep through the thresholds.

**Triangle, 0–1.8 V at 1 Hz, 8 captures** (`results/enable-sine/20261112-144341_JAFWAK_enable-sine`): valid.

| | Measured |
|---|---|
| VT+ / VT− | 456.3 mV / 456.2 mV (spread under 0.2 mV) |
| Hysteresis | 0.04 mV, i.e. none |
| VOH / VOL | 0.756 V / 0.001 V |
| Duty | 74.5 % |
| Edges per input cycle | 1.00, no chatter |

- **Notes the run raised:** VOH under 80 % of EN_VDD, and no hysteresis. Neither explained yet. A 0.76 V high level from a block on a 1.8 V EN_VDD, and no hysteresis at all, need checking against the design before they're taken as the block's behaviour (EN_VDD at the block, scope 1's probe point).
- **Duty check:** 74.5 % is what a 0.456 V threshold on a 0–1.8 V triangle should give: (1.8 − 0.456) / 1.8 = 74.7 %.
- **Edge times:** 791 µs at a 488 µs sample interval: that's the sampling, not the edge. Now reported as "under 977 µs".

**Sine, 0–1.8 V at 1 kHz** (`..._144708_..._INVALID`, `..._144747_..._INVALID`): **never triggered**.
- **Not the wiring:** the untriggered captures show EN_AIN swinging −0.003 to 1.798 V at 1 kHz, crossing the 0.90 V trigger level every cycle. EN_OUT switches at 0.457 V / 0.456 V, the same as with the triangle.
- **Cause:** a race in the AD2 driver. After arming it polled (every 2 ms) for the Armed state before continuing. A free-running 1 kHz sine triggers within a millisecond of arming, and the 4 ms capture finishes about 5 ms after the start. So the driver missed Armed, waited for it until the timeout, and reported "never triggered". The triangle's 4 s capture stays Armed long enough to be seen.
- **Fixed:** the arm wait accepts Triggered / Done too. A capture marked `free_running` (only enable-sine) carries on. Any other capture that triggers before its stimulus now raises "triggered before the stimulus", since the step isn't in it. Tested on a fake AD2 that skips Armed; **not yet re-run on the board.**
- **Edge times at 1 kHz:** 0.65–0.77 µs at a 0.49 µs interval, the same 1.3–1.6 intervals as at 1 Hz. So the edge is faster than either rate resolves.

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
- **Explained:** R5 is a 100 mΩ sense resistor in series between `/LDO_VOUT` and `/VOUT`. 100 + 2.7 mΩ = 102.7 mΩ against the 102.9 mΩ measured, and 65 mA × 0.103 Ω = 6.7 mV against the 6.6 mV load-tran shift. So today's probes were on `/VOUT` and the 2026-11-06 DMM was on `/LDO_VOUT`.
- **Probe points:** TP4 "OUT_S2" is on `/LDO_VOUT` (the regulator); TP3, J8 and J2 are on `/VOUT`. Read from the PCB file; to be confirmed on the board.
- **Transient peaks:** today's load-tran peaks include R5's 6.7 mV IR step. Probe TP4 to measure the regulator's own response, with `settle_reference = "pre"`.

**At TP4** (`/LDO_VOUT`, before R5; AD2 scope 1, 54624A ch1 and the DMM all on TP4; `settle_reference` back to `"pre"`):
- **`load-reg`** (`results/load-reg/20261110-114145_TPS7A7001-demo-board_load-reg`): valid.
  - **DMM:** 0.99489 → 0.99459 V over 0–123.9 mA, **2.4 mΩ** (R² 0.997), ΔVOUT 0.30 mV. This matches 2026-11-06 (2.7 mΩ, 0.34 mV).
  - **AD2:** within 0.4–0.7 mV of the DMM.
- **`load-tran`** (`results/load-tran/20261110-114323_TPS7A7001-demo-board_load-tran`; AD2 scope 2 back on DIO 4): valid.

| | AD2 | 54624A |
|---|---|---|
| step up 4 → 69 mA: peak / settling | **−17.34 mV / 19.9 µs** | −17.00 mV / 16.4 µs |
| step down 69 → 4 mA: peak / settling | **+13.52 mV / 205.8 µs** | +13.45 mV / 177.7 µs |

  - **Level shift:** VOUT is 0.99444 V at 4 mA and 0.99424 V at 69 mA, 0.2 mV apart, so the R5 shift is gone.
  - **Peaks:** they agree within 0.34 mV, and match 2026-11-06 (−16.4 / +14.6 mV).
  - **Settling:** the two scopes differ by one ring period. On the step down the ringing near 180–205 µs is about as large as the ±1.35 mV band, so a 0.1 mV difference moves which ring is "last outside" by ~30 µs. Quote it as ~180–210 µs to ±10 %. A wider band or more averages would make it steadier.

**`load-tran --avg 64` at TP4** (`results/load-tran/20261110-114729_TPS7A7001-demo-board_load-tran`): valid.
- **Peaks:** −17.30 / +13.45 mV on the AD2 (54624A within 0.38 mV).
- **Noise:** 0.038 mV rms.
- **DMM beep fixed:** the DMM's port was reserved before the scope search, so the scope's 8N1 probe never reached it.
- **Settling at different bands**, re-analysed offline from this run's saved waveforms. At 10 % it reproduces the run's own numbers; nothing was saved.

| | 10 % | 15 % | 20 % |
|---|---|---|---|
| step up, AD2 / 54624A | 20.0 / 16.4 µs | 15.6 / 15.6 µs | 12.3 / 12.3 µs |
| step down, AD2 / 54624A | 205.8 / 203.5 µs | 149.7 / 148.6 µs | 146.2 / 119.6 µs |

- **64 averages** bring the step down into agreement at 10 % (2.3 µs, from 28 µs at 16 averages). The AD2's own values match the 16-average run.
- **A wider band isn't a general fix.** At 20 % a different ring sits on the band edge and the scopes disagree again.
- **Kept:** 10 % (the WaveForms definition). Use `--avg 64` when settling matters.
- **Step up gap:** 3.6 µs at 10 %. A guess: the 54624A's 410 ns/pt and 1 µs smoothing miss a narrow last excursion.
- **Default:** `load_tran.averages` is now 64 in both configs.

**`load-tran` with the 64-average default** (`results/load-tran/20261110-120824_TPS7A7001-demo-board_load-tran`, first with `plot_zoom.png`): valid.
- **AD2:** −17.31 mV / 19.90 µs (step up), +13.53 mV / 205.70 µs (step down). The 54624A peaks are within 0.39 mV.
- **Correction:** 64 averages did *not* fix the step-down disagreement. The 54624A gave 178.5 µs this time (203.5 µs at 11:47, 177.7 µs at 11:43). The AD2 gave 205.7–205.8 µs on all three runs and 19.9–20.0 µs for the step up.
- **Why:** on each edge one ring sits right at the ±10 % band edge, and `plot_zoom.png` shows it for the step up. The trough at ~20 µs just reaches the band's lower edge: the AD2 counts it as outside, the 54624A (a fraction of a mV shallower) as inside.
- **Result:** settling ≈ 20 µs up and ≈ 206 µs down to ±10 %, from the AD2. The 54624A's settling times on these edges aren't repeatable. Its peaks are.
- **Plot flaw:** the legend overlapped the step-up "settled" label. Fixed: there is now one figure legend below the panels.

**`load-tran` again, at TP4 with 64 averages** (`results/load-tran/20261110-121414_TPS7A7001-demo-board_load-tran`; first with the legend below the panels): valid.
- **AD2:** −17.38 mV / 19.9 µs (step up), +13.54 mV / 205.5 µs (step down). Noise 0.028–0.036 mV rms.
- **54624A:** peaks within 0.44 mV. Its step-down settling was 204.3 µs this time, on the later ring.
- **Over the four TP4 runs:** the AD2 settles in 19.9–20.0 µs and 205.5–205.8 µs, with peaks within 0.1 mV.

**`load-tran`, VOUT missing** (`results/load-tran/20261110-121546_TPS7A7001-demo-board_load-tran_INVALID`, exit 3):
- **What both scopes saw:** AD2 scope 1 read −0.3 to +0.1 mV. The 54624A read a flat 0.9206 V, the bottom of its 0.92–1.08 V screen. DIO 4 swung 0 → 3.27 V on both, so the trigger and load bank were working.
- **Cause:** a deliberate test. The supply was unplugged to check that nothing gets made up. The run was marked `_INVALID`, saved the real readings and exited 3.
- **Limit:** `load-tran` doesn't read VIN or the supply current, so it can only say VOUT is missing, not that VIN is. Fixed: `load-tran` now checks the supply current rises by the load step (sim-tested; the threshold was checked against today's load-reg readings, 63.1 mA rise vs 64.9 mA expected).

**`load-tran` with the supply current check** (`results/load-tran/20261110-122334_TPS7A7001-demo-board_load-tran`): valid.
- **Supply current:** 4.94 mA at code 1 and 68.06 mA at code 17, a 63.1 mA rise against 64.9 mA expected. The check passed on hardware.
- **AD2:** −17.38 mV / 19.9 µs (step up), +13.49 mV / 205.6 µs (step down). 54624A peaks within 0.42 mV.
- **Repeatability:** five TP4 runs give AD2 settling of 19.9–20.0 µs and 205.5–205.8 µs.

**`load-tran` with the supply leads unplugged on purpose** (`results/load-tran/20261110-122446_TPS7A7001-demo-board_load-tran_INVALID`, exit 3):
- **Supply current check:** flagged it. 0.18 mA at code 1 and 0.20 mA at code 17, a 0.0 mA rise against 64.9 mA expected: "Is VIN reaching the board…".
- **VOUT check:** flagged it, at 0.2 mV.
- **54624A clipping check:** flagged both edges (2000 of 2000 points at 0.920 V), so no cross-check peak was reported.
- **Result:** both new checks are now tested on hardware, in the passing and the failing case.
- **Gap found:** the 54624A cross-check reported "peak −0.00 mV" with no issue, because the clipped 0.92 V is within the ±20 % VOUT tolerance. Now a VOUT trace touching its screen edge is flagged as clipped. This flags the run above and none of the six valid runs today.

**AD2 vs 54624A DC offset** (pre-step VOUT from the saved waveforms):
- **Readings:** the 54624A reads +5.8 mV above the AD2 on `/VOUT`, and +7.1 / +7.2 mV at TP4 (16 / 64 averages). At TP4 the DMM read 0.99487 V at 4 mA and the AD2 0.4–0.7 mV below it, so the 54624A is the one off, by about +6.7 mV.
- **Fixed offset:** the same at 4 and 69 mA (7.13 / 7.26 mV), so it isn't a ground drop carrying load current.
- **Probable cause:** offset accuracy with ch1 offset at 1.0 V for 20 mV/div. From memory the 54620 spec is about ±(2 % of offset + 1 % of full scale), ~±20 mV; not checked against the datasheet.
- **Peak difference:** the 0.3–0.4 mV between the scopes fits the 54624A's ~±2 % gain accuracy (also from memory).
- **Effect:** peaks and settling are measured from each scope's own pre-step level, so the offset doesn't touch them. Absolute VOUT comes from the DMM.
- **To try:** Self Cal on the 54624A after warm-up.

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
