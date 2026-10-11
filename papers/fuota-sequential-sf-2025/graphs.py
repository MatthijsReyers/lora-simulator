#!/usr/bin/env python3
"""
Renders the figures and tables of the paper from the runs in `data/` into `graphs/`:

- Figure 2: energy consumption and delivery time against the distance from the gateway for
  every scheme, with the analytical curve and the values read off the paper's plot.
- Figure 2, proposed scheme only: the paper's curve next to the simulation, the two metrics
  side by side (`fig2_proposed.png`).
- Figure 3: the averages over node distances against w for L = 7..11.
- Tables I, II and III as Markdown next to the paper's numbers (printed, and written to
  `graphs/tables.md`).

    pipenv run python papers/fuota-sequential-sf-2025/graphs.py
"""
from __future__ import annotations

import argparse
import math
import os
import sys
from pathlib import Path

sys.path.append(".")
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

import analytical
import paper_values as paper
from colors import BLUE_DARK, RED
from scenario import Scenario

SCHEMES = {
    "proposed_L7M12w300": ("Proposed", "tab:blue", "-"),
    "fsf10": ("FSF-10", "tab:orange", "--"),
    "fsf11": ("FSF-11", "tab:purple", "-"),
    "fsf12": ("FSF-12", "tab:green", "-."),
    "gb_e": ("GB-E", "tab:cyan", "-"),
    "gb_l": ("GB-L", "tab:red", "-."),
}
PAPER_KEYS = {"proposed_L7M12w300": "proposed", "fsf10": "fsf10", "fsf11": "fsf11", "fsf12": "fsf12", "gb_e": "gb_e", "gb_l": "gb_l"}


def load(data_dir: Path, kind: str) -> pd.DataFrame:
    files = sorted(data_dir.glob(f"*_{kind}.csv"))
    if not files:
        raise SystemExit(f"No *_{kind}.csv files in {data_dir}, run run.sh first")
    frames = [pd.read_csv(f) for f in files]
    return pd.concat(frames, ignore_index=True)


def scheme_of(run: str) -> str:
    return run.split("_n")[0]


def select(nodes: pd.DataFrame, summaries: pd.DataFrame, scheme: str, **params) -> pd.DataFrame:
    """ The per node rows of the runs of one scheme with the given scenario parameters. """
    runs = summaries[summaries["run"].map(scheme_of) == scheme]
    for (key, value) in params.items():
        runs = runs[np.isclose(runs[key], value)]
    return nodes[nodes["run"].isin(runs["run"])]


def by_distance(rows: pd.DataFrame, bins: int = 20) -> pd.DataFrame:
    """ Means per distance bin (in km), delivery time in hours, only decoded nodes count. """
    rows = rows[rows["decoded"]].copy()
    rows["km"] = rows["distance"] / 1000
    rows["bin"] = np.minimum((rows["km"] * bins).astype(int), bins - 1)
    grouped = rows.groupby("bin").agg(km=("km", "mean"), ec=("ec", "mean"), dt=("delivery_time", "mean"), n=("ec", "size"))
    grouped["dt"] /= 3600
    return grouped


def figure2(nodes: pd.DataFrame, summaries: pd.DataFrame, out: Path, density: float) -> None:
    fig, (ax_ec, ax_dt) = plt.subplots(2, 1, figsize=(7, 9))
    for (scheme, (label, colour, style)) in SCHEMES.items():
        rows = select(nodes, summaries, scheme, interferer_density=density)
        if rows.empty:
            continue
        binned = by_distance(rows)
        ax_ec.plot(binned["km"], binned["ec"], style, color=colour, label=label)
        ax_dt.plot(binned["km"], binned["dt"], style, color=colour, label=label)
        key = PAPER_KEYS[scheme]
        ax_ec.plot(paper.FIG2_DISTANCES, [v if v is not None else np.nan for v in paper.FIG2_EC[key]], ":", color=colour, alpha=0.6)
        ax_dt.plot(paper.FIG2_DISTANCES, paper.FIG2_DT[key], ":", color=colour, alpha=0.6)
    # Analytical curve of the proposed scheme.
    prop = select(nodes, summaries, "proposed_L7M12w300", interferer_density=density)
    if "analytical_ec" in prop and prop["analytical_ec"].notna().any():
        an = prop.dropna(subset=["analytical_ec"]).groupby("distance").first().reset_index()
        ax_ec.plot(an["distance"] / 1000, an["analytical_ec"], "s", color="tab:red", mfc="none", ms=4, label="Proposed (analytical)")
        ax_dt.plot(an["distance"] / 1000, an["analytical_delivery_time"] / 3600, "s", color="tab:red", mfc="none", ms=4, label="Proposed (analytical)")
    ax_ec.set_ylim(0, 60)
    ax_dt.set_ylim(0, 150)
    ax_ec.set_ylabel("Energy consumption (normalised)")
    ax_dt.set_ylabel("Delivery time (hours)")
    for ax in (ax_ec, ax_dt):
        ax.set_xlabel("Distance from gateway (km)")
        ax.set_xlim(0, 1)
        ax.grid(alpha=0.3)
    ax_ec.legend(fontsize=8, title="solid: this reproduction, dotted: paper", title_fontsize=8)
    fig.suptitle(f"Figure 2 (λ_I = {density:g} /m²)")
    fig.tight_layout()
    fig.savefig(out / "fig2.png", dpi=150)
    plt.close(fig)


def figure2_proposed(nodes: pd.DataFrame, summaries: pd.DataFrame, out: Path, density: float) -> None:
    """ Figure 2 reduced to the proposed scheme: the paper's curve next to the simulation, EC and DT side by side. """
    rows = select(nodes, summaries, "proposed_L7M12w300", interferer_density=density)
    if rows.empty:
        return
    binned = by_distance(rows)
    fig, (ax_ec, ax_dt) = plt.subplots(1, 2, figsize=(11, 4.2))
    # Same look as the MAC examples (examples/aloha and friends): the reference as a thin
    # dashed line, the simulation as star markers, both from the repository's palette.
    ax_ec.plot(paper.FIG2_DISTANCES, paper.FIG2_EC["proposed"], linestyle="--", linewidth=1, color=BLUE_DARK, label="Borkotoky (2025)", zorder=3)
    ax_ec.scatter(binned["km"], binned["ec"], color=RED, marker="*", label="Simulated", zorder=2)
    ax_dt.plot(paper.FIG2_DISTANCES, paper.FIG2_DT["proposed"], linestyle="--", linewidth=1, color=BLUE_DARK, label="Borkotoky (2025)", zorder=3)
    ax_dt.scatter(binned["km"], binned["dt"], color=RED, marker="*", label="Simulated", zorder=2)
    ax_ec.set_ylabel("Energy consumption (normalised)")
    ax_dt.set_ylabel("Delivery time (hours)")
    ax_ec.set_ylim(0, 45)
    ax_dt.set_ylim(0, 60)
    for ax in (ax_ec, ax_dt):
        ax.grid(zorder=0)
        ax.set_xlabel("Distance from gateway (km)")
        ax.set_xlim(0, 1)
        ax.set_xticks(np.arange(0, 1.1, 0.2))
        ax.legend()
    # No title: the figure gets its caption in the document it is included in.
    fig.tight_layout()
    fig.savefig(out / "fig2_proposed.png", dpi=150, bbox_inches="tight")
    plt.close(fig)


def figure3(nodes: pd.DataFrame, summaries: pd.DataFrame, out: Path) -> None:
    runs = summaries[summaries["scheme"] == "proposed"].copy()
    runs = runs[np.isclose(runs["interferer_density"], Scenario().interferer_density)]
    if runs.empty:
        return
    fig, (ax_ec, ax_dt) = plt.subplots(2, 1, figsize=(7, 9))
    for (L, colour) in zip(range(7, 12), ("tab:blue", "tab:red", "tab:orange", "tab:purple", "tab:green")):
        sub = runs[(runs["first_sf"] == L) & (runs["last_sf"] == 12)]
        if sub["transmissions_per_sf"].nunique() < 2:
            continue
        agg = sub.groupby("transmissions_per_sf").agg(ec=("mean_ec_without_control", "mean"), dt=("mean_delivery_hours", "mean")).reset_index()
        ax_ec.plot(agg["transmissions_per_sf"], agg["ec"], "-o", ms=3, color=colour, label=f"L = {L}")
        ax_dt.plot(agg["transmissions_per_sf"], agg["dt"], "-o", ms=3, color=colour, label=f"L = {L}")
        ax_ec.plot(paper.FIG3_W, paper.FIG3_EC[L], ":", color=colour, alpha=0.6)
        ax_dt.plot(paper.FIG3_W, paper.FIG3_DT[L], ":", color=colour, alpha=0.6)
    if not ax_ec.lines:
        plt.close(fig)
        return
    ax_ec.set_ylabel("Average energy consumption (norm., without E_C)")
    ax_dt.set_ylabel("Average delivery time (hours)")
    ax_ec.set_ylim(10, 26)
    ax_dt.set_ylim(14, 22)
    for ax in (ax_ec, ax_dt):
        ax.set_xlabel("w")
        ax.grid(alpha=0.3)
    ax_ec.legend(fontsize=8, title="solid: this reproduction, dotted: paper", title_fontsize=8)
    fig.suptitle("Figure 3 (M = 12)")
    fig.tight_layout()
    fig.savefig(out / "fig3.png", dpi=150)
    plt.close(fig)


def battery_lifetime_years(rx_hours_per_update: float, uplink_sf: int, scenario: Scenario) -> float:
    """ Equation (21) with the assumptions of Section VII. """
    uplink_airtime = scenario.airtime(uplink_sf, paper.UPLINK_PAYLOAD)
    tx_hours = 365 * (24 / paper.UPLINK_INTERVAL_H) * uplink_airtime / 3600
    rx_hours = 12 * paper.UPDATES_PER_MONTH * rx_hours_per_update
    sleep_hours = 365 * 24 - tx_hours - rx_hours
    return paper.BATTERY_CAPACITY_MAH / (paper.CURRENT_TX_MA * tx_hours + paper.CURRENT_RX_MA * rx_hours + paper.CURRENT_SLEEP_MA * sleep_hours)


def nearest(rows: pd.DataFrame, distance: float) -> pd.DataFrame:
    """ The rows of the node(s) closest to `distance` in every run. """
    picked = []
    for (_, run_rows) in rows.groupby("run"):
        idx = (run_rows["distance"] - distance).abs().idxmin()
        picked.append(run_rows.loc[idx])
    return pd.DataFrame(picked)


def tables(nodes: pd.DataFrame, summaries: pd.DataFrame, out: Path) -> str:
    scenario = Scenario()
    density = scenario.interferer_density
    lines = []

    lines.append("### Table I: battery lifetime (years) and delivery time (hours)\n")
    lines.append("| | Prop. edge | FSF-11 edge | GB-E edge | Prop. R/4 | FSF-11 R/4 | GB-E R/4 |")
    lines.append("| :-- | --: | --: | --: | --: | --: | --: |")
    ours_lt, ours_dt, paper_lt, paper_dt = [], [], [], []
    for (where, distance) in (("edge", scenario.radius), ("quarter", scenario.radius / 4)):
        for (scheme, key) in (("proposed_L7M12w300", "proposed"), ("fsf11", "fsf11"), ("gb_e", "gb_e")):
            rows = nearest(select(nodes, summaries, scheme, interferer_density=density), distance)
            rows = rows[rows["decoded"]] if not rows.empty else rows
            if rows.empty:
                ours_lt.append("-"); ours_dt.append("-")
            else:
                rx_hours = rows["rx_time"].mean() / 3600 + scenario.control_airtime / 3600
                lt = battery_lifetime_years(rx_hours, 12 if where == "edge" else 7, scenario)
                ours_lt.append(f"{lt:.2f}"); ours_dt.append(f"{rows['delivery_time'].mean() / 3600:.1f}")
            (plt_, pdt) = paper.TABLE_I[where][key]
            paper_lt.append(f"{plt_:.2f}"); paper_dt.append(f"{pdt:.1f}")
    lines.append("| LT, this reproduction | " + " | ".join(ours_lt) + " |")
    lines.append("| LT, paper | " + " | ".join(paper_lt) + " |")
    lines.append("| DT, this reproduction | " + " | ".join(ours_dt) + " |")
    lines.append("| DT, paper | " + " | ".join(paper_dt) + " |")

    lines.append("\n### Table II: EC (normalised, without E_C) and DT (hours) averaged over node distances\n")
    lines.append("| | " + " | ".join(SCHEMES[s][0] for s in SCHEMES) + " |")
    lines.append("| :-- | " + " | ".join("--:" for _ in SCHEMES) + " |")
    row_ec, row_dt, p_ec, p_dt, row_n = [], [], [], [], []
    for scheme in SCHEMES:
        rows = select(nodes, summaries, scheme, interferer_density=density)
        rows = rows[rows["decoded"]]
        if rows.empty:
            row_ec.append("-"); row_dt.append("-"); row_n.append("0")
        else:
            row_ec.append(f"{rows['ec_without_control'].mean():.1f}")
            row_dt.append(f"{rows['delivery_time'].mean() / 3600:.1f}")
            row_n.append(str(rows["run"].nunique()))
        (pe, pd_) = paper.TABLE_II[PAPER_KEYS[scheme]]
        p_ec.append(f"{pe:.1f}"); p_dt.append(f"{pd_:.1f}")
    lines.append("| EC, this reproduction | " + " | ".join(row_ec) + " |")
    lines.append("| EC, paper | " + " | ".join(p_ec) + " |")
    lines.append("| DT, this reproduction | " + " | ".join(row_dt) + " |")
    lines.append("| DT, paper | " + " | ".join(p_dt) + " |")
    lines.append("| runs | " + " | ".join(row_n) + " |")

    lines.append("\n### Table III: other interferer densities (EC without E_C / DT in hours)\n")
    lines.append("| λ_I | Prop. | FSF-11 | GB-L | Prop. paper | FSF-11 paper | GB-L paper |")
    lines.append("| :-- | --: | --: | --: | --: | --: | --: |")
    for (dens, values) in paper.TABLE_III.items():
        cells = []
        for scheme in ("proposed_L7M12w300", "fsf11", "gb_l"):
            rows = select(nodes, summaries, scheme, interferer_density=dens)
            rows = rows[rows["decoded"]]
            cells.append("-" if rows.empty else f"{rows['ec_without_control'].mean():.2f} / {rows['delivery_time'].mean() / 3600:.2f}")
        for key in ("proposed", "fsf11", "gb_l"):
            cells.append(f"{values[key][0]:.2f} / {values[key][1]:.2f}")
        lines.append(f"| {dens:g} | " + " | ".join(cells) + " |")

    text = "\n".join(lines) + "\n"
    (out / "tables.md").write_text(text)
    return text


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--data-dir", type=Path, default=Path(__file__).parent / "data")
    parser.add_argument("--out-dir", type=Path, default=Path(__file__).parent / "graphs")
    args = parser.parse_args()
    args.out_dir.mkdir(parents=True, exist_ok=True)
    nodes = load(args.data_dir, "nodes")
    summaries = load(args.data_dir, "summary")
    figure2(nodes, summaries, args.out_dir, Scenario().interferer_density)
    figure2_proposed(nodes, summaries, args.out_dir, Scenario().interferer_density)
    figure3(nodes, summaries, args.out_dir)
    print(tables(nodes, summaries, args.out_dir))
    print(f"figures written to {args.out_dir}")


if __name__ == "__main__":
    main()
