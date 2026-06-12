#!/usr/bin/env python3
"""Generate thesis-quality capture effect graphs comparing simulator results
against measurements from Haxhibeqiri et al. (2017)."""
import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np
import os
import sys
sys.path.append('.')
from colors import *

DATA_DIR = './papers/interference-measurements-2017/data'
OUT_DIR = './papers/interference-measurements-2017'


def load_simulation_results(bw, sf, cr, preamble_len, tx_delta):
    sim_results = {'shift_ms': [], 'failures_tx1': [], 'failures_tx2': []}
    seen_offsets: set[int] = set()
    for file in os.listdir(DATA_DIR):
        settings = f'bw{bw}_sf{sf}_cr{cr}_pre{preamble_len}_txd{tx_delta}_'
        if file.endswith('.csv') and file.startswith(settings):
            file_path = os.path.join(DATA_DIR, file)
            offset = int(file.split(f'txd{tx_delta}_')[1].split('.csv')[0].split('_')[0])
            if offset in seen_offsets:
                continue
            seen_offsets.add(offset)
            receiver_packets = file_path.replace('_phy_packets.csv', '_receiver_packets.csv')
            phy_packets_path = file_path.replace('_receiver_packets.csv', '_phy_packets.csv')
            receiver_df = pd.read_csv(receiver_packets)
            phy_df = pd.read_csv(phy_packets_path)
            receiver_df.set_index('id', inplace=True)
            phy_df.set_index('id', inplace=True)
            receiver_df['radio_id'] = phy_df['radio_id']
            rx1 = receiver_df[receiver_df['radio_id'] == 1]
            rx1_total = len(rx1)
            rx1_collisions = len(rx1[rx1['collision'] == True])
            rx2 = receiver_df[receiver_df['radio_id'] == 2]
            rx2_total = len(rx2)
            rx2_collisions = len(rx2[rx2['collision'] == True])
            sim_results['shift_ms'].append(offset)
            sim_results['failures_tx1'].append((rx1_collisions / rx1_total) * 100 if rx1_total else 0)
            sim_results['failures_tx2'].append((rx2_collisions / rx2_total) * 100 if rx2_total else 0)
    df = pd.DataFrame(sim_results)
    df.sort_values('shift_ms', inplace=True)
    df.reset_index(inplace=True, drop=True)
    df['shift_s'] = df['shift_ms'] / 1000
    return df


# --- Paper measurements (from Tables 2-5 of Haxhibeqiri et al. 2017) ---

# Table 2: SF12, preamble=8, equal power
table_2 = pd.DataFrame({
    'shift_ms': [100, 200, 300, 400, 500, 600, 700, 800, 900, 1000, 1100, 1200, 1300, 1400, 1500, 1600],
    'collisions_tx1': [2.2, 4, 3.2, 3, 4.4, 2, 3.4, 2.4, 0.8, 0.8, 2, 2, 1, 2.2, 1, 2],
    'collisions_tx2': [100, 100, 100, 100, 100, 100, 100, 100, 100, 100, 100, 100, 100, 84, 24, 5.2],
    'bad_crc_tx1': [25, 9, 4.4, 1, 7, 4.3, 3.5, 8.4, 4.3, 2.5, 5.4, 3.2, 2.1, 0, 2.1, 4.5],
    'bad_crc_tx2': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 0],
})
table_2['shift_s'] = table_2['shift_ms'] / 1000
table_2['failures_tx1'] = table_2['collisions_tx1'] + table_2['bad_crc_tx1']
table_2['failures_tx2'] = table_2['collisions_tx2'] + table_2['bad_crc_tx2']

# Table 3: SF7, preamble=14, equal power
table_3 = pd.DataFrame({
    'shift_ms': [5, 10, 20, 30, 40, 50, 60, 70],
    'collisions_tx1': [2.3, 2.1, 2.8, 2.5, 2.1, 2, 0, 0],
    'collisions_tx2': [100, 100, 100, 100, 100, 100, 100, 0],
    'bad_crc_tx1': [18, 7, 4, 5.2, 3.1, 2.1, 2, 0],
    'bad_crc_tx2': [0, 0, 0, 0, 0, 0, 0, 0],
})
table_3['shift_s'] = table_3['shift_ms'] / 1000
table_3['failures_tx1'] = table_3['collisions_tx1'] + table_3['bad_crc_tx1']
table_3['failures_tx2'] = table_3['collisions_tx2'] + table_3['bad_crc_tx2']

# Table 4: SF12, preamble=8, 12dB power difference (Node 2 stronger)
table_4 = pd.DataFrame({
    'shift_ms': [100, 200, 300, 400, 500, 600, 700, 800, 900, 1000, 1100, 1200, 1300, 1400, 1500, 1600],
    'collisions_tx1': [100, 100, 100, 100, 62, 2, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
    'collisions_tx2': [100, 100, 100, 0, 50, 100, 100, 100, 100, 100, 100, 100, 100, 48, 0, 0],
    'bad_crc_tx1': [0, 0, 0, 0, 38, 98, 100, 100, 100, 100, 100, 100, 100, 99, 73, 1],
    'bad_crc_tx2': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 2, 0, 0],
})
table_4['shift_s'] = table_4['shift_ms'] / 1000
table_4['failures_tx1'] = table_4['collisions_tx1'] + table_4['bad_crc_tx1']
table_4['failures_tx2'] = table_4['collisions_tx2'] + table_4['bad_crc_tx2']

# Table 5: SF7, preamble=14, 12dB power difference (Node 2 stronger)
table_5 = pd.DataFrame({
    'shift_ms': [5, 10, 20, 30, 40, 50, 60, 70],
    'collisions_tx1': [100, 100, 72, 0, 0, 0, 0, 0],
    'collisions_tx2': [100, 100, 41, 100, 100, 100, 100, 0],
    'bad_crc_tx1': [0, 0, 28, 100, 100, 100, 100, 0],
    'bad_crc_tx2': [0, 0, 0, 0, 0, 0, 0, 0],
})
table_5['shift_s'] = table_5['shift_ms'] / 1000
table_5['failures_tx1'] = table_5['collisions_tx1'] + table_5['bad_crc_tx1']
table_5['failures_tx2'] = table_5['collisions_tx2'] + table_5['bad_crc_tx2']


# --- Load simulation results ---
sim_sf12_eq = load_simulation_results(bw=125, sf=12, cr=8, preamble_len=8, tx_delta=0)
sim_sf12_cap = load_simulation_results(bw=125, sf=12, cr=8, preamble_len=8, tx_delta=12)
sim_sf7_eq = load_simulation_results(bw=125, sf=7, cr=8, preamble_len=14, tx_delta=0)
sim_sf7_cap = load_simulation_results(bw=125, sf=7, cr=8, preamble_len=14, tx_delta=12)


def make_graph(paper_df, sim_df, title, filename, xlabel_unit='s'):
    fig, ax = plt.subplots(1, 1)
    ax.grid(zorder=0)

    # Paper measurements
    ax.scatter(paper_df['shift_s'], paper_df['failures_tx1'],
               marker='o', color=BLUE_DARK, label='Node 1 (measured)', zorder=3, s=40)
    ax.scatter(paper_df['shift_s'], paper_df['failures_tx2'],
               marker='s', color=RED, label='Node 2 (measured)', zorder=3, s=40)

    # Simulation results
    ax.plot(sim_df['shift_s'], sim_df['failures_tx1'],
            color=BLUE_DARK, linewidth=1.5, linestyle='-', label='Node 1 (simulated)', zorder=2)
    ax.plot(sim_df['shift_s'], sim_df['failures_tx2'],
            color=RED, linewidth=1.5, linestyle='-', label='Node 2 (simulated)', zorder=2)

    ax.set_xlabel(f'Collision offset ({xlabel_unit})')
    ax.set_ylabel('Packet failure rate (%)')
    ax.set_ylim(-5, 105)
    ax.set_xlim(0)
    ax.set_title(title)
    ax.legend(loc='center right', fontsize='small')
    fig.set_figwidth(7)
    fig.tight_layout()
    out_path = os.path.join(OUT_DIR, filename)
    plt.savefig(out_path, dpi=150, bbox_inches='tight')
    print(f'Saved: {out_path}')
    plt.close()


# Generate all 4 graphs
make_graph(
    table_2, sim_sf12_eq,
    'Equal power collision (SF12, BW125, preamble=8)',
    'capture_effect_sf12_equal.png'
)

make_graph(
    table_4, sim_sf12_cap,
    'Capture effect: Node 2 is 12 dB stronger (SF12, BW125, preamble=8)',
    'capture_effect_sf12_capture.png'
)

make_graph(
    table_3, sim_sf7_eq,
    'Equal power collision (SF7, BW125, preamble=14)',
    'capture_effect_sf7_equal.png'
)

make_graph(
    table_5, sim_sf7_cap,
    'Capture effect: Node 2 is 12 dB stronger (SF7, BW125, preamble=14)',
    'capture_effect_sf7_capture.png'
)
