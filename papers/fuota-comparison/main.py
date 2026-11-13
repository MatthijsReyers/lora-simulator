"""
    Runs every FUOTA protocol in the repository on one and the same deployment and compares what
    an update costs a device.

        pipenv run python papers/fuota-comparison/main.py --nodes 10 --radius 400 --firmware-kb 100

    One layout file is written and handed to every protocol, each protocol runs in a process of
    its own (a process can only hold one simulation), and the cost of the update is worked out
    for every device with the same energy model (`simulator/lora/radio_usage.py`). Results go to
    `data/<run name>/`. Run from the repository root; see README.md.
"""
import argparse
import math
import os
import random
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

sys.path.append(".")  # the simulator package, when running from the repository root

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

from simulator.layout import uniform_disc, write_layout

ROOT = Path(os.path.dirname(os.path.abspath(__file__))).parent.parent
HERE = Path(os.path.dirname(os.path.abspath(__file__)))

SURFACE = "#fcfcfb"
INK = "#1f1f1e"
MUTED = "#6b6a65"
GRID = "#e4e3de"
SERIES = "#2a78d6"

FREE_SPACE_LOSS_1M = 31.22  # dB at 868 MHz, the reference loss of the log-distance model


@dataclass
class Variant:
    """ One protocol configuration: how to run it and where it leaves its row per device. """
    name: str
    label: str
    script: str
    arguments: list[str]
    nodes_csv: str  # glob pattern inside the variant's output directory
    completed_column: str  # seconds from the start of the campaign to the device's update


def variants(args: argparse.Namespace, layout: Path, out: Path) -> list[Variant]:
    firmware = int(args.firmware_kb * 1000)
    duty_percent = str(args.duty_cycle)
    duty_fraction = str(args.duty_cycle / 100)
    exponent = str(args.exponent)

    def ews(name: str, label: str, burst: int | None) -> Variant:
        directory = out / name
        burst_arguments = ["--burst-packets", str(burst)] if burst else []
        return Variant(
            name, label, "examples/ews_fuota/main.py",
            [
                "--layout-file", str(layout), "--exponent", exponent,
                "--image-bytes", str(firmware), "--hours", str(args.nodes + 2),
                "--seed", str(args.seed), "--out", str(directory),
            ] + burst_arguments,
            "sensors.csv", "completed_at_s",
        )

    def lorawan(name: str, label: str, extra: list[str]) -> Variant:
        directory = out / name
        return Variant(
            name, label, "examples/lorawan_fuota/main.py",
            [
                "--layout-file", str(layout), "--exponent", exponent,
                "--image-size", str(firmware), "--duty-cycle", duty_fraction,
                "--uplink-interval", str(args.uplink_interval), "--seed", str(args.seed),
                "--nodes-csv", str(directory / "nodes.csv"), "--log-level", "WARNING",
            ] + extra,
            "nodes.csv", "cmp_time_s",
        )

    def malumbres(name: str, label: str, method: str) -> Variant:
        return Variant(
            name, label, "papers/fuota-unicast-broadcast-2024/main.py",
            [
                "--method", method, "--layout-file", str(layout), "--exponent", exponent,
                "--no-fading", "--per-device-duty-cycle", "--duty-cycle", duty_percent,
                "--firmware-kb", str(args.firmware_kb), "--seed", str(args.seed),
                "--output-dir", str(out / name),
            ],
            "*_nodes.csv", "cmp_time_s",
        )

    # The rateless code needs a few fragments more than the image has: one pass over the image
    # at the first spreading factor with a tenth to spare.
    fragment_size = 250
    assert firmware % fragment_size == 0, "The sequential SF scheme needs whole 250 B fragments"
    per_sf = math.ceil(1.1 * firmware / fragment_size)
    def sequential(name: str, label: str, extra: list[str]) -> Variant:
        return Variant(name, label, "papers/fuota-sequential-sf-2025/main.py", [
            *sequential_arguments, "--output-dir", str(out / name), *extra,
        ], "*_nodes.csv", "cmp_time_s")

    sequential_arguments = [
            "--scheme", "proposed", "-L", "7", "-M", "12", "-w", str(per_sf),
            "--layout-file", str(layout), "--firmware-kb", str(args.firmware_kb),
            "--fragment-size", str(fragment_size), "--exponent", exponent,
            "--reference-loss", str(FREE_SPACE_LOSS_1M), "--no-fading",
            "--interference", "none", "--duty-cycle", duty_percent, "--seed", str(args.seed),
    ]

    return [
        ews("ews_burst2", "EWS, 2 fragments per burst", None),
        ews("ews_burst12", "EWS, 12 fragments per burst", 12),
        lorawan("lorawan_class_b", "LoRaWAN FUOTA, Class B", [
            "--class-b", "--class-b-periodicity", str(args.class_b_periodicity),
        ]),
        lorawan("lorawan_class_c", "LoRaWAN FUOTA, Class C", []),
        sequential("sequential_sf", "Sequential SF multicast (Borkotoky)", []),
        sequential("sequential_sf_always_on", "Sequential SF, receiver always on",
                   ["--listen-always"]),
        malumbres("broadcast_unicast", "Broadcast + unicast (Malumbres)", "broadcast_unicast"),
        malumbres("broadcast_only", "Broadcast only (Malumbres)", "broadcast_only"),
    ]


def run(variant: Variant, out: Path) -> pd.DataFrame:
    directory = out / variant.name
    directory.mkdir(parents=True, exist_ok=True)
    started = time.time()
    with open(directory / "output.log", "w") as log:
        process = subprocess.run(
            [sys.executable, variant.script] + variant.arguments, cwd=ROOT,
            stdout=log, stderr=subprocess.STDOUT,
        )
    files = sorted(directory.glob(variant.nodes_csv))
    # The LoRaWAN example exits with 1 when its campaign did not complete, which is a result.
    assert files, f"{variant.name} left no results (exit code {process.returncode}), see {directory}"
    nodes = pd.read_csv(files[0])
    columns = [column for column in nodes.columns if column.startswith("cmp_")]
    result = nodes[["distance"] + columns].copy()
    result.insert(0, "protocol", variant.name)
    result.insert(1, "label", variant.label)
    result["completed_at_s"] = nodes[variant.completed_column].where(nodes["cmp_updated"])
    print(f"  {variant.label}: {time.time() - started:.0f} s")
    return result


def summarise(nodes: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for ((protocol, label), group) in nodes.groupby(["protocol", "label"], sort=False):
        rows.append({
            "protocol": protocol,
            "label": label,
            "devices": len(group),
            "updated": int(group["cmp_updated"].sum()),
            "energy_mean_j": group["cmp_total_j"].mean(),
            "energy_min_j": group["cmp_total_j"].min(),
            "energy_worst_j": group["cmp_total_j"].max(),
            "tx_j": group["cmp_tx_j"].mean(),
            "rx_j": group["cmp_rx_j"].mean(),
            "mcu_j": group["cmp_mcu_j"].mean(),
            "sleep_j": group["cmp_sleep_j"].mean(),
            "tx_s": group["cmp_tx_s"].mean(),
            "rx_s": group["cmp_rx_s"].mean(),
            "device_time_h": group["cmp_time_s"].mean() / 3600,
            "fleet_time_h": group["completed_at_s"].max() / 3600,
        })
    return pd.DataFrame(rows)


def number(value: float) -> str:
    return f"{value:.0f}" if value >= 100 else f"{value:.1f}" if value >= 10 else f"{value:.2f}"


def plot(summary: pd.DataFrame, path: Path, subtitle: str) -> None:
    summary = summary.sort_values("energy_mean_j", ascending=False).reset_index(drop=True)
    rows = range(len(summary))
    (figure, (energy, duration)) = plt.subplots(
        1, 2, figsize=(11, 1.6 + 0.5 * len(summary)), facecolor=SURFACE, sharey=True,
        gridspec_kw={"width_ratios": [3, 2]},
    )

    # Energy spans two orders of magnitude, hence dots on a logarithmic axis instead of bars.
    energy.hlines(rows, summary["energy_min_j"], summary["energy_worst_j"], color=SERIES, linewidth=2)
    energy.plot(
        summary["energy_mean_j"], rows, "o", color=SERIES, markersize=8,
        markeredgecolor=SURFACE, markeredgewidth=1.5,
    )
    energy.set_xscale("log")
    for row in summary.itertuples():
        energy.annotate(
            f"{number(row.energy_mean_j)} J", (row.energy_worst_j, row.Index), xytext=(9, 0),
            textcoords="offset points", va="center", fontsize=9, color=INK,
        )
    (low, high) = (summary["energy_min_j"].min(), summary["energy_worst_j"].max())
    energy.set_xlim(low / 1.6, high * 3.2)
    energy.set_xlabel("energy per device for one update (J, logarithmic)", color=MUTED, fontsize=9)
    energy.set_yticks(list(rows))
    energy.set_yticklabels(summary["label"], color=INK, fontsize=9)
    energy.set_ylim(len(summary) - 0.5, -0.5)

    duration.plot(
        summary["fleet_time_h"], rows, "o", color=SERIES, markersize=8,
        markeredgecolor=SURFACE, markeredgewidth=1.5,
    )
    for row in summary.itertuples():
        duration.annotate(
            f"{row.fleet_time_h:.1f} h" + ("" if row.updated == row.devices
                                           else f" ({row.updated}/{row.devices} updated)"),
            (row.fleet_time_h, row.Index), xytext=(9, 0), textcoords="offset points",
            va="center", fontsize=9, color=INK,
        )
    duration.set_xlim(0, summary["fleet_time_h"].max() * 1.3)
    duration.set_xlabel("time until the last device is updated (h)", color=MUTED, fontsize=9)

    for ax in (energy, duration):
        ax.tick_params(colors=MUTED, length=0, labelsize=9)
        ax.tick_params(axis="y", colors=INK)
        ax.grid(axis="x", color=GRID, linewidth=0.8)
        ax.set_axisbelow(True)
        ax.set_facecolor(SURFACE)
        for spine in ax.spines.values():
            spine.set_visible(False)
    figure.suptitle(
        "One firmware update on the same deployment, by protocol", x=0.012, ha="left",
        color=INK, fontsize=12,
    )
    figure.text(0.012, 0.9, subtitle, color=MUTED, fontsize=9, va="top")
    figure.tight_layout(rect=(0, 0, 1, 0.88))
    figure.savefig(path, dpi=150, facecolor=SURFACE)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    parser.add_argument("--nodes", type=int, default=10)
    parser.add_argument("--radius", type=float, default=400.0, help="nodes are spread within this many meters")
    parser.add_argument("--firmware-kb", type=float, default=100.0, help="firmware size in kB (1000 bytes)")
    parser.add_argument("--exponent", type=float, default=3.2, help="path loss exponent")
    parser.add_argument("--duty-cycle", type=float, default=1.0,
                        help="gateway duty cycle in percent, for the protocols that have one")
    parser.add_argument("--uplink-interval", type=float, default=3600.0,
                        help="seconds between a LoRaWAN device's uplinks, which pace its setup")
    parser.add_argument("--class-b-periodicity", type=int, default=6,
                        help="LoRaWAN Class B ping periodicity; 6 keeps the gateway under 1%%")
    parser.add_argument("--seed", type=int, default=1, help="node placement and every protocol's own seed")
    parser.add_argument("--only", nargs="+", default=None, help="run only these protocol names")
    parser.add_argument("--jobs", type=int, default=os.cpu_count())
    args = parser.parse_args()

    name = f"n{args.nodes}_r{args.radius:g}_fw{args.firmware_kb:g}kb_s{args.seed}"
    out = HERE / "data" / name
    out.mkdir(parents=True, exist_ok=True)
    layout = out / "layout.csv"
    write_layout(layout, uniform_disc(args.nodes, args.radius, random.Random(args.seed)))

    chosen = [v for v in variants(args, layout, out) if not args.only or v.name in args.only]
    print(f"{len(chosen)} protocols on {args.nodes} nodes within {args.radius:g} m, "
          f"{args.firmware_kb:g} kB firmware")
    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        nodes = pd.concat(pool.map(lambda variant: run(variant, out), chosen), ignore_index=True)
    nodes.to_csv(out / "comparison_nodes.csv", index=False, float_format="%.6g")

    summary = summarise(nodes)
    summary.to_csv(out / "comparison_summary.csv", index=False, float_format="%.6g")
    shown = summary.drop(columns=["protocol", "energy_min_j"]).rename(columns={"label": "protocol"})
    print(shown.to_string(index=False, float_format=lambda value: f"{value:.3g}"))

    subtitle = (
        f"{args.nodes} nodes within {args.radius:g} m, {args.firmware_kb:g} kB image, SF7, "
        f"no fading; dot: mean device, line: best to worst device"
    )
    plot(summary, out / "comparison.png", subtitle)
    print(f"results in {out}")


if __name__ == "__main__":
    main()
