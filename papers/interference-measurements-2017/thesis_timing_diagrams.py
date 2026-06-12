#!/usr/bin/env python3
"""Generate timing diagram illustrations showing the three capture effect
scenarios for the thesis section 6.1.2."""
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np
import sys
sys.path.append('.')
from colors import *

# SF12, BW125, preamble=8, 17B payload
# Preamble: ~401 ms, Header: ~164 ms, Total: ~1712 ms
PREAMBLE = 0.401
HEADER = 0.164
TOTAL = 1.712
PAYLOAD = TOTAL - PREAMBLE - HEADER

OUT_DIR = './papers/interference-measurements-2017'

RECEIVED_COLOR = BLUE_DARK
LOST_COLOR = RED


def hex_to_rgba(hex_color: str, alpha: float):
    h = hex_color.lstrip('#')
    r, g, b = int(h[0:2], 16)/255, int(h[2:4], 16)/255, int(h[4:6], 16)/255
    return (r, g, b, alpha)


def draw_packet(ax, y, start, outcome):
    """Draw a packet as three horizontal bars (preamble, header, payload).
    Color is based on outcome: blue for received, red for lost."""
    color = RECEIVED_COLOR if outcome == 'success' else LOST_COLOR

    pre_color = hex_to_rgba(color, 0.9)
    hdr_color = hex_to_rgba(color, 0.7)
    pay_color = hex_to_rgba(color, 0.5)

    ax.barh(y, PREAMBLE, left=start, color=pre_color, edgecolor='none', height=0.3)
    ax.barh(y, HEADER, left=start + PREAMBLE, color=hdr_color, edgecolor='none', height=0.3)
    ax.barh(y, PAYLOAD, left=start + PREAMBLE + HEADER, color=pay_color, edgecolor='none', height=0.3)

    # Text labels on top of each section
    text_y = y + 0.17
    OFFSET_X = 0.01
    ax.text(start + OFFSET_X, text_y, 'Preamble', ha='left', va='bottom', fontsize=7)
    ax.text(start + PREAMBLE + OFFSET_X, text_y, 'Header', ha='left', va='bottom', fontsize=7)
    ax.text(start + PREAMBLE + HEADER + OFFSET_X, text_y, 'Payload', ha='left', va='bottom', fontsize=7)


def make_timing_diagram(offset_s, title, filename, outcome_node1, outcome_node2):
    fig, ax = plt.subplots(1, 1, figsize=(8, 2.5))

    # Node 1 (0 dB, starts first)
    draw_packet(ax, y=1.0, start=0, outcome=outcome_node1)
    # Node 2 (+12 dB, starts at offset)
    draw_packet(ax, y=0.5, start=offset_s, outcome=outcome_node2)

    # Labels
    ax.set_yticks([0.5, 1.0])
    ax.set_yticklabels(['Node 2\n(+12 dB)', 'Node 1\n(0 dB)'])
    ax.set_xlabel('Time (s)')
    ax.set_title(title)
    ax.set_ylim(0.0, 1.6)

    # x limits with padding
    x_end = max(TOTAL, offset_s + TOTAL) + 0.1
    ax.set_xlim(-0.05, x_end)
    ax.grid(axis='x', alpha=0.3)

    # Legend for outcome colors
    from matplotlib.patches import Patch
    legend_elements = [
        Patch(facecolor=hex_to_rgba(RECEIVED_COLOR, 0.7), label='Received'),
        Patch(facecolor=hex_to_rgba(LOST_COLOR, 0.7), label='Lost'),
    ]
    ax.legend(handles=legend_elements, loc='upper right', fontsize='small', ncol=2)

    fig.tight_layout()
    out_path = f'{OUT_DIR}/{filename}'
    plt.savefig(out_path, dpi=150, bbox_inches='tight')
    print(f'Saved: {out_path}')
    plt.close()


# Scenario 1: Small offset — preambles overlap, both lost
make_timing_diagram(
    offset_s=0.2,
    title='Preamble overlap: both packets lost',
    filename='capture_timing_preamble_overlap.png',
    outcome_node1='fail',
    outcome_node2='fail',
)

# Scenario 2: Medium offset — Node 2's preamble arrives after Node 1's preamble → capture
make_timing_diagram(
    offset_s=0.5,
    title='Capture succeeds: Node 2 arrives after Node 1\'s preamble',
    filename='capture_timing_capture_success.png',
    outcome_node1='fail',
    outcome_node2='success',
)

# Scenario 3: No overlap — both succeed independently
make_timing_diagram(
    offset_s=1.8,
    title='No overlap: both packets received independently',
    filename='capture_timing_no_overlap.png',
    outcome_node1='success',
    outcome_node2='success',
)
