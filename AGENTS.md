# Standards for this repository

These rules apply to every task. The repo holds two kinds of work:

- **KiCad designs** (`LDO_Stable_JAFWAK/`, `TPS7A7001_Evaluation_Board/`, `Switched_R_Load_Bank/`) and WaveForms workspaces (`AD_workspaces/`).
- **`ldo_bench/`:** Python bench automation that drives real lab instruments.

Before creating or moving files, adding a dependency, or changing the build, read `ldo_bench/README.md` and `ldo_bench/docs/architecture.md`, and keep them true when you change the design.

## Boundaries (these override everything else)

- **Host installs.** Only these are installed on the host, and a person installs them: `uv`, the Digilent WaveForms runtime (the Analog Discovery 2 driver), and NI-VISA on Windows. Everything else (Python, packages, tools) lives in the project's uv environment (`ldo_bench/.venv`, declared in `pyproject.toml`). Containers aren't used: they can't reach the AD2 or the GPIB/serial USB adapters on macOS. If you believe another host install is unavoidable, stop, explain why, and wait for my explicit approval of that specific install. Expect the answer to be no.
- **Stay in the repo.** Every change on the host happens inside this repository. Nothing is written outside it, except uv's own cache and an agent's own memory or instruction file when I have agreed.
- **Run things through the tools.** Run project tools only through `uv run` (bench commands: `uv run ldo-bench …`; dev tasks: `uv run poe <task>`). Run `gh`, `curl`, `wget` and package managers other than uv only when a task needs them, never tucked inside a longer command or script. Anything run more than once (testing, linting, validation) belongs in the task runner (`[tool.poe.tasks]` in `ldo_bench/pyproject.toml`); if a task needs a new command, add a task and say so.
- **Design files.** Don't edit the KiCad projects, Gerbers, BOMs or schematic PDFs without asking; they're my teammates' design files. Information read from a schematic (pin numbers, jumper positions, resistor values) is a guess until a person confirms it.

## The bench (live hardware)

- **Live runs only when asked.** Any `ldo-bench` command without `--sim` drives real instruments (only `discover` and `check-config` don't). Run one only when I've asked for that specific run. Say first which board, which voltages, and the wiring it expects.
- **Never weaken a safety limit** without my approval: `dut.vin_max`, `dut.i_max`, `psu.v_max`, `psu.ovp`, `psu.current_limit`, `line_tran.w1_max`, or any cross-check in `config.py`.
- **Voltages reach the DUT only through the Bench methods** (`vin_from_psu`, `buffer_rail`, `vin_from_wavegen`, `vin_square_from_wavegen`, `vin_sine_from_wavegen`, `en_ain_sine`, `en_ain_dc`, `enable_logic`, `set_enable`, `loadbank.set`, `reference_on`, `split_rails_on`, `split_rails_off`). Every hardware script ends in the Bench shutdown, including on error and Ctrl-C. A script that holds a state (for probing) has a hard timeout.
- **Real data only.** No fallback values, assumed readings, or "fixed up" data. A setup problem is recorded as an issue (an `_INVALID` run, exit code 3), never hidden or worked around. Readings below an instrument's resolution are reported as bounds.
- **Results.** Never delete or overwrite anything in `results/` without asking. Simulated runs save nothing unless I ask for `--save-sim`.
- **After a bench session,** update `ldo_bench/docs/bench-log.md` (what ran, what was measured, what went wrong) and the status table in `ldo_bench/README.md`.

## Git

- **Branches.** Work on a feature branch (currently `ldo-bench`). Commit to it often, in small steps, each with a clear message. You may push it.
- **`main`** changes only through a pull request that I review and merge. Never commit to, merge into, or push to `main`.
- **Other merges.** Merging into any branch other than your feature branch needs my explicit approval, every time. Approval for one merge is not approval for the next.
- **History.** Never force-push, rewrite published history, or change git configuration.

## Before you start

For a new feature, a design change, or a new dependency touching more than one file, reply first with your plan and the files you will touch, in under 40 lines, followed by your assumptions and your questions. Wait for approval.

These don't need a plan first: documentation, tests, and bench debugging I've explicitly started (fixes while I'm at the bench). Say what you changed afterwards.

## Invariants (true after every change)

- **Docs:** the invariants in `ldo_bench/README.md` and `ldo_bench/docs/architecture.md` still hold. If a change would break one, stop and ask.
- **Dependencies** are declared and pinned in `pyproject.toml` / `uv.lock`, never fetched ad hoc while the tool runs.
- **Platforms:** code and tasks work on macOS and Windows. No POSIX-only shell in tasks; build paths with `pathlib`.
- **Not iCloud:** the repo and its `.venv` stay out of iCloud-synced folders (`~/Documents`, `~/Desktop`).

## Running anything

Through `uv run`, and nothing else. In `ldo_bench/`:
- `uv run poe` lists the tasks.
- `uv run poe verify` is the gate: format check, lint, both configs validated, and the test suite on the simulator. It needs no hardware.

## Quality

- **Practice:** follow current best practice for Python, and say in the plan where you depart from it and why.
- **Readability:** code a stranger can read, with types and docstrings (with units) on anything public, in the idiom of the language. Match the existing style of the codebase.
- **Boundaries and errors:** every external input is validated at the boundary (configs by the pydantic models, instrument replies by the drivers), and errors are handled, never swallowed. No `eval`, no shell built from user input.
- **Dependencies:** add one only when it earns its place, and say why.

## Done means

- **The gate:** `uv run poe verify` is green from a fresh clone.
- **Tests:** each behaviour you add or change has a test that fails when it is wrong, on the simulator or a fake instrument link; show it failing once before you make it pass.
- **Docs:** the README and `docs/` are enough for a teammate who wasn't there, and `docs/architecture.md` still describes the code.
- **Say what you did not verify,** and in particular what has not been run on real hardware. A simulator test passing doesn't mean the instrument works.
