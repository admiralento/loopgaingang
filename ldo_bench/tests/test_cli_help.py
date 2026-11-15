"""--help text: option help that names a config section ([vref], [enable], ...) shows it.

Typer renders help through Rich, which treats a bare "[name]" as markup and drops it, leaving
"()" or "(: ..." behind.
"""

import re

import pytest
from typer.testing import CliRunner

from ldo_bench.cli import app

WIDE = {"COLUMNS": "250", "TERM": "dumb"}


def help_text(command: str) -> str:
    r = CliRunner().invoke(app, [command, "--help"], env=WIDE, terminal_width=250)
    assert r.exit_code == 0, r.output
    return re.sub(r"\s+", " ", r.output)


@pytest.mark.parametrize(
    "command, section",
    [
        ("load-tran", "[xscope]"),
        ("load-reg", "[vref]"),
        ("load-reg", "[enable]"),
        ("line-reg", "[split_rails]"),
    ],
)
def test_section_names_survive_in_help(command, section):
    assert section in help_text(command)


@pytest.mark.parametrize("command", [c.name for c in app.registered_commands if c.name])
def test_no_markup_holes_in_help(command):
    text = help_text(command)
    assert "()" not in text and "(:" not in text and "through ;" not in text, text
