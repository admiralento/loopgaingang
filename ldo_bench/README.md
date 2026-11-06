# ldo-bench

Bench automation for characterizing the LDO on the JAFWAK board. It covers load regulation, line regulation, load transient and line transient.

Hardware:
- Agilent E3638A power supply over GPIB (USB adapter)
- Digilent Analog Discovery 2

## Setup

1. Install [uv](https://docs.astral.sh/uv/).
2. Run `uv sync` in this folder.

```
uv run ldo-bench --help
uv run ldo-bench check-config -c configs/jafwak.toml       # custom LDO (1.2 V)
uv run ldo-bench check-config -c configs/demo_board.toml   # TPS7A7001 demo board (1.0 V)
```

There is one config per board. Each holds the wiring, load-bank pins and resistors, voltage limits, and test conditions.

### macOS: `ModuleNotFoundError: No module named 'ldo_bench'`

If the repo is in an iCloud-synced folder (e.g. `~/Documents`), macOS marks files inside `.venv` as hidden, and Python 3.12+ skips hidden `.pth` files. Keep the virtualenv outside the synced folder:

```
export UV_PROJECT_ENVIRONMENT="$HOME/.venvs/ldo-bench"   # add to ~/.zshrc
```

Windows is not affected.

Work in progress: see the plan in the PR description.
