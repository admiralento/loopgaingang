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


@pytest.fixture
def bench(cfg):
    with open_bench(cfg, sim=True) as b:
        yield b


@pytest.fixture(autouse=True)
def _no_serial_delays(monkeypatch):
    """Fake serial ports don't need the 34401A's pacing."""
    from ldo_bench.instruments.transports import SerialTransport

    monkeypatch.setattr(SerialTransport, "CMD_GAP", 0.0)
