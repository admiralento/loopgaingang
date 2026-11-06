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
def bench(cfg):
    with open_bench(cfg, sim=True) as b:
        yield b
