"""
Extract the power levels of every radio state from a capture made by capture.py (the sequence of
firmware/main.c, one Joulescope per board on the board's 5 V supply).

    python measurements/nucleo_wl55jc1_power_profile_5v/extract_power_profile.py run-1

Reads <run>/joulescope.jls and writes into <run>/:
    power_profile.json   per board: the level of every state, per cycle and averaged
    power_1khz.npz       the power of every board averaged down to 1 kHz (small enough to commit)
    overview_<board>.png the trace with the phases that were found

All levels are whole board power in watts at the 5 V supply. "delta" values are a state's level
minus the level with the radio asleep and the CPU stopped: what the radio adds, as seen at 5 V.

Dependencies: numpy, matplotlib, pyjls
"""
import argparse
import json
import math
import os

import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

HERE = os.path.dirname(os.path.realpath(__file__))

# ---- Timing of firmware/main.c, in seconds
MARKER = 0.6
MCU_RUN = 3.0
SLEEP = 4.0
TOGGLES = 3.0
STANDBY_RC = 3.0
STANDBY_XOSC = 2.0
RX_HOLD = 2.0
RX_PHASES = ['rx_bw125', 'rx_bw250', 'rx_bw500', 'rx_sf12_bw125', 'rx_boosted_bw125']
TX_SWEEPS = [('tx_middleware', list(range(0, 23))), ('tx_ping_pong', list(range(0, 15)))]
CYCLES = 3

# TX packet: SF7, 125 kHz, CR 4/5, preamble 8, 64 bytes, CRC on, explicit header
SF, BW_HZ, CR, PREAMBLE, PAYLOAD = 7, 125000, 1, 8, 64

THRESHOLDS = [0.015, 0.035]  # W above the standby level: receiving or transmitting


def lora_airtime() -> float:
    t_sym = (2 ** SF) / BW_HZ
    n_payload = 8 + max(math.ceil((8 * PAYLOAD - 4 * SF + 28 + 16) / (4 * SF)) * (CR + 4), 0)
    return (PREAMBLE + 4.25 + n_payload) * t_sym


def load_power(path):
    """ {joulescope serial: (sample rate, power in W at full rate)} """
    from pyjls import Reader
    reader = Reader(path)
    signals = {}
    for signal in reader.signals.values():
        if signal.name in ('current', 'voltage') and signal.sample_rate > 0:
            signals.setdefault(reader.sources[signal.source_id].name, {})[signal.name] = signal
    for name, pair in sorted(signals.items()):
        n = min(pair['current'].length, pair['voltage'].length)
        power = reader.fsr(pair['current'].signal_id, 0, n).astype(np.float32)
        power *= reader.fsr(pair['voltage'].signal_id, 0, n)
        np.nan_to_num(power, copy=False)
        yield name.split('-')[-1], float(pair['current'].sample_rate), power


def find_events(mask, fs, merge, shortest):
    """ Stretches where mask is true as (start, end) in seconds, gaps under `merge` closed. """
    d = np.diff(mask.astype(np.int8))
    rises = list((np.flatnonzero(d == 1) + 1) / fs)
    falls = list((np.flatnonzero(d == -1) + 1) / fs)
    if mask[0]:
        rises.insert(0, 0.0)
    if mask[-1]:
        falls.append(len(mask) / fs)
    events = []
    for s, e in zip(rises, falls):
        if events and s - events[-1][1] < merge:
            events[-1][1] = e
        else:
            events.append([s, e])
    return [(s, e) for s, e in events if e - s > shortest]


def groups_of_blips(events, width, count=None):
    """ Runs of events about `width` long with gaps about `width`: [(start of first, number)]. """
    groups = []
    i = 0
    while i < len(events):
        n = 0
        while (i + n < len(events) and 0.75 * width < events[i + n][1] - events[i + n][0] < 1.3 * width
               and (n == 0 or events[i + n][0] - events[i + n - 1][1] < 1.4 * width)):
            n += 1
        if n and (count is None or n == count):
            groups.append((events[i][0], n))
        i += max(n, 1)
    return groups


def median(p, fs, t0, t1, trim=0.25):
    return float(np.median(p[int((t0 + trim) * fs):int((t1 - trim) * fs)]))


def crossing(p, fs, t0, t1, level, rising, smooth=20):
    """ Time of the first (rising) or last (falling) crossing of level in [t0, t1], full rate. """
    i0 = max(int(t0 * fs), 0)
    segment = np.convolve(p[i0:int(t1 * fs)], np.ones(smooth) / smooth, mode='same')
    above = segment > level
    index = int(np.argmax(above)) if rising else len(above) - int(np.argmax(above[::-1]))
    return (i0 + index) / fs


def analyse_cycle(p, fs, p1k, events, t0, t_end):
    """ Levels of one cycle that starts with its marker at t0. """
    out = {}
    t = t0 + MARKER
    out['mcu_run'] = median(p, fs, t, t + MCU_RUN)
    t += MCU_RUN
    out['sleep'] = median(p, fs, t, t + SLEEP, trim=0.4)
    t += SLEEP
    toggles_start = t
    t += TOGGLES
    out['standby_rc'] = median(p, fs, t, t + STANDBY_RC, trim=0.4)
    t += STANDBY_RC
    out['standby_xosc'] = median(p, fs, t, t + STANDBY_XOSC, trim=0.4)

    # Sleep to standby edges: 10 to 90 % rise time of the 0.2 ms moving average.
    rises = []
    span = out['standby_rc'] - out['sleep']
    for i in range(3):
        i0 = int((toggles_start + i - 0.1) * fs)
        sm = np.convolve(p[i0:i0 + int(0.4 * fs)], np.ones(200) / 200, mode='same')
        above = sm > out['sleep'] + 0.5 * span
        if not above.any():
            continue
        lo = hi = int(np.argmax(above))
        while lo > 0 and sm[lo] > out['sleep'] + 0.1 * span:
            lo -= 1
        while hi < len(sm) - 1 and sm[hi] < out['sleep'] + 0.9 * span:
            hi += 1
        rises.append((hi - lo) / fs)
    out['sleep_to_standby_rise_s'] = rises

    # Receive phases: the 2 second events of this cycle, in order.
    holds = [(s, e) for s, e in events if t0 < s < t_end and 0.9 * RX_HOLD < e - s < 1.1 * RX_HOLD]
    assert len(holds) == len(RX_PHASES), f'expected {len(RX_PHASES)} receive phases, found {len(holds)}'
    for name, (s, e) in zip(RX_PHASES, holds):
        out[name] = median(p, fs, s, e)

    # Transmit pulses: the events after the last receive phase.
    airtime = lora_airtime()
    pulses = [(s, e) for s, e in events if holds[-1][1] < s < t_end and 0.8 * airtime < e - s < 1.3 * airtime]
    expected = sum(len(powers) for _, powers in TX_SWEEPS)
    assert len(pulses) == expected, f'expected {expected} transmit pulses, found {len(pulses)}'
    pulses = iter(pulses)
    for sweep, powers in TX_SWEEPS:
        out[sweep] = {}
        for dbm in powers:
            s, e = next(pulses)
            plateau = float(np.median(p[int((s + 0.2 * (e - s)) * fs):int((e - 0.2 * (e - s)) * fs)]))
            before = float(np.median(p[int((s - 0.2) * fs):int((s - 0.03) * fs)]))
            half = before + 0.5 * (plateau - before)
            # The coarse event can start early: the radio starts its TCXO before the amplifier.
            start = crossing(p, fs, s - 0.002, s + 0.03, half, rising=True)
            end = crossing(p, fs, e - 0.03, e + 0.002, half, rising=False)
            # Everything the pulse costs over standby, from 30 ms before to 30 ms after it,
            # expressed as time at the plateau level.
            window = p[int((s - 0.03) * fs):int((e + 0.03) * fs)]
            energy = float(np.sum(window - before, dtype=np.float64)) / fs
            out[sweep][dbm] = {
                'plateau': plateau,
                'standby_before': before,
                'duration_s': end - start,
                'startup_s': end - start - airtime,
                'energy_over_standby_j': energy,
                'equivalent_duration_s': energy / (plateau - before),
                't': start,
            }
    return out


def mean_std(values):
    return {'mean': float(np.mean(values)), 'std': float(np.std(values)), 'n': len(values)}


def summarise(cycles):
    """ Average the cycles of one board, levels both absolute and over the sleep level. """
    states = ['mcu_run', 'sleep', 'standby_rc', 'standby_xosc'] + RX_PHASES
    out = {'level_w': {s: mean_std([c[s] for c in cycles]) for s in states},
           'delta_w': {s: mean_std([c[s] - c['sleep'] for c in cycles]) for s in states if s != 'sleep'},
           'over_standby_w': {s: mean_std([c[s] - c['standby_rc'] for c in cycles]) for s in states},
           'sleep_to_standby_rise_s': mean_std([r for c in cycles for r in c['sleep_to_standby_rise_s']])}
    for sweep, powers in TX_SWEEPS:
        out[sweep] = {}
        for dbm in powers:
            out[sweep][dbm] = {
                'level_w': mean_std([c[sweep][dbm]['plateau'] for c in cycles]),
                'delta_w': mean_std([c[sweep][dbm]['plateau'] - c['sleep'] for c in cycles]),
                'over_standby_w': mean_std([c[sweep][dbm]['plateau'] - c['standby_rc'] for c in cycles]),
                'startup_s': mean_std([c[sweep][dbm]['startup_s'] for c in cycles]),
                'equivalent_startup_s': mean_std([c[sweep][dbm]['equivalent_duration_s'] - lora_airtime() for c in cycles]),
            }
    return out


def plot_overview(path, title, p1k, t0s, cycles):
    t = np.arange(len(p1k)) / 1000
    fig, axes = plt.subplots(2, 1, figsize=(16, 8), sharex=True)
    for ax in axes:
        ax.plot(t, p1k * 1000, lw=0.6)
        for t0 in t0s:
            ax.axvline(t0, color='tab:red', ls=':', lw=1)
        ax.set_ylabel('power at 5 V [mW]')
        ax.grid(alpha=0.3)
    floor = min(c['sleep'] for c in cycles) * 1000
    axes[1].set_ylim(floor - 3, max(c['rx_boosted_bw125'] for c in cycles) * 1000 + 8)
    axes[0].set_title(title, loc='left')
    axes[1].set_xlabel('time [s], red: start of a cycle')
    fig.tight_layout()
    fig.savefig(path, dpi=110)
    plt.close(fig)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('run')
    args = parser.parse_args()
    run_dir = os.path.join(HERE, args.run)
    params = json.load(open(os.path.join(run_dir, 'params.json')))
    serials = params['boards_stlink_serial']

    result = {'run': args.run, 'airtime_s': lora_airtime(), 'boards': {}}
    reduced = {}
    for joulescope, fs, p in load_power(os.path.join(run_dir, 'joulescope.jls')):
        n = len(p) // 1000 * 1000
        p1k = p[:n].reshape(-1, int(fs / 1000)).mean(axis=1) if fs == 1e6 else None
        assert p1k is not None, f'unexpected sample rate {fs}'
        reduced[joulescope] = p1k
        # The level the board is at most of the time is standby (the gaps between the pulses).
        counts, edges = np.histogram(p1k, bins=np.arange(0, 0.2, 0.0005))
        standby = float(edges[np.argmax(counts)])
        # Receiving is 27 mW over standby on most boards, but a board that draws more in every
        # state also has its TCXO standby further up: try a higher threshold when the phases
        # do not come out.
        for threshold in THRESHOLDS:
            try:
                events = find_events(p1k > standby + threshold, 1000, merge=0.005, shortest=0.03)
                # The board index blips of the last boot in the capture, then one marker per cycle.
                boot_time, blips = groups_of_blips(events, 0.2)[-1]
                markers = [t for t, _ in groups_of_blips([e for e in events if e[0] > boot_time + 0.4 * blips], 0.1, count=3)]
                assert len(markers) == CYCLES, f'{joulescope}: expected {CYCLES} markers, found {len(markers)}'
                ends = markers[1:] + [len(p) / fs]
                cycles = [analyse_cycle(p, fs, p1k, events, t0, t_end) for t0, t_end in zip(markers, ends)]
                break
            except (AssertionError, IndexError):
                if threshold == THRESHOLDS[-1]:
                    raise

        serial = serials[blips - 1]
        board = {
            'joulescope': joulescope,
            'stlink_serial': serial,
            'firmware_id': params['firmware_ids'].get(serial),
            'board_index': blips - 1,
            'cycle_starts_s': markers,
            **summarise(cycles),
            'cycles': cycles,
        }
        result['boards'][serial] = board
        plot_overview(os.path.join(run_dir, f'overview_{serial}.png'),
                      f'{args.run}: board {serial} (Joulescope {joulescope})', p1k, markers, cycles)
        print(f"\nboard {serial} (id {board['firmware_id']}, Joulescope {joulescope}), power at 5 V in mW")
        for state, value in board['level_w'].items():
            delta = board['delta_w'].get(state, {'mean': 0.0, 'std': 0.0})
            print(f"  {state:18s} {value['mean'] * 1e3:8.3f}   over sleep {delta['mean'] * 1e3:8.3f} +- {delta['std'] * 1e3:.3f}"
                  f"   over standby {board['over_standby_w'][state]['mean'] * 1e3:8.3f}")
        print(f"  sleep to standby rise {board['sleep_to_standby_rise_s']['mean'] * 1e3:.3f} ms")
        for sweep, _ in TX_SWEEPS:
            for dbm, value in board[sweep].items():
                print(f"  {sweep} {dbm:2d} dBm {value['level_w']['mean'] * 1e3:8.2f}   over sleep "
                      f"{value['delta_w']['mean'] * 1e3:8.2f} +- {value['delta_w']['std'] * 1e3:.2f}"
                      f"   over standby {value['over_standby_w']['mean'] * 1e3:8.2f}"
                      f"   startup {value['startup_s']['mean'] * 1e6:7.0f} us"
                      f"   by energy {value['equivalent_startup_s']['mean'] * 1e6:7.0f} us")
        del p

    with open(os.path.join(run_dir, 'power_profile.json'), 'w') as f:
        json.dump(result, f, indent=1)
    np.savez_compressed(os.path.join(run_dir, 'power_1khz.npz'), **{f'js{k}': v for k, v in reduced.items()})
    print(f'\nwrote {run_dir}/power_profile.json')


if __name__ == '__main__':
    main()
