# Temperature test: how to run it (JAFWAK)

A step-by-step guide to measuring VREF against temperature: the commands, how long each takes, and how
to get clean data. What the code does, its options and its checks are in
[tests.md](tests.md#temp-temperature-log-cold-run-and-hot-run).

The goal is VREF's curve against temperature (the bandgap's parabola) in a CSV, to compare with
simulation. It comes from two runs, joined at the end:
- **Cold run** (`temp`): ice by hand, from room temperature down and back up.
- **Hot run** (`temp --heater`): the run ramps a power resistor on PSU OUT2 up to 60 °C and back.

## Before you start

- **Run from `ldo_bench\`**, as `uv run python -m ldo_bench ...`. On Windows with Smart App Control on, the
  `ldo-bench` launcher is blocked ("An Application Control policy has blocked this file"); `python -m`
  runs the same code. From the repo root, `python -m ldo_bench` fails with "No module named
  ldo_bench.__main__".
- **WaveForms** must be installed (the AD2 reads the TMP235 and VOUT). Without it, `discover` reports
  "could not load the Digilent WaveForms runtime".
- **Every run prints its wiring and setup steps** and waits for you to confirm before powering
  anything. Read them each time: the cold and hot runs differ.
- **Try it on the simulator first** if anything changed: add `--sim` (`--save-sim` keeps the run,
  marked SIM).

## The runs, in order

| Step | Command | Time |
|---|---|---|
| 1. Check the instruments (nothing powered) | `uv run python -m ldo_bench discover` | ~10 s |
| 2. Cold run (ice) | `uv run python -m ldo_bench temp --minutes 90` | ~60-90 min; you end it with Ctrl-C |
| 3. Hot run (heater) | `uv run python -m ldo_bench temp --heater` | ~80 min from 22 °C; ends by itself |
| 4. Join them into `curve.csv` | `uv run python -m ldo_bench temp-curve results\temp\<cold run> results\temp\<hot run>` | a few seconds; no instruments |

About 2.5-3 h of bench time in all, plus the time for the board to come back to room temperature
between the two runs. Each run prints its folder name when it finishes.

`discover` should list the PSU (COM7), the DMM (COM8) and the AD2. The PSU and DMM ports are the ones
seen on 2026-11-21; they can change.

### Cold run: `temp --minutes 90`

1. **First ~3 min:** room temperature.
2. **Next ~15-20 min:** ice on. It cools the board to roughly 5-10 °C; take it off once T stops
   falling. The fast drop when the ice goes on is over 1.5 °C/min and stays out of the curve.
3. **Next ~40-60 min:** the board warms back up under the box. This slows as it nears room temperature.
4. **Ctrl-C** while T is still a degree or two below room temperature, and start the hot run straight
   away. The hot run then begins inside the cold run's range, so the two overlap and `temp-curve` can
   check the join (on 2026-11-21 they missed by 0.4 °C). The data is kept and the bench shuts down
   normally.

`--minutes 90` is only the upper limit; up to `--minutes 120` is allowed.

### Hot run: `temp --heater`

- **Steps:** resistance check at 0.2 V (~10 s), ramp up at 1 °C/min to 60 °C (~38 min), 5 min at 60 °C,
  ramp down at 1 °C/min to the start + 2 °C (~36 min). Then the heater turns off and the run ends.
- **Length:** `--minutes` defaults to 120 (the 2 h maximum). The run refuses to start if the profile
  won't fit.
- **Start:** straight after the cold run, the box and board left as they are apart from swapping the
  ice for the heater. The run won't start within 5 °C of 60 °C.
- **Let it finish:** the down ramp gives the cooling half that cancels the sensor lag. On 2026-11-21 it
  was stopped at 44 °C, so below that the hot run had heating samples only.
- **Interlocks:** the heater goes to 0 W and the log stops if T goes above 70 °C, if the TMP235 reading is
  lost, or if the board doesn't warm while the heater is on (heater not on the board). Ctrl-C stops it
  too, and every shutdown turns the heater off first.

### Joining the runs: `temp-curve`

- **Inputs:** the cold run's folder first, then the hot run's.
- **Output:** a new folder under `results\temp-curve\` with `curve.csv`, `bins.csv`, `summary.json` and
  `plot.png`. The source folders are only read.
- **Excluding a disturbance:** if a run shows a VREF drop that isn't temperature (as on 2026-11-21), add
  `--exclude RUN:START-END`, e.g. `--exclude 1:40-110` (run 1, 40 to 110 s). The exclusion is listed in
  the summary.
- **Overlap:** the summary reports the mean VREF difference between the runs where they overlap. A
  large one means the change of setup moved VREF.

## Setup for both runs

- **VS_BUFF** (J4) from a separate 5.0 V supply, on before you confirm the wiring. It powers the TMP235
  and U2. PSU OUT2 is the heater's.
- **U2's V− wire** (the split-supply rework) on GND, not on the PSU.
- **Load:** a constant 1.0 mA (load code 1) for the whole run. It never steps.
- **An opaque box over the board**, the same one for both runs, with the leads out under its edge.
  It keeps light and drafts the same.

**Cold run:**
- Heater off the board. It may stay wired to OUT2, which is held at 0 V.
- Ice in a sealed bag, with a dry paper towel between the bag and the board. Water on the board
  leaks current; the run adds a note if the supply current rises more than 20 % (condensation).
- Cover U1 and the TMP235 together.

**Hot run:**
- **Wiring:** PSU OUT2 + and − on the resistor's two leads, nothing else on OUT2, neither lead
  touching the board.
- **The resistor:** 1 Ω, 10 W, ceramic, on U1 and the TMP235 together, with a couple of layers of paper
  between it and the board. No ice.
- **The paper:** the 70 °C cutoff reads the TMP235, not the resistor's surface, which runs hotter than
  the chip. Watch it the first time.

## Getting good data

- **Ramp slowly: 1 °C/min or less.** The TMP235 sits beside U1, not inside it, so while the board heats
  it reads cooler than the die, and while it cools it reads warmer. Samples ramping faster than
  1.5 °C/min stay out of the curve.
- **Measure both directions over the same range.** Averaging heating and cooling at each temperature
  cancels most of that lag. The hot run does both by design.
- **Cover a wide range.** The bow is small: a few hundred µV over 35 °C. Ice (~5 °C) to 60 °C makes it
  much clearer than room to 60 °C.
- **Don't touch or move things during a run.** On 2026-11-21, VREF and VOUT dropped together by up to
  134 mV for a minute or two, twice, at moments that matched setting up the heater. Pressure, the
  heater supply switching on, or light are the candidates. Leave the setup alone once logging has
  started.
- **Settling isn't needed for the curve.** A slow continuous sweep gives many more temperatures than a
  few settled plateaus. Plateaus are still found and reported (T steady within 0.5 °C for 30 s or more).

## Reading the results

Each `temp` run folder (`results\temp\<stamp>_JAFWAK_temp[_INVALID]`) holds:
- `data.csv`: every sample, with t_s, temp_c, tmp_v, vref_v, vout_v, the currents, and the heater's
  target_c / heater_w in the hot run. It also has rate_c_per_min, direction, misread and in_curve.
- `curve.csv`: VREF per 0.5 °C bin, heating and cooling averaged.
- `bins.csv`: the same per direction (heating / cooling / steady), with each bin's spread.
- `summary.json`: the parabola fit (peak, curvature in ppm/°C², tempco at 25 °C, box tempco), the
  plateaus, the misreads and the notes.
- `plot.png`: T, VREF, VOUT (and heater power) against time, and VREF against T.

**TMP235 voltage to temperature:** T = (V − 0.500 V) / 0.010 V/°C, so 607 mV is 10.7 °C and 700 mV
is 20 °C. This is the datasheet's nominal formula, and the AD2 can read a few mV off: each mV is
0.1 °C, so absolute temperatures could be off by about ±1 °C. The shape of the curve is affected
much less.

**DMM misreads:** a sample where VREF jumps but VOUT doesn't is the meter's misread, not VREF's. It is
left out and listed. The run is marked `_INVALID` only if more than 1 % of samples are misreads. VREF
and VOUT moving together is the reference itself: it stays in, and a bin with more than 0.5 mV of
spread gets a note.

**The first run** (2026-11-21, heater by hand at 9 W) is in [bench-log.md](bench-log.md). With its two
VREF drops excluded, it gave a peak near 43 °C, −1.17 ppm/°C² and 16 ppm/°C box tempco over 21-63 °C.

## On the board so far

- **2026-11-21 evening:** the first cold and hot runs, both valid ([bench-log.md](bench-log.md)). The
  E3648A accepted OUT2's LOW range, 3.2 A limit and 3.5 V OVP; the heater measured 1.12 Ω; the PI loop
  stayed within 0.7 °C of its target with the gains tuned on the simulator, at up to 6.6 W.
- **Lesson:** the two runs didn't overlap (cold ended at 21.1 °C, hot started at 21.5 °C), so
  `temp-curve` couldn't check the join. Start the hot run straight after the cold run, while the board is
  still a degree or two below room temperature, and let it finish its down ramp (it was stopped at 44 °C).
