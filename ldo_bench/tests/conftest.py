from pathlib import Path

import pytest

from ldo_bench.bench import open_bench
from ldo_bench.config import load_config

CONFIG_DIR = Path(__file__).parents[1] / "configs"


@pytest.fixture(params=["jafwak", "demo_board"])
def cfg(request):
    return load_config(CONFIG_DIR / f"{request.param}.toml")


@pytest.fixture
def jafwak():
    return load_config(CONFIG_DIR / "jafwak.toml")


@pytest.fixture
def jafwak_single(jafwak):
    """JAFWAK without [buffer]: the single-rail buffer path (U2 before its rework, and the demo board's)."""
    return jafwak.model_copy(update={"buffer": None})


@pytest.fixture(params=["split", "single"])
def buffer_board(request, jafwak, jafwak_single):
    """JAFWAK with each buffer path: the split-supply U2 ([buffer]) and the single-rail one."""
    return jafwak if request.param == "split" else jafwak_single


@pytest.fixture
def bench(cfg):
    with open_bench(cfg, sim=True) as b:
        yield b


@pytest.fixture(autouse=True)
def _no_real_buses(monkeypatch):
    """The suite never touches hardware: any real VISA / RS-232 / Prologix / AD2 search fails the test.

    Tests that need a fake bench patch these functions themselves (e.g. test_any_interface's `bus`).
    """
    from ldo_bench.instruments import discovery, transports

    def refuse(*_a, **_k):
        raise AssertionError("a test tried to reach real instruments; patch the opener or use the simulator")

    for name in ("_visa_candidates", "_serial_ports", "_probe_serial", "_prologix_candidates", "_ad2_lines"):
        monkeypatch.setattr(discovery, name, refuse)
    monkeypatch.setattr(transports, "visa_resource_manager", refuse)


@pytest.fixture(autouse=True)
def _no_serial_delays(monkeypatch):
    """Fake serial ports don't need the 34401A's pacing."""
    from ldo_bench.instruments.transports import SerialTransport

    monkeypatch.setattr(SerialTransport, "CMD_GAP", 0.0)
