# Line transient, JAFWAK, 2026-11-18

All runs: VIN 1.75 <-> 1.85 V through U2 (split supply, VS_BUFF 5 V, -5 V), load code 22 (20 mA),
AD2 CH1 = VOUT (J13 pin 4), CH2 = VIN (J13 pin 1), 20 ms capture (8192 samples at 0.41 MS/s).

| Folder | Input on W1 | Captures | What it shows |
|---|---|---|---|
| `raw_square-20Hz_20mA_1capture/` | 20 Hz square | 1 (no averaging) | **Raw data.** `plot_raw.png` = measured samples only, no smoothing |
| `averaged_square-20Hz_20mA_16avg/` | 20 Hz square | 16 averaged | Same edge, noise ~0.06 mV rms instead of ~0.25 mV |
| `averaged_DC-steps_20mA_16avg/` | DC level stepped by the script | 16 averaged | VIN edge slow (tau ~2.3 ms): the AD2's DC mode, not the board |

Each folder: `waveform_rising.csv` / `waveform_falling.csv` (`t_s`, `vin_v`, `vout_v` = what the scope
read, averaged over the captures if more than one; `vout_smoothed_v` = analysis copy only), `data.csv`,
`summary.json`, `meta.json`, plots.

**Result (20 Hz square):** VIN switches in ~0.2 ms; VOUT moves ~4.75 mV for the 100 mV step
(~48 mV/V) and settles in ~2 ms, no overshoot or ringing. The reports' "did not settle" is because
settling is judged against the pre-step level, which VOUT never returns to (it's line regulation).

Command (from `ldo_bench/`):
`uv run ldo-bench line-tran -c configs/jafwak.toml --no-xscope --window-ms 20 --square-hz 20 [--raw | --avg 16]`

Not verified: `--raw` itself has run only on the simulator (the bench was unplugged); the raw folder is
a `--avg 1` run, whose plot_raw.png was redrawn from its CSV with only the measured samples.
