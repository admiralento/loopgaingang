# AD Workspaces – LDO Demo Board

WaveForms workspaces for characterizing the demo board LDO with an Analog Discovery.

## Workspace index

| File | Test | Step | Section |
|---|---|---|---|
| `demo_board_load_tran_rising.dwf3work` | Load transient | 4 mA → 67 mA (load applied) | [Load transient](#load-transient) |
| `demo_board_load_tran_falling.dwf3work` | Load transient | 67 mA → 4 mA (load removed) | [Load transient](#load-transient) |
| _TBD_ | Line transient | _TBD_ | [Line transient](#line-transient) |
| _TBD_ | Line transient | _TBD_ | [Line transient](#line-transient) |

## Opening a workspace

1. Connect the Analog Discovery and wire it for the test (see the test's section below).
2. In WaveForms: **File → Open Workspace…** and pick the file.
3. Start any stimulus the test needs (e.g. **Patterns** → **Run**), then press **Run** in **Scope**.

Use the same device model and the same or newer WaveForms version as the one the files were saved with.

## Common wiring notes

- Always connect Scope **1−** and **2−** to GND. A floating negative input makes the trace very noisy.
- The Analog Discovery **GND** (⏚) must be connected to the demo board ground.

---

## Load transient

Measures the LDO output's response to a load step between **4 mA** and **67 mA**.

| File | Load step | Scope trigger | Script `EDGE` |
|---|---|---|---|
| `demo_board_load_tran_rising.dwf3work` | 4 mA → 67 mA (load applied) | Channel 2, **Rising** | `"rising"` |
| `demo_board_load_tran_falling.dwf3work` | 67 mA → 4 mA (load removed) | Channel 2, **Falling** | `"falling"` |

### Wiring

| Analog Discovery | Demo board | Purpose |
|---|---|---|
| Scope **1+** | `LDO_out` | LDO output (the signal being measured) |
| Scope **1−** | GND | Channel 1 reference |
| **DIO 0** | `J0` | |
| **DIO 4** | `J4` | Load select: toggles between 4 mA and 67 mA |
| Scope **2+** | `J4` / DIO 4 | Load-step reference for the trigger and scripts |
| Scope **2−** | GND | Channel 2 reference |
| **GND** (⏚) | GND | Common ground |

DIO 4 high = 67 mA, DIO 4 low = 4 mA.

### Measurements

Both workspaces include these custom measurements in the Scope **Measurements** panel. They update on every capture.

| Measurement | `OUTPUT` | Unit | Meaning |
|---|---|---|---|
| **Nominal** | `"nominal"` | V | Average `LDO_out` before the step |
| **Peak** | `"peak"` | V | Largest excursion after the step: the dip (rising) or overshoot (falling) |
| **Band Upper** | `"upper"` | V | Nominal + settling band |
| **Band Lower** | `"lower"` | V | Nominal − settling band |
| **Settling Time** | `"time"` | s | Time from the DIO 4 edge until `LDO_out` enters the band for the last time and stays inside |
| **Noise** | `"noise"` | V | RMS noise of the smoothed signal before the step. Use it to check the result can be trusted |

The **settling band** is ±10% of the peak deviation, i.e. ±0.1 × |Peak − Nominal| around Nominal. For example, a 20 mV dip gives a ±2 mV band.

Settling time counts ringing: if the signal leaves the band again after first entering it, the time is measured from the **last** time it comes back in.

Two more `OUTPUT` values are available for debugging: `"band"` (band half-width in V) and `"rate"` (the sample rate the script is using, in Hz, which should match the Scope header).

### How the script works

All measurements share one script. Two settings at the top of each one control what it shows:

- `OUTPUT`: which value this measurement displays (see the table above).
- `EDGE`: `"rising"` or `"falling"`. This must match the Scope trigger condition.

The script:
1. Finds the load step as the Channel 2 edge.
2. Averages Channel 1 before the step to get Nominal.
3. Blanks the switching glitch: ±0.5 µs around the edge is replaced with a straight line.
4. Smooths Channel 1 with a 1 µs moving average.
5. Finds the largest deviation from Nominal within 20 µs after the step (Peak).
6. Sets the band to ±10% of |Peak − Nominal|.
7. Reports the time after the last smoothed sample that falls outside the band.

The sample rate is read from the Scope settings, so the measurements stay correct if you change time/div.

Tunable settings in the script:

| Setting | Default | Change if… |
|---|---|---|
| `SETTLE_FRACTION` | `0.1` | You want a different band (0.1 = ±10% of peak deviation) |
| `SMOOTH_TIME` | `1e-6` (1 µs) | Noise is too high (increase it), but keep it well below the ringing period |
| `PEAK_WINDOW` | `20e-6` (20 µs) | The dip or overshoot peaks later than 20 µs after the step |
| `BLANK_TIME` | `0.5e-6` (0.5 µs) | The switching glitch at the edge is wider than ±0.5 µs |

If you edit a setting, change it in **every** custom measurement so they stay consistent.

### Getting a trustworthy result

- **Noise should be at most about ⅓ of the band** (Band Upper − Nominal). If it's higher, noise crosses the band edge and makes the settling time too long. To fix it:
  - Turn on Scope **averaging** (16 or more). The response repeats on every trigger, so averaging removes noise without blurring the response.
  - Increase `SMOOTH_TIME` or `SETTLE_FRACTION`.
- **Leave enough time after the step.** Move the trigger position left or increase time/div so the capture runs well past the point where the signal settles.

### Troubleshooting

- **All measurements show NaN:** Channel 2 isn't seeing the DIO 4 edge, or the trigger edge doesn't match `EDGE`. Check that Scope 2+ is wired to DIO 4 and that the trigger condition matches the file.
- **Settling Time is NaN:** the signal was still outside the band at the end of the capture. Increase time/div or move the trigger left. If that doesn't help, the noise is too high (see above).
- **Peak is huge or looks wrong:** the switching glitch is getting through. Increase `BLANK_TIME`.
- **Values look wrong:** compare against manual cursors. Put Y cursors at Band Upper and Band Lower and an X cursor at the Settling Time. After that X cursor, the trace should stay between the two Y cursors.

---

## Line transient

_TBD: measures the LDO output's response to a step in input voltage._

| File | Input step | Scope trigger | Script `EDGE` |
|---|---|---|---|
| _TBD_ | _TBD_ | _TBD_ | _TBD_ |

### Wiring

| Analog Discovery | Demo board | Purpose |
|---|---|---|
| _TBD_ | _TBD_ | _TBD_ |

### Measurements

_TBD. If the line transient workspaces reuse the load transient script, note any changes here (e.g. what Channel 2 is connected to, different `PEAK_WINDOW` or `BLANK_TIME`)._

### Troubleshooting

_TBD_
