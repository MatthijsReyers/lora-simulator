"""Figures for the FUOTA example: fragment progress, phase timeline, airtime and power.

Pure plotting helpers — they only read what ``main.py`` already collected, so a run with
``--plots`` costs nothing extra in the simulation itself. Matplotlib is imported inside the
functions and forced onto the Agg backend, like ``examples/lorawan_clock_sync/plots.py``.
"""

from __future__ import annotations

import os

from simulator.lora.airtime import estimate_airtime
from simulator.lora.radio import LoraRadio
from simulator.lorawan.fuota.campaign import FuotaCampaign
from simulator.lorawan.fuota.device_stack import FuotaDeviceStack
from simulator.lorawan.fuota.frag_transport import (
    FRAGMENTATION_FPORT,
    DataFragment,
    parse_downlink_commands,
)
from simulator.lorawan.gateway import LoRaWanGateway
from simulator.lorawan.region import EU868_DATA_RATES

#: Colour per campaign phase in the timeline figure.
PHASE_COLOURS = {
    "IDLE": "#bdbdbd",
    "GROUP_SETUP": "#4878d0",
    "FRAG_SETUP": "#6acc64",
    "SESSION_SETUP": "#ee854a",
    "BROADCAST": "#d65f5f",
    "STATUS": "#956cb4",
    "REPAIR": "#8c613c",
    "CLEANUP": "#797979",
    "DONE": "#2ca02c",
    "FAILED": "#000000",
}


# ---------------------------------------------------------------------------
# Data extraction
# ---------------------------------------------------------------------------


def fragment_arrivals(stack: FuotaDeviceStack) -> list[float]:
    """Times at which a ``DataFragment`` was delivered to this device's TS004 package.

    Taken from the device's downlink log, i.e. every fragment frame the radio demodulated
    and the LoRaWAN layer accepted. With ``--loss`` the package throws some of them away
    afterwards, so this curve is the *delivered* count, an upper bound on the accepted one.
    """
    times = []
    for time, fport, payload in stack.downlink_log:
        if fport != FRAGMENTATION_FPORT:
            continue
        try:
            commands = parse_downlink_commands(payload)
        except ValueError:
            continue
        if any(isinstance(command, DataFragment) for command in commands):
            times.append(time)
    return times


def session_windows(stack: FuotaDeviceStack) -> list[tuple[float, float]]:
    """``(start, end)`` of every multicast session the device actually scheduled."""
    return [
        (float(record.session_time), float(record.session_time + record.timeout_seconds))
        for record in stack.multicast_setup.session_history
        if record.started
    ]


def phase_spans(campaign: FuotaCampaign, end_time: float) -> list[tuple[str, float, float]]:
    """``(phase, start, end)`` for every state the campaign passed through, in order."""
    spans: list[tuple[str, float, float]] = []
    history = campaign.state_history
    for index, (time, state) in enumerate(history):
        stop = history[index + 1][0] if index + 1 < len(history) else end_time
        spans.append((state.value, time, stop))
    return spans


# ---------------------------------------------------------------------------
# Figures
# ---------------------------------------------------------------------------


def plot_fragment_progress(
    campaign: FuotaCampaign, stacks: list[FuotaDeviceStack], path: str
) -> str:
    """(a) Per-device fragment reception over time, with the session windows shaded."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(11, 5))

    shaded: set[tuple[float, float]] = set()
    for stack in stacks:
        for window in session_windows(stack):
            if window in shaded:
                continue
            shaded.add(window)
            ax.axvspan(
                window[0], window[1], color="#ffd966", alpha=0.25, zorder=0,
                label="multicast session window" if len(shaded) == 1 else None,
            )

    for stack in stacks:
        times = fragment_arrivals(stack)
        if not times:
            continue
        counts = range(1, len(times) + 1)
        # Devices that lose nothing produce identical curves; the alpha makes the overlap
        # visible instead of leaving only the last one drawn.
        ax.step(
            [times[0], *times], [0, *counts], where="post", linewidth=1.4, alpha=0.65,
            label=f"0x{stack.dev_addr:08X}",
        )

    nb_frag = campaign.result.nb_frag
    if nb_frag:
        ax.axhline(
            nb_frag, linestyle="--", linewidth=1, color="black",
            label=f"NbFrag = {nb_frag} (enough to decode)",
        )

    ax.set_xlabel("Simulation time [s]")
    ax.set_ylabel("DataFragment frames delivered")
    ax.set_title("TS004 fragment reception per device")
    ax.grid(alpha=0.3)
    ax.legend(loc="upper left", fontsize=7, ncol=2)
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)
    return path


def plot_phase_timeline(campaign: FuotaCampaign, path: str) -> str:
    """(b) The campaign's phases as a Gantt-style bar, from ``state_history``."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Patch

    spans = phase_spans(campaign, campaign.result.finished_at)

    fig, ax = plt.subplots(figsize=(11, 2.8))
    for phase, start, stop in spans:
        width = max(stop - start, 0.0)
        ax.barh(
            0, width, left=start, height=0.6,
            color=PHASE_COLOURS.get(phase, "#999999"), edgecolor="white",
        )
        if width > campaign.result.total_time * 0.04:
            ax.text(
                start + width / 2, 0, phase, ha="center", va="center",
                fontsize=7, color="white",
            )

    seen = []
    for phase, _start, _stop in spans:
        if phase not in seen:
            seen.append(phase)
    ax.legend(
        handles=[Patch(color=PHASE_COLOURS.get(p, "#999999"), label=p) for p in seen],
        loc="upper center", bbox_to_anchor=(0.5, -0.25), ncol=min(len(seen), 6),
        fontsize=7, frameon=False,
    )

    ax.set_yticks([])
    ax.set_xlabel("Simulation time [s]")
    ax.set_title(
        f"FUOTA campaign phases  —  {campaign.result.state.value} after "
        f"{campaign.result.total_time:.0f}s"
    )
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)
    return path


def plot_gateway_airtime(
    campaign: FuotaCampaign, gateway: LoRaWanGateway, sim_length: float, path: str
) -> str:
    """(c) Cumulative multicast airtime against the gateway's duty-cycle budget."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    dr = EU868_DATA_RATES[campaign.config.data_rate]
    entries = sorted(gateway.multicast_log, key=lambda entry: entry[0])

    times = [0.0]
    cumulative = [0.0]
    total = 0.0
    for time, _group, _fport, length in entries:
        airtime = estimate_airtime(
            payload_len=length,
            bandwidth=dr.bandwidth.to_khz(),
            spreading_factor=dr.spreading_factor.value,
            code_rate=5,
        )
        total += airtime
        times.append(time)
        cumulative.append(total)
    # The simulation is sized with a safety margin; show the campaign, not the tail.
    end = min(sim_length, max(campaign.result.finished_at * 1.05, 1.0))
    times.append(end)
    cumulative.append(total)

    fig, (top, bottom) = plt.subplots(
        2, 1, figsize=(11, 5.5), sharex=True, height_ratios=[2, 1]
    )

    top.step(times, cumulative, where="post", linewidth=1.5, label="multicast airtime")
    if gateway.duty_cycle is not None:
        budget = [t * gateway.duty_cycle.duty_cycle for t in times]
        top.plot(
            times, budget, linestyle="--", linewidth=1, color="red",
            label=f"{gateway.duty_cycle.duty_cycle:.0%} duty-cycle budget",
        )
    top.set_ylabel("Cumulative airtime [s]")
    top.set_title("Gateway multicast transmissions and duty cycle")
    top.grid(alpha=0.3)
    top.legend(loc="upper left", fontsize=8)

    if entries:
        bottom.eventplot(
            [entry[0] for entry in entries], lineoffsets=0.5, linelengths=0.8,
            colors="#d65f5f",
        )
    else:
        bottom.text(
            0.5, 0.5, "no frames in the multicast log\n"
            "(a Class B campaign sends them through the ping-slot scheduler)",
            ha="center", va="center", transform=bottom.transAxes, fontsize=8,
        )
    bottom.set_yticks([])
    bottom.set_ylim(0, 1)
    bottom.set_xlabel("Simulation time [s]")
    bottom.set_ylabel("frames")
    bottom.set_xlim(0, end)

    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)
    return path


def power_trace_segments(
    radio: LoraRadio, sim_length: float
) -> list[tuple[float, float, float]]:
    """The radio's power trace as ``(start, end, power)`` segments covering the run."""
    events = radio.power_consumer.events
    times = list(events["time"]) + [sim_length]
    powers = list(events["power"])
    return [(times[i], times[i + 1], powers[i]) for i in range(len(powers))]


def plot_device_power(
    stack: FuotaDeviceStack, campaign: FuotaCampaign, sim_length: float, path: str
) -> str | None:
    """(d) One device's radio power over time, with the session windows shaded."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    events = getattr(stack.device.radio.power_consumer, "events", None)
    if events is None or len(events) == 0:
        return None

    segments = power_trace_segments(stack.device.radio, sim_length)
    times = [segments[0][0]] + [end for _, end, _ in segments]
    powers_mw = [power * 1000 for _, _, power in segments]
    powers_mw.append(powers_mw[-1])

    end = min(sim_length, max(campaign.result.finished_at * 1.05, 1.0))

    fig, ax = plt.subplots(figsize=(11, 4))
    ax.set_xlim(0, end)
    for start, stop in session_windows(stack):
        ax.axvspan(start, stop, color="#ffd966", alpha=0.3, zorder=0)

    # Drawn on top of the reference lines: during a Class C session the trace sits exactly
    # on the RX line and would otherwise disappear underneath it.
    ax.plot(times, powers_mw, drawstyle="steps-post", linewidth=1.0, zorder=3)

    profile = stack.device.radio.power_profile
    sleep_mw = profile.sleep_power() * 1000
    rx_mw = profile.rx_power(stack.device.radio.rx_chains[0].config) * 1000
    ax.axhline(
        sleep_mw, linestyle="--", linewidth=0.8, color="green", zorder=1,
        label=f"sleep ({sleep_mw:.1f} mW)",
    )
    ax.axhline(
        rx_mw, linestyle="--", linewidth=0.8, color="red", zorder=1,
        label=f"RX ({rx_mw:.1f} mW)",
    )

    energy = stack.energy_consumed
    ax.set_xlabel("Simulation time [s]")
    ax.set_ylabel("Power [mW]")
    ax.set_title(
        f"Radio power of 0x{stack.dev_addr:08X} during the campaign"
        + (f"  —  {energy:.1f} J total" if energy is not None else "")
        + "  (shaded: multicast session window)"
    )
    ax.legend(loc="upper right", fontsize=8)
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)
    return path


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def plot_campaign(
    *,
    campaign: FuotaCampaign,
    gateway: LoRaWanGateway,
    stacks: list[FuotaDeviceStack],
    sim_length: float,
    directory: str,
    prefix: str = "fuota",
) -> list[str]:
    """Write all four figures next to the example and return the paths written."""
    paths: list[str] = []
    paths.append(
        plot_fragment_progress(
            campaign, stacks, os.path.join(directory, f"{prefix}_fragment_progress.png")
        )
    )
    paths.append(
        plot_phase_timeline(
            campaign, os.path.join(directory, f"{prefix}_phase_timeline.png")
        )
    )
    paths.append(
        plot_gateway_airtime(
            campaign, gateway, sim_length,
            os.path.join(directory, f"{prefix}_gateway_airtime.png"),
        )
    )
    if stacks:
        power = plot_device_power(
            stacks[0], campaign, sim_length,
            os.path.join(directory, f"{prefix}_device_power.png"),
        )
        if power is not None:
            paths.append(power)
    return paths
