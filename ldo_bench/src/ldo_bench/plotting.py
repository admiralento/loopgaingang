"""Shared plot styling (matplotlib, headless).

Colors come from the reference chart palette: categorical slot 1 for the
measured series, secondary ink for reference marks (fits, limits), recessive
gridlines and axes.

Importing this module selects the headless Agg backend and applies STYLE globally,
so procedures just call `figure()` and the run folder saves the result.
"""

from __future__ import annotations

import matplotlib

matplotlib.use("Agg")  # no GUI needed; works on headless and Windows machines

import matplotlib.pyplot as plt

SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_2 = "#52514e"
MUTED = "#898781"
GRID = "#e1e0d9"
AXIS = "#c3c2b7"
CRITICAL = "#d03b3b"  # status red: setup problems only, never a data series
SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100"]  # fixed order, never cycled

STYLE = {
    "figure.facecolor": SURFACE,
    "axes.facecolor": SURFACE,
    "savefig.facecolor": SURFACE,
    "axes.edgecolor": AXIS,
    "axes.labelcolor": INK_2,
    "axes.titlecolor": INK,
    "axes.titlesize": 11,
    "axes.titleweight": "bold",
    "axes.titlelocation": "left",
    "axes.labelsize": 9,
    "axes.grid": True,
    "axes.spines.top": False,
    "axes.spines.right": False,
    "grid.color": GRID,
    "grid.linewidth": 0.6,
    "xtick.color": MUTED,
    "ytick.color": MUTED,
    "xtick.labelcolor": INK_2,
    "ytick.labelcolor": INK_2,
    "xtick.labelsize": 8,
    "ytick.labelsize": 8,
    "legend.frameon": False,
    "legend.fontsize": 8,
    "legend.labelcolor": INK_2,
    "lines.linewidth": 2,
    "font.family": "sans-serif",
}


plt.rcParams.update(STYLE)  # applies at plot and save time too, unlike rc_context


def figure(nrows: int = 1, ncols: int = 1, *, size=(7.0, 4.2), **kw):
    """(fig, axes) like plt.subplots, with constrained layout; `size` in inches."""
    return plt.subplots(nrows, ncols, figsize=size, layout="constrained", **kw)


def subtitle(ax, text: str) -> None:
    """Secondary line under the left-aligned title."""
    ax.text(0, 1.02, text, transform=ax.transAxes, fontsize=8.5, color=INK_2, va="bottom")


def close(fig) -> None:
    """Free a figure's memory once it's saved."""
    plt.close(fig)


def mark_simulated(fig) -> None:
    """Banner across a figure made from simulated data, so it can't pass for a measurement."""
    fig.text(
        0.5,
        0.5,
        "SIMULATED: NOT MEASURED DATA",
        ha="center",
        va="center",
        rotation=20,
        fontsize=30,
        fontweight="bold",
        color=CRITICAL,
        alpha=0.25,
        zorder=100,
    )


def issue_box(ax, issues: list[str], *, width: int = 58) -> None:
    """Red-bordered list of setup problems in the top-left of an axes."""
    import textwrap

    lines = ["SETUP PROBLEM, not a valid measurement:"]
    for msg in issues:
        wrapped = textwrap.wrap(msg, width)
        lines += [f"- {wrapped[0]}", *(f"  {w}" for w in wrapped[1:])]
    ax.text(
        0.02,
        0.98,
        "\n".join(lines),
        transform=ax.transAxes,
        va="top",
        ha="left",
        fontsize=7.5,
        color=CRITICAL,
        family="monospace",
        zorder=10,
        bbox={"boxstyle": "round,pad=0.4", "fc": SURFACE, "ec": CRITICAL, "lw": 1.2, "alpha": 0.95},
    )


def suptitle(fig, title: str, *, invalid: bool) -> None:
    """The figure title, left-aligned; an invalid run's gets a red SETUP PROBLEM line above it."""
    if invalid:
        title = "SETUP PROBLEM, not a valid measurement\n" + title
    fig.suptitle(title, x=0.01, ha="left", fontsize=11, fontweight="bold", color=CRITICAL if invalid else INK)
