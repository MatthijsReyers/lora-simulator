"""
    Draws who transmits when and on which channel, from the radio logs of a run:

        pipenv run python examples/ews_fuota/timeline.py examples/ews_fuota/data/<run name>

    One row per device, three zoom levels: the whole run, the start of the first interval with
    an update, and the first seconds of that update. Writes `timeline.png` into the run directory.
"""
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd
from matplotlib.patches import Patch

# Fixed order: the async channel first, then the pool channels as they come.
CHANNEL_COLORS = [
    "#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948",
]
SURFACE = "#fcfcfb"
INK = "#1f1f1e"
MUTED = "#6b6a65"
GRID = "#e4e3de"
ASYNC_CHANNEL = 868.3


def load(run: Path) -> pd.DataFrame:
    logs = []
    for path in sorted((run / "radio_log").glob("*.csv")):
        log = pd.read_csv(path)
        log["device"] = path.stem
        logs.append(log[log["event"] == "tx"])
    return pd.concat(logs, ignore_index=True)


def draw(ax, tx: pd.DataFrame, devices: list[str], colors: dict[float, str],
         start: float, end: float, scale: float, unit: str, title: str) -> None:
    """ `scale` converts seconds to the unit of the x axis. """
    shown = tx[(tx["end_s"] >= start) & (tx["time_s"] <= end)]
    # A 31 ms ping would vanish at most zoom levels: no mark is drawn thinner than this.
    thinnest = (end - start) * 0.0015
    for (row, device) in enumerate(devices):
        mine = shown[shown["device"] == device]
        for (frequency, frames) in mine.groupby("channel_mhz"):
            widths = (frames["end_s"] - frames["time_s"]).clip(lower=thinnest)
            ax.broken_barh(
                list(zip(frames["time_s"] / scale, widths / scale)), (row - 0.32, 0.64),
                facecolors=colors[frequency], linewidth=0,
            )
    ax.set_xlim(start / scale, end / scale)
    ax.set_ylim(len(devices) - 0.4, -0.6)
    ax.set_yticks(range(len(devices)))
    ax.set_yticklabels([device.replace("_", " ") for device in devices], color=INK, fontsize=9)
    ax.set_xlabel(f"time ({unit})", color=MUTED, fontsize=9)
    ax.set_title(title, loc="left", color=INK, fontsize=11, pad=8)
    ax.tick_params(colors=MUTED, length=0, labelsize=9)
    ax.grid(axis="x", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    ax.set_facecolor(SURFACE)
    for spine in ax.spines.values():
        spine.set_visible(False)
    for row in range(len(devices)):
        ax.axhline(row, color=GRID, linewidth=0.6, zorder=0)


def label(ax, tx: pd.DataFrame, devices: list[str], scale: float, start: float, end: float):
    """ Names the first frame of every kind in the most detailed panel. """
    shown = tx[(tx["time_s"] >= start) & (tx["end_s"] <= end)].sort_values("time_s")
    seen: set[str] = set()
    for ((device, frame), frames) in shown.groupby(["device", "frame"], sort=False):
        first = frames.iloc[0]
        # The first two frames of a device sit close together: one label ends at its frame,
        # the next one starts at its frame.
        leading = device not in seen
        seen.add(device)
        ax.annotate(
            f"{frame} ({int(first['bytes'])} B)",
            (first["end_s" if leading else "time_s"] / scale, devices.index(device) - 0.32),
            xytext=(0, 3), textcoords="offset points", ha="right" if leading else "left",
            va="bottom", fontsize=8, color=INK,
        )


def main() -> None:
    run = Path(sys.argv[1])
    tx = load(run)
    devices = ["gateway"] + sorted(d for d in tx["device"].unique() if d != "gateway")

    pool = sorted(c for c in tx["channel_mhz"].unique() if c != ASYNC_CHANNEL)
    channels = [ASYNC_CHANNEL] + pool
    colors = dict(zip(channels, CHANNEL_COLORS))

    # The first update: from the first ping of its interval to shortly after its last frame.
    starts = tx[tx["frame"] == "MP_START"]["time_s"]
    update = float(starts.iloc[0]) if len(starts) else 0.0
    fragments = tx[(tx["frame"] == "FRAGMENT") & (tx["time_s"] >= update)]
    gaps = fragments["time_s"].diff().fillna(0)
    later = fragments[gaps > 60]
    update_end = float(later["time_s"].iloc[0]) if len(later) else float(tx["end_s"].max())
    update_end = float(fragments[fragments["time_s"] < update_end]["end_s"].max())
    first_ping = float(tx[tx["time_s"] <= update]["time_s"].min())

    # The most detailed panel only has rows for the devices that transmit in it.
    (detail_start, detail_end) = (update - 0.45, update + 3.2)
    busy = tx[(tx["end_s"] >= detail_start) & (tx["time_s"] <= detail_end)]["device"].unique()
    detail_devices = [device for device in devices if device in busy]

    rows = [len(devices), len(devices), len(detail_devices) + 1]
    (figure, axes) = plt.subplots(
        3, 1, figsize=(12, 3.6 + 0.32 * sum(rows)), facecolor=SURFACE,
        gridspec_kw={"height_ratios": rows},
    )
    total = float(tx["end_s"].max())

    draw(axes[0], tx, devices, colors, 0, total, 3600, "hours",
         "Whole run: every sensor pings at the start of each hour, one gets the update")
    span = update_end - first_ping
    draw(axes[1], tx, devices, colors, first_ping - 0.03 * span, update_end + 0.05 * span, 1,
         "seconds", "First update: static ping slots, then the sensor to update pings again at the FUOTA moment")
    draw(axes[2], tx, detail_devices, colors, detail_start, detail_end, 1, "seconds",
         "Start of the update: ping, MP_START, accept, then bursts of fragments")
    axes[2].set_ylim(len(detail_devices) - 0.4, -1.1)
    label(axes[2], tx, detail_devices, 1, detail_start, detail_end)

    figure.legend(
        handles=[Patch(color=colors[c], label=f"{c:.1f} MHz") for c in channels],
        loc="upper right", ncols=len(channels), frameon=False, fontsize=9,
        title="Channel of the transmission", title_fontsize=9, bbox_to_anchor=(0.985, 0.995),
    )
    figure.suptitle("Radio transmissions per device", x=0.015, ha="left", color=INK, fontsize=13)
    figure.tight_layout(rect=(0, 0, 1, 0.965), h_pad=2.0)
    figure.savefig(run / "timeline.png", dpi=150, facecolor=SURFACE)
    print(f"wrote {run / 'timeline.png'}")


if __name__ == "__main__":
    main()
