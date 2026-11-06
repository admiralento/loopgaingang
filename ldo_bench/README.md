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
uv run ldo-bench check-config -c configs/jafwak.toml
```

Work in progress: see the plan in the PR description.
