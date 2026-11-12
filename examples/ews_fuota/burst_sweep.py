"""
    Energy of one firmware update against the number of fragments per burst, one line per level
    of frame loss. A level applies in both directions: to the downlink frames at the sensor and
    to the uplink frames at the gateway.

        pipenv run python examples/ews_fuota/burst_sweep.py --bursts 1 2 4 8 16 32 --losses 0 0.05

    Runs `main.py` once per burst size, loss level and seed (a process can only hold one
    simulation) and writes `burst_sweep.csv` and `burst_sweep.png` into
    `data/burst_sweep<passed on arguments>/`. Arguments it does not know, such as
    `--image-bytes`, are passed on to `main.py`.

    The energy of an update includes the transfers that failed on the way to it: everything the
    sensors spent in update wake-ups, divided by the number of updates that completed.
"""
import argparse
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from itertools import product
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

HERE = Path(os.path.dirname(os.path.abspath(__file__)))
SURFACE = "#fcfcfb"
INK = "#1f1f1e"
MUTED = "#6b6a65"
GRID = "#e4e3de"
# One hue, light to dark with the amount of loss.
RAMP = ["#86b6ef", "#5598e7", "#2a78d6", "#1c5cab", "#0d366b"]


def sweep_directory(passed_on: list[str]) -> Path:
    """ One directory per set of passed on arguments, so that sweeps do not overwrite each other. """
    suffix = "".join("_" + argument.lstrip("-") for argument in passed_on)
    return HERE / "data" / f"burst_sweep{suffix}"


def run(burst: int, loss: float, seed: int, passed_on: list[str]) -> dict:
    out = sweep_directory(passed_on) / f"loss{loss:g}_burst{burst}_seed{seed}"
    command = [
        sys.executable, str(HERE / "main.py"), "--burst-packets", str(burst),
        "--downlink-loss", str(loss), "--uplink-loss", str(loss),
        "--seed", str(seed), "--out", str(out),
    ]
    subprocess.run(command + passed_on, check=True, stdout=subprocess.DEVNULL)
    cycles = pd.read_csv(out / "cycles.csv")
    updates = cycles[cycles["kind"] == "update"]
    return {
        "loss": loss,
        "burst_packets": burst,
        "completed": int((updates["outcome"] == "complete").sum()),
        "failed": int((updates["outcome"] != "complete").sum()),
        "awake_s": float(updates["awake_s"].sum()),
        "energy_j": float(updates["energy_j"].sum()),
    }


def plot(results: pd.DataFrame, path: Path, subtitle: str) -> None:
    losses = sorted(results["loss"].unique())
    # Spread the levels over the ramp, the highest loss always the darkest.
    steps = [round(i * (len(RAMP) - 1) / max(len(losses) - 1, 1)) for i in range(len(losses))]
    if len(losses) == 1:
        steps = [2]

    # A point where most transfers fail costs many times the rest and would flatten every
    # line: it is left off the scale and named at the top edge instead.
    ceiling = 2 * float(results["energy_j"].median())
    shown = results["energy_j"] <= ceiling  # false as well where no update completed at all
    (off_scale, results) = (results[~shown], results[shown])

    (figure, ax) = plt.subplots(figsize=(8.6, 5), facecolor=SURFACE)
    for (loss, step) in zip(losses, steps):
        line = results[results["loss"] == loss].sort_values("burst_packets")
        if line.empty:
            continue
        ax.plot(
            line["burst_packets"], line["energy_j"], color=RAMP[step], linewidth=2,
            marker="o", markersize=6, markeredgecolor=SURFACE, markeredgewidth=1.2,
            label=f"{loss * 100:g}%",
        )
        # A ring around the cheapest burst size of this loss level.
        lowest = line.loc[line["energy_j"].idxmin()]
        ax.plot(
            lowest["burst_packets"], lowest["energy_j"], marker="o", markersize=12,
            markerfacecolor="none", markeredgecolor=INK, markeredgewidth=1.2,
        )

    for (row, point) in enumerate(off_scale.itertuples()):
        ax.text(
            0.985, 0.84 - 0.05 * row,
            f"Not shown: {point.loss * 100:g}% loss at {point.burst_packets} per burst, "
            + (f"{point.energy_j:.0f} J" if point.completed else "no update completed"),
            transform=ax.transAxes, ha="right", va="top", fontsize=9, color=INK,
        )

    bursts = sorted(set(results["burst_packets"]) | set(off_scale["burst_packets"]))
    ax.set_xscale("log", base=2)
    ax.set_xticks(bursts)
    ax.set_xticklabels([str(burst) for burst in bursts])
    ax.minorticks_off()
    ax.set_xlim(bursts[0] / 1.12, bursts[-1] * 1.12)
    ax.set_xlabel("fragments per burst", color=MUTED, fontsize=10)
    ax.set_ylabel("energy per completed update (J)", color=MUTED, fontsize=10)
    ax.set_title(
        "Sensor energy for one firmware update by burst size", loc="left", color=INK,
        fontsize=12, pad=24 + 12 * subtitle.count("\n"),
    )
    ax.text(0, 1.03, subtitle, transform=ax.transAxes, color=MUTED, fontsize=9)
    ax.tick_params(colors=MUTED, length=0, labelsize=9)
    ax.grid(axis="y", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    ax.set_facecolor(SURFACE)
    for spine in ax.spines.values():
        spine.set_visible(False)
    if len(losses) > 1:
        legend = ax.legend(
            title="Frame loss, both directions", frameon=False, fontsize=9, title_fontsize=9,
            loc="upper right", ncols=len(losses),
        )
        legend.get_title().set_color(MUTED)
    figure.tight_layout()
    figure.savefig(path, dpi=150, facecolor=SURFACE)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    parser.add_argument("--bursts", type=int, nargs="+", default=[1, 2, 3, 4, 6, 8, 12, 16, 24, 32])
    parser.add_argument("--losses", type=float, nargs="+", default=[0.0, 0.02, 0.05, 0.10],
                        help="shares of frames lost, in both directions")
    parser.add_argument("--seeds", type=int, default=3, help="runs per point")
    parser.add_argument("--jobs", type=int, default=os.cpu_count(), help="runs at the same time")
    (args, passed_on) = parser.parse_known_args()

    points = list(product(sorted(args.bursts), sorted(args.losses), range(1, args.seeds + 1)))
    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        runs = pd.DataFrame(pool.map(
            lambda point: run(point[0], point[1], point[2], passed_on), points,
        ))

    # Pool the seeds: all the energy spent on updates over all the updates that completed.
    results = runs.groupby(["loss", "burst_packets"], as_index=False).sum()
    completed = results["completed"].where(results["completed"] > 0)
    results["awake_s"] /= completed
    results["energy_j"] /= completed

    directory = sweep_directory(passed_on)
    results.to_csv(directory / "burst_sweep.csv", index=False, float_format="%.6g")
    print(results.to_string(index=False))

    subtitle = (
        f"failed transfers included, {args.seeds} runs per point, ring: lowest of its line, "
        "y axis does not start at zero"
    )
    if passed_on:
        subtitle += "\n" + " ".join(passed_on)
    plot(results, directory / "burst_sweep.png", subtitle)
    print(f"wrote {directory / 'burst_sweep.png'}")


if __name__ == "__main__":
    main()
