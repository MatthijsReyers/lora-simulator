#!/usr/bin/env python3
"""
Thesis graphs for the reproduction: RSSI timelines like Figures 8 to 13 of the paper, the per
node update time and frame loss of the only unicast method against the analytical model of
section 4, the total update times of the three methods next to the values reported in the
paper, and the spread of the 2 km results over seeds.

Run from the repository root after `main.py` produced the 10 node runs (seed 0 with
`--frame-log` for the timelines, seeds 1 and up for the averages).
"""
import math, os, sys
sys.path.append('.')
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from scipy.stats import gamma

# Blue, red and green are the three of the thesis colours that stay distinguishable for
# colour blind readers (validated with the dataviz palette checks).
from colors import BLUE_DARK, GREEN_DARK, RED_DARK
from scenario import Scenario
from simulator.path_loss.log_distance_path_loss import log_distance_path_loss

PAPER_DIR = './papers/fuota-unicast-broadcast-2024'
DATA_DIR = os.path.join(PAPER_DIR, 'data')

METHODS = {
    'unicast_only': 'Only unicast',
    'broadcast_unicast': 'Broadcast + unicast',
    'broadcast_only': 'Only broadcast',
}

# Total update time of the binary exchange reported in section 5.2 of the paper (single runs).
PAPER_TOTALS = {
    'unicast_only': {400: 176_725, 2000: 294_613},
    'broadcast_unicast': {400: 16_775, 2000: 136_413},
    'broadcast_only': {400: 17_542, 2000: 66_874},
}

GRID = '#e1e0d9'
MUTED = '#898781'
SHADE = '#f2f2ef'
FREQUENCY = 868_100_000


def run_name(
    radius: int, paper_frames: bool, method: str = 'unicast_only', nodes: int = 10,
    seed: int = 0, rounds: int = 1,
) -> str:
    frames = 'p' if paper_frames else 'f'
    suffix = f'_b{rounds}' if method != 'unicast_only' else ''
    return f'{method}_n{nodes}_r{radius}_{frames}215{suffix}_s{seed}'


def load(name: str, kind: str) -> pd.DataFrame:
    return pd.read_csv(os.path.join(DATA_DIR, f'{name}_{kind}.csv'))


def load_seeds(radius: int, paper_frames: bool, method: str = 'unicast_only') -> pd.DataFrame:
    """ Summaries of every seed above 0 (seed 0 is the single run used for the timelines). """
    prefix = run_name(radius, paper_frames, method, seed=0).rsplit('_s0', 1)[0] + '_s'
    rows = [
        pd.read_csv(os.path.join(DATA_DIR, f))
        for f in os.listdir(DATA_DIR)
        if f.startswith(prefix) and f.endswith('_summary.csv') and not f.endswith('_s0_summary.csv')
    ]
    return pd.concat(rows).sort_values('seed') if rows else pd.DataFrame()


def mean_and_ci(values: pd.Series) -> tuple[float, float]:
    """ Mean and half width of the 95% confidence interval, in hours. """
    hours = values / 3600
    if len(hours) < 2:
        return float(hours.mean()), 0.0
    return float(hours.mean()), float(1.96 * hours.std() / math.sqrt(len(hours)))


def seeded(radius: int, paper_frames: bool, method: str) -> tuple[float, float, int]:
    """ Mean, confidence interval and number of runs of a configuration, in hours. """
    seeds = load_seeds(radius, paper_frames, method)
    if seeds.empty:
        seeds = load(run_name(radius, paper_frames, method), 'summary')
    (mean, ci) = mean_and_ci(seeds.binary_total_time)
    return (mean, ci, len(seeds))


def style(ax):
    ax.grid(color=GRID, linewidth=0.6, zorder=0)
    ax.set_axisbelow(True)
    for spine in ('top', 'right'):
        ax.spines[spine].set_visible(False)
    for spine in ('left', 'bottom'):
        ax.spines[spine].set_color(MUTED)
    ax.tick_params(colors=MUTED, labelcolor='#52514e')


def save(fig, filename: str):
    out_path = os.path.join(PAPER_DIR, filename)
    fig.savefig(out_path, dpi=150, bbox_inches='tight')
    plt.close(fig)
    print(f'Saved: {out_path}')


# ── Analytical model of section 4 ───────────────────────────────────────────

def frame_loss_probability(distance: float, scenario: Scenario) -> float:
    """
        Probability that a single frame fades below the sensitivity at `distance`, using the
        same log-distance + Nakagami model the simulation runs on.
    """
    m = 1.5 if distance < 80 else 0.75
    path_loss = log_distance_path_loss(exponent=scenario.path_loss_exponent)(distance, FREQUENCY)
    margin_db = scenario.tx_power - path_loss - scenario.sensitivity
    return float(gamma.cdf(10 ** (-margin_db / 10), a=m, scale=1 / m))


def analytical_update_time(distance: float, scenario: Scenario) -> float:
    """ Equation (7) of the paper for one node, a fragment exchange fails if either frame does. """
    q = frame_loss_probability(distance, scenario)
    p = 1 - (1 - q) ** 2
    toa_chunk = scenario.airtime(scenario.header_len + 3 + scenario.chunk_size)
    toa_ack = scenario.airtime(scenario.header_len + 3)
    return scenario.fragments * (1 / scenario.duty_cycle) * (toa_chunk / (1 - p) + toa_ack)


# ── Figures 8 to 13: RSSI timelines ─────────────────────────────────────────

def rssi_timeline(method: str, radius: int, filename: str):
    name = run_name(radius, paper_frames=True, method=method)
    frames = load(name, 'frames')
    nodes = load(name, 'nodes')
    broadcast = method != 'unicast_only'

    fig, axes = plt.subplots(2, 1, figsize=(9, 6.5), sharex=True)
    directions = [
        (axes[0], frames[frames.source == 0], 'Frames from gateway to nodes (firmware fragments)'),
        (axes[1], frames[frames.source != 0], 'Frames from nodes to gateway (acknowledgements)'),
    ]
    label_y = -68
    total = nodes.end.max()
    for (ax, df, title) in directions:
        style(ax)
        # The nodes are served one after the other, so time already separates them: shade
        # every other node's slot and label it instead of colouring ten series. Narrow slots
        # (nodes that missed only a few chunks) get a shorter label or none at all.
        if broadcast:
            end = nodes.pending_start.min()
            ax.axvspan(nodes.binary_start.min() / 3600, end / 3600, color=SHADE, zorder=0, lw=0)
            ax.text(
                (nodes.binary_start.min() + end) / 7200, label_y, 'broadcast\nround',
                ha='center', va='top', fontsize=7, color='#52514e',
            )
        for (i, node) in enumerate(nodes.itertuples()):
            if i % 2 == (0 if broadcast else 1):
                ax.axvspan(node.pending_start / 3600, node.end / 3600, color=SHADE, zorder=0, lw=0)
            width = (node.end - node.pending_start) / total
            if width > 0.06:
                label = f'{node.node}\n{node.distance:.0f} m'
            elif width > 0.02:
                label = f'{node.node}'
            else:
                continue
            ax.text(
                (node.pending_start + node.end) / 7200, label_y, label,
                ha='center', va='top', fontsize=7, color='#52514e',
            )
        ax.scatter(
            df.time / 3600, df.rssi, s=3, color=BLUE_DARK, alpha=0.55, linewidths=0, zorder=3,
        )
        ax.axhline(-125, color=RED_DARK, linewidth=1.5, zorder=4)
        ax.text(
            0.995, -125 + 1.5, 'sensitivity -125 dBm', ha='right', va='bottom', fontsize=8,
            color=RED_DARK, transform=ax.get_yaxis_transform(), zorder=5,
            bbox=dict(facecolor='white', edgecolor='none', alpha=0.85, pad=1.5),
        )
        ax.set_title(title, fontsize=10, loc='left')
        ax.set_ylabel('RSSI (dBm)')
        low = frames.rssi.min() if len(df) else -160
        ax.set_ylim(min(-160, math.floor(low / 10) * 10), -65)
    axes[1].set_xlabel('Simulation time (h)')
    axes[1].set_xlim(0, nodes.end.max() / 3600)
    fig.suptitle(
        f'{METHODS[method]} update of 10 nodes within {radius} m '
        '(node number and distance per slot)',
        fontsize=11, x=0.02, ha='left',
    )
    fig.tight_layout()
    save(fig, filename)


# ── Per node results of the only unicast method against the analytical model ─

def per_node_vs_distance(filename: str):
    scenario = Scenario(header_on_air=False)
    runs = [
        (400, 'o', BLUE_DARK, '400 m scenario'),
        (2000, 's', RED_DARK, '2 km scenario'),
    ]
    fig, axes = plt.subplots(1, 2, figsize=(10, 4))
    for ax in axes:
        style(ax)

    distances = np.linspace(20, 2000, 300)
    axes[0].plot(
        distances, [analytical_update_time(d, scenario) / 3600 for d in distances],
        color=GREEN_DARK, linewidth=2, zorder=2, label='Analytical model (eq. 7)',
    )
    axes[1].plot(
        distances, [100 * frame_loss_probability(d, scenario) for d in distances],
        color=GREEN_DARK, linewidth=2, zorder=2, label='Channel model',
    )

    for (radius, marker, color, label) in runs:
        name = run_name(radius, paper_frames=True)
        nodes = load(name, 'nodes')
        frames = load(name, 'frames')
        to_nodes = frames[frames.source == 0]
        loss = 100 * (1 - to_nodes.groupby('destination').delivered.mean())
        axes[0].scatter(
            nodes.distance, nodes.binary_time / 3600, marker=marker, s=36, color=color,
            edgecolors='white', linewidths=0.8, zorder=3, label=label,
        )
        axes[1].scatter(
            loss.index.map(dict(zip(nodes.node, nodes.distance))), loss.values, marker=marker,
            s=36, color=color, edgecolors='white', linewidths=0.8, zorder=3, label=label,
        )

    axes[0].set_xlabel('Distance to gateway (m)')
    axes[0].set_ylabel('Update time of the node (h)')
    axes[0].set_title('Only unicast: binary exchange time per node', fontsize=10, loc='left')
    axes[0].set_ylim(0)
    axes[0].legend(fontsize='small', loc='upper left', frameon=False)

    axes[1].set_xlabel('Distance to gateway (m)')
    axes[1].set_ylabel('Lost fragments (%)')
    axes[1].set_title('Only unicast: fragment loss per node', fontsize=10, loc='left')
    axes[1].set_ylim(0)
    axes[1].legend(fontsize='small', loc='upper left', frameon=False)
    for ax in axes:
        ax.set_xlim(0, 2050)
    fig.tight_layout()
    save(fig, filename)


# ── Totals next to the paper ────────────────────────────────────────────────

def paper_comparison(filename: str):
    """
        Total update time of the three methods next to the paper's single runs, one panel per
        radius. Simulated bars average the seeded runs with a 95% confidence interval.
    """
    radii = [400, 2000]
    series = [
        ('Paper (ns-3, single run)', BLUE_DARK, lambda m, r: (PAPER_TOTALS[m][r] / 3600, 0.0, 1)),
        ('Simulated, paper frames', RED_DARK, lambda m, r: seeded(r, True, m)),
        ('Simulated, full MiWi header', GREEN_DARK, lambda m, r: seeded(r, False, m)),
    ]
    fig, axes = plt.subplots(1, 2, figsize=(11, 4.2))
    width = 0.26
    x = np.arange(len(METHODS))
    for (ax, radius) in zip(axes, radii):
        style(ax)
        top = 0.0
        for (i, (label, color, value)) in enumerate(series):
            stats = [value(m, radius) for m in METHODS]
            hours = [s[0] for s in stats]
            errors = [s[1] for s in stats]
            runs = max(s[2] for s in stats)
            if runs > 1:
                label = f'{label} (mean, 95% CI)'
            elif 'Simulated' in label:
                label = f'{label} (single run)'
            positions = x + (i - 1) * (width + 0.02)
            bars = ax.bar(positions, hours, width=width, color=color, label=label, zorder=3)
            ax.errorbar(
                positions, hours, yerr=errors, fmt='none', ecolor='#52514e', elinewidth=1,
                capsize=3, zorder=4,
            )
            for (bar, error) in zip(bars, errors):
                ax.text(
                    bar.get_x() + bar.get_width() / 2, bar.get_height() + error + top * 0.01,
                    f'{bar.get_height():.0f} h', ha='center', va='bottom', fontsize=8,
                    color='#52514e',
                )
            top = max(top, max(h + e for (h, e) in zip(hours, errors)))
        ax.set_xticks(x, list(METHODS.values()))
        ax.set_ylim(0, top * 1.18)
        ax.set_title(f'{radius} m radius', fontsize=10, loc='left')
    axes[0].set_ylabel('Time to deliver the binary to all 10 nodes (h)')
    axes[0].legend(fontsize='small', loc='upper right', frameon=False)
    fig.suptitle(
        'Total update time of the three methods, 10 nodes, 100 kB firmware, one broadcast round',
        fontsize=11, x=0.02, ha='left',
    )
    fig.tight_layout()
    save(fig, filename)


# ── Spread over seeds at 2 km ───────────────────────────────────────────────

def seed_spread(filename: str):
    """ Every seeded 2 km run as a dot, with the mean and the paper's single run, per method. """
    fig, axes = plt.subplots(len(METHODS), 1, figsize=(8, 7.5), sharex=True)
    for (ax, (method, title)) in zip(axes, METHODS.items()):
        style(ax)
        seeds = load_seeds(2000, paper_frames=True, method=method)
        hours = seeds.binary_total_time / 3600
        (mean, ci) = mean_and_ci(seeds.binary_total_time)
        paper = PAPER_TOTALS[method][2000] / 3600
        ax.scatter(
            seeds.seed, hours, s=36, color=RED_DARK, edgecolors='white', linewidths=0.8,
            zorder=3, label='Simulated run (paper frames)',
        )
        ax.axhspan(mean - ci, mean + ci, color=SHADE, zorder=0, lw=0)
        ax.axhline(mean, color=RED_DARK, linewidth=1.5, zorder=2, label=f'Mean {mean:.1f} h (95% CI shaded)')
        ax.axhline(
            paper, color=BLUE_DARK, linewidth=1.5, linestyle='--', zorder=2,
            label=f'Paper, single run ({paper:.1f} h)',
        )
        ax.set_ylabel('Time (h)')
        ax.set_title(f'{title} at 2 km, 10 nodes', fontsize=10, loc='left')
        ax.set_xticks(seeds.seed)
        ax.set_ylim(0, math.ceil(max(hours.max(), paper) / 20) * 20)
        ax.legend(fontsize='small', loc='lower left', frameon=False, ncol=3)
    axes[-1].set_xlabel('Seed (node placement and fading)')
    fig.tight_layout()
    save(fig, filename)


if __name__ == '__main__':
    rssi_timeline('unicast_only', 400, 'unicast_rssi_400m.png')
    rssi_timeline('unicast_only', 2000, 'unicast_rssi_2km.png')
    rssi_timeline('broadcast_unicast', 400, 'broadcast_unicast_rssi_400m.png')
    rssi_timeline('broadcast_unicast', 2000, 'broadcast_unicast_rssi_2km.png')
    rssi_timeline('broadcast_only', 400, 'broadcast_only_rssi_400m.png')
    rssi_timeline('broadcast_only', 2000, 'broadcast_only_rssi_2km.png')
    per_node_vs_distance('unicast_per_node_vs_distance.png')
    paper_comparison('methods_paper_comparison.png')
    seed_spread('methods_seed_spread_2km.png')
