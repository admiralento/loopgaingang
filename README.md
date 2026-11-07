# loopgaingang

EE372B project: a custom LDO test board (JAFWAK), and the tools to characterize it.

| Folder | What's in it |
|---|---|
| `LDO_Stable_JAFWAK/` | KiCad project for our board. Sub-sheets: `ldo_harness`, `buffer` (BUF634 for line transient / PSRR), `load_cell` (on-board switched load), `rc_filter`, `temp_sense`. Gerbers in `Out/`, BOM in `bom/`, PDFs in `Schematic PDFs/` and `LayoutLayers/`. |
| `TPS7A7001_Evaluation_Board/` | KiCad project for the TPS7A7001 demo board (1.0 V out), used to bring up the bench before our board. |
| `Switched_R_Load_Bank/` | KiCad project for the separate 5-bit switched resistor load used with the demo board. |
| `AD_workspaces/` | WaveForms workspaces and the JavaScript settling-time script for manual measurements with the Analog Discovery. |
| `ldo_bench/` | Python bench automation: load / line regulation, load / line transient, PSRR, quiescent current and enable tests, driven from the command line. **Start with [`ldo_bench/README.md`](ldo_bench/README.md).** |

The automated tests and the WaveForms workspaces use the same settling analysis (`ldo_bench` ports `AD_workspaces/load_tran_script.js`), so they give the same numbers.
