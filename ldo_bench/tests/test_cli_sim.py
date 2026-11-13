"""Every command, end to end on the simulator, on each board: what `uv run poe sim-all` runs.

The list comes from the CLI itself, so a new command without a simulator run fails the gate. A
command whose test section a board's config doesn't have must refuse cleanly (exit 2); every
command must run on at least one board, saving nothing (no --save-sim).
"""

import pytest
from helpers import config_path, invoke

from ldo_bench.cli import app

NOT_RUNS = {"check-config", "discover", "compare"}  # these don't run a test (compare is in test_vref)
COMMANDS = sorted((c.name or c.callback.__name__.replace("_", "-")) for c in app.registered_commands)
TESTS = [c for c in COMMANDS if c not in NOT_RUNS]
BOARDS = ["jafwak", "demo_board"]


def _run(cmd, board, tmp_path):
    out = [] if cmd == "smoke" else ["-o", str(tmp_path)]  # smoke saves nothing anyway
    return invoke(cmd, "-c", config_path(board), "--sim", "--yes", *out)


@pytest.mark.parametrize("cmd", TESTS)
def test_runs_on_the_simulator(cmd, tmp_path):
    ran = []
    for board in BOARDS:
        r = _run(cmd, board, tmp_path / board)
        if r.exit_code == 2 and "section" in r.output:
            continue  # this board's config has no section for the test
        assert r.exit_code == 0, f"{cmd} on {board}:\n{r.output}"
        assert "SIMULATED" in r.output
        ran.append(board)
    assert ran, f"{cmd} runs on neither board"
    assert not any(tmp_path.rglob("*.csv")), "a simulated run saved data without --save-sim"


def test_every_command_is_listed():
    assert {"load-reg", "line-reg", "load-tran", "line-tran", "psrr", "iq", "temp", "buffer-check"} <= set(
        TESTS
    )
