from __future__ import annotations
from simulator.lora.radio import LoraRadio


def power_trace_segments(radio: LoraRadio, sim_length: float) -> list[tuple[float, float, float]]:
    """The radio's power trace as (start, end, power) segments covering the whole simulation."""
    events = radio.power_consumer.events
    times = list(events["time"]) + [sim_length]
    powers = list(events["power"])
    return [(times[i], times[i + 1], powers[i]) for i in range(len(powers))]


def plot_power_trace(radio: LoraRadio, segments: list[tuple[float, float, float]], path: str) -> None:
    """Plot the radio's power consumption over time as a stepped line."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    times = [segments[0][0]] + [end for _, end, _ in segments]
    powers_mw = [power * 1000 for _, _, power in segments]
    powers_mw.append(powers_mw[-1])

    profile = radio.power_profile
    sleep_mw = profile.sleep_power() * 1000
    rx_mw = profile.rx_power(radio.rx_chains[0].config) * 1000

    fig, ax = plt.subplots(figsize=(10, 4))
    ax.plot(times, powers_mw, drawstyle="steps-post", linewidth=1.5)
    ax.axhline(sleep_mw, linestyle="--", linewidth=1, color="green", label=f"Sleep ({sleep_mw:.1f} mW)")
    ax.axhline(rx_mw, linestyle="--", linewidth=1, color="red", label=f"RX ({rx_mw:.1f} mW)")
    ax.set_xlabel("Simulation time [s]")
    ax.set_ylabel("Power [mW]")
    ax.set_title("Clock sync sensor radio power consumption (Class A)")
    ax.legend(loc="center right")
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)
