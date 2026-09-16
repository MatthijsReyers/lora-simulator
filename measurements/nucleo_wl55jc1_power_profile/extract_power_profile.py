"""
Extract Stm32wl55PowerProfile model values from a Joulescope capture of the
gateway-radio-firmware power measurement sequence.

The firmware (gateway-radio-firmware Core/main.cpp) runs this sequence:

    boot baseline -> lora::initialize() -> standby hold (3s)
    then repeating cycles of:
      t0+0.0    sync marker: 3 x (100ms RX + 100ms standby)
      t0+0.6    sleep hold (4s)                     -> disabled_power()
      t0+4.6    3 x (standby 500ms + sleep 500ms)   -> standby_startup_time()
      t0+7.6    standby hold (3s)                   -> standby_power()
      t0+10.6   RX 2s each at BW 125/250/500 kHz    -> rx_power()
      t0+16.6   23 TX pulses 0..22 dBm, 500ms gaps  -> tx_power(), tx_startup_time()
      standby hold (3s), next cycle

Two JS220s are expected in the capture: the "board" channel (JP1 / VDD_MCU, mW scale) and an
auxiliary channel (e.g. JP9 / VDD_APP, µW scale). The board channel is auto-detected as the one
with the higher median power.

Input formats:
    capture.jls        raw full-rate Joulescope recording (requires pyjls)
    capture.power.npz  reduced archive created with --pack: board power at full rate plus the
                       auxiliary power averaged down to ~1 kHz. Raw .jls files are gitignored
                       (too large for GitHub), the .power.npz is what gets committed. The
                       reduction is lossless for every value this script extracts: all edge
                       timing uses the board channel, the aux channel is only used for
                       state medians.

Usage:
    python extract_power_profile.py [capture.jls|capture.power.npz] [-o OUTPUT_DIR]
    python extract_power_profile.py capture.jls --pack     # create capture.power.npz

Outputs (in OUTPUT_DIR, default: alongside the capture):
    power_profile.json     all extracted values
    profile_snippet.py     paste-ready values for simulator/lora/radio_power_profile.py
    overview.png           full trace with detected phase boundaries
    tx_sweep.png           TX plateaus vs dBm and pulse duration vs dBm
    edges.png              zooms: sleep->standby edge and a TX pulse rising edge

Dependencies: numpy, matplotlib (pyjls only when reading raw .jls)
"""
import argparse
import json
import math
from pathlib import Path

import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

# --------------------------------------------------------------------------- firmware timing
# These must match the constants in gateway-radio-firmware Core/main.cpp.
MARKER_S = 0.6          # 3 x (100ms RX + 100ms standby)
SLEEP_HOLD_S = 4.0
TOGGLE_S = 3.0          # 3 x (500ms standby + 500ms sleep)
STANDBY_HOLD_S = 3.0
RX_HOLD_S = 2.0
RX_BANDWIDTHS = ['125', '250', '500']
TX_GAP_S = 0.5
TX_POWERS = list(range(0, 23))

# TX packet parameters (tx_pulse() in main.cpp): SF7, BW125, CR4/5, preamble 8, 64 byte
# payload, CRC on, explicit header.
SF, BW_HZ, CR, PREAMBLE, PAYLOAD, CRC_ON = 7, 125000, 1, 8, 64, True

# --------------------------------------------------------------------------- level thresholds
INIT_EDGE_W = 0.035     # board power above this (sustained) = radio initialized
RX_LEVEL_W = 0.052      # board power above this = RX (or more)
TX_LEVEL_W = 0.065      # board power above this = TX pulse

AUX_DECIMATE = 500      # aux channel reduction factor used by --pack


def lora_airtime_s() -> float:
    """Semtech LoRa airtime formula for the firmware's TX test packet."""
    t_sym = (2 ** SF) / BW_HZ
    n = 8 * PAYLOAD - 4 * SF + 28 + (16 if CRC_ON else 0)
    n_payload = 8 + max(math.ceil(n / (4 * SF)) * (CR + 4), 0)
    return (PREAMBLE + 4.25) * t_sym + n_payload * t_sym


def load_jls(path: Path) -> dict:
    """Load both power channels at full rate from a raw Joulescope recording."""
    from pyjls import Reader
    r = Reader(str(path))
    powers = []
    for sid, s in r.signals.items():
        if s.name == 'power' and s.sample_rate > 0:
            data = r.fsr(sid, 0, s.length).astype(np.float32)
            powers.append((r.sources[s.source_id].name, float(s.sample_rate), data))
    assert len(powers) >= 1, 'no power signals found in capture'
    powers.sort(key=lambda x: -float(np.median(x[2])))  # board channel first
    board_name, board_fs, board = powers[0]
    out = {'board': board, 'board_fs': board_fs, 'board_name': board_name,
           'aux': None, 'aux_fs': None, 'aux_name': None}
    if len(powers) > 1:
        out.update(aux=powers[1][2], aux_fs=powers[1][1], aux_name=powers[1][0])
    return out


def load_npz(path: Path) -> dict:
    z = np.load(path, allow_pickle=False)
    out = {'board': z['board_power'], 'board_fs': float(z['board_fs']),
           'board_name': str(z['board_name']),
           'aux': None, 'aux_fs': None, 'aux_name': None}
    if 'aux_power' in z:
        out.update(aux=z['aux_power'], aux_fs=float(z['aux_fs']),
                   aux_name=str(z['aux_name']))
    return out


def load_power_channels(path: Path) -> dict:
    return load_npz(path) if path.suffix == '.npz' else load_jls(path)


def pack(jls_path: Path) -> Path:
    """Reduce a raw .jls to a committable .power.npz (see module docstring)."""
    d = load_jls(jls_path)
    out_path = jls_path.parent / (jls_path.stem + '.power.npz')
    arrays = {
        'board_power': d['board'], 'board_fs': np.float64(d['board_fs']),
        'board_name': d['board_name'],
    }
    if d['aux'] is not None:
        n = (len(d['aux']) // AUX_DECIMATE) * AUX_DECIMATE
        arrays['aux_power'] = d['aux'][:n].reshape(-1, AUX_DECIMATE).mean(
            axis=1).astype(np.float32)
        arrays['aux_fs'] = np.float64(d['aux_fs'] / AUX_DECIMATE)
        arrays['aux_name'] = d['aux_name']
    np.savez_compressed(out_path, **arrays)
    print(f'packed {jls_path.name} ({jls_path.stat().st_size/1e6:.0f} MB) -> '
          f'{out_path.name} ({out_path.stat().st_size/1e6:.0f} MB)')
    return out_path


def find_events(mask: np.ndarray, fs: float, merge_s: float, min_s: float):
    """Contiguous True-regions of mask as (t_start, t_end), merged and filtered."""
    d = np.diff(mask.astype(np.int8))
    rises = (np.flatnonzero(d == 1) + 1) / fs
    falls = (np.flatnonzero(d == -1) + 1) / fs
    if mask[0]:
        rises = np.r_[0.0, rises]
    if mask[-1]:
        falls = np.r_[falls, len(mask) / fs]
    events = []
    for s, e in zip(rises, falls):
        if events and s - events[-1][1] < merge_s:
            events[-1][1] = e
        else:
            events.append([s, e])
    return [(s, e) for s, e in events if e - s > min_s]


def find_markers(events):
    """Starts of 3-blip sync markers among RX-level events."""
    markers = []
    i = 0
    while i < len(events) - 2:
        widths = [events[i + k][1] - events[i + k][0] for k in range(3)]
        gaps = [events[i + k + 1][0] - events[i + k][1] for k in range(2)]
        if all(0.05 < w < 0.16 for w in widths) and all(g < 0.16 for g in gaps):
            markers.append(events[i][0])
            i += 3
        else:
            i += 1
    return markers


def seg_median(p: np.ndarray, fs: float, t0: float, t1: float, trim: float = 0.1) -> float:
    """Median power over [t0, t1], trimming a fraction at both ends."""
    d = t1 - t0
    i0, i1 = int((t0 + trim * d) * fs), int((t1 - trim * d) * fs)
    return float(np.median(p[i0:i1]))


def edge_time(p: np.ndarray, fs: float, t_lo: float, t_hi: float,
              lo: float, hi: float, smooth_s: float = 0.002):
    """(t_cross, rise_10_90_s) of a rising edge from level lo to hi inside [t_lo, t_hi]."""
    i0, i1 = int(t_lo * fs), int(t_hi * fs)
    k = max(int(smooth_s * fs), 1)
    sm = np.convolve(p[i0:i1], np.ones(k) / k, mode='same')
    span = hi - lo
    above = sm > lo + 0.5 * span
    if not above.any():
        return None, None
    cross = int(np.argmax(above))
    j = cross
    while j > 0 and sm[j] > lo + 0.1 * span:
        j -= 1
    j2 = cross
    while j2 < len(sm) - 1 and sm[j2] < lo + 0.9 * span:
        j2 += 1
    return (i0 + cross) / fs, (j2 - j) / fs


def main():
    ap = argparse.ArgumentParser(description='Extract Stm32wl55PowerProfile values')
    ap.add_argument('capture', nargs='?', default=None,
                    help='.jls or .power.npz capture (default: newest under this directory)')
    ap.add_argument('-o', '--out', default=None, help='output directory')
    ap.add_argument('--pack', action='store_true',
                    help='only reduce the given .jls to a committable .power.npz and exit')
    args = ap.parse_args()

    here = Path(__file__).parent
    if args.capture:
        cap_path = Path(args.capture)
    else:
        candidates = [p for p in here.rglob('*.jls') if not p.name.endswith('.anno.jls')]
        candidates += list(here.rglob('*.power.npz'))
        assert candidates, 'no .jls or .power.npz capture found'
        cap_path = max(candidates, key=lambda p: p.stat().st_mtime)

    if args.pack:
        assert cap_path.suffix == '.jls', '--pack takes a raw .jls capture'
        pack(cap_path)
        return

    out_dir = Path(args.out) if args.out else cap_path.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f'capture: {cap_path}')

    d = load_power_channels(cap_path)
    fs, p1 = d['board_fs'], d['board']
    aux_fs, p2 = d['aux_fs'], d['aux']
    board_name, aux_name = d['board_name'], d['aux_name']
    t = np.arange(len(p1)) / fs
    print(f'board channel: {board_name} @ {fs:.0f} Hz, aux channel: {aux_name} '
          f'@ {aux_fs or 0:.0f} Hz, {t[-1]:.1f} s')

    airtime = lora_airtime_s()
    print(f'expected TX airtime: {airtime*1000:.3f} ms')

    # ------------------------------------------------------------------ init edge & baseline
    k = max(int(0.010 * fs), 1)
    sustained = np.convolve((p1 > INIT_EDGE_W).astype(np.float32),
                            np.ones(k, dtype=np.float32) / k, mode='same') > 0.9
    t_init = int(np.argmax(sustained)) / fs
    baseline = seg_median(p1, fs, 0.1, t_init - 0.05)
    post_init = seg_median(p1, fs, t_init + 0.2, t_init + STANDBY_HOLD_S - 0.2)

    # ------------------------------------------------------------------ sync markers
    rx_events = find_events(p1 > RX_LEVEL_W, fs, merge_s=0.020, min_s=0.030)
    markers = find_markers(rx_events)
    print(f'init edge: t={t_init:.3f}s, markers: {[f"{m:.2f}" for m in markers]}')
    assert markers, 'no sync marker found in capture'
    t0 = markers[0]

    # ------------------------------------------------------------------ steady phases
    t_sleep0 = t0 + MARKER_S
    t_tog0 = t_sleep0 + SLEEP_HOLD_S
    t_stby0 = t_tog0 + TOGGLE_S
    t_rx0 = t_stby0 + STANDBY_HOLD_S
    sleep_p = seg_median(p1, fs, t_sleep0 + 0.1, t_tog0 - 0.1)
    standby_p = seg_median(p1, fs, t_stby0 + 0.1, t_rx0 - 0.1)
    rx_p = {bw: seg_median(p1, fs, t_rx0 + i * RX_HOLD_S + 0.2, t_rx0 + (i + 1) * RX_HOLD_S - 0.2)
            for i, bw in enumerate(RX_BANDWIDTHS)}

    aux = {}
    if p2 is not None:
        aux = {
            'baseline_uW': seg_median(p2, aux_fs, 0.1, t_init - 0.05) * 1e6,
            'post_init_uW': seg_median(p2, aux_fs, t_init + 0.2,
                                       t_init + STANDBY_HOLD_S - 0.2) * 1e6,
            'sleep_uW': seg_median(p2, aux_fs, t_sleep0 + 0.1, t_tog0 - 0.1) * 1e6,
            'standby_uW': seg_median(p2, aux_fs, t_stby0 + 0.1, t_rx0 - 0.1) * 1e6,
            'rx_uW': {bw: seg_median(p2, aux_fs, t_rx0 + i * RX_HOLD_S + 0.2,
                                     t_rx0 + (i + 1) * RX_HOLD_S - 0.2) * 1e6
                      for i, bw in enumerate(RX_BANDWIDTHS)},
        }

    # ------------------------------------------------------------------ sleep->standby edges
    toggles = []
    for i in range(3):
        te, rise = edge_time(p1, fs, t_tog0 + i * 1.0 - 0.1, t_tog0 + i * 1.0 + 0.4,
                             sleep_p, standby_p)
        if te is not None:
            toggles.append({'t': te, 'rise_10_90_ms': rise * 1000})

    # ------------------------------------------------------------------ TX pulses
    t_tx0 = t_rx0 + len(RX_BANDWIDTHS) * RX_HOLD_S - 0.2
    t_tx1 = min(t_tx0 + len(TX_POWERS) * (TX_GAP_S + airtime + 0.05) + 1.0, t[-1])
    i0, i1 = int(t_tx0 * fs), int(t_tx1 * fs)
    pulses = find_events(p1[i0:i1] > TX_LEVEL_W, fs, merge_s=0.010, min_s=0.050)
    pulses = [(s + t_tx0, e + t_tx0) for s, e in pulses][:len(TX_POWERS)]
    print(f'TX pulses found: {len(pulses)}')
    assert len(pulses) == len(TX_POWERS), \
        f'expected {len(TX_POWERS)} TX pulses, found {len(pulses)}'

    tx = []
    for dbm, (ts, te) in zip(TX_POWERS, pulses):
        s, e = int(ts * fs), int(te * fs)
        w = e - s
        plateau = float(np.median(p1[s + int(0.2 * w):e - int(0.2 * w)]))
        # refine edges: 50% crossing between the standby floor and the plateau
        thresh = standby_p + 0.5 * (plateau - standby_p)
        m = int(0.005 * fs)
        s2 = s - m + int(np.argmax(p1[s - m:s + m] > thresh))
        e2 = e - m + int(np.argmax(p1[e - m:e + m] < thresh))
        entry = {
            'dbm': dbm,
            'plateau_W': plateau,
            'duration_ms': (e2 - s2) / fs * 1000,
            'startup_ms': ((e2 - s2) / fs - airtime) * 1000,
            't_start': s2 / fs,
        }
        if p2 is not None:
            entry['aux_uW'] = float(np.median(
                p2[int(s2 / fs * aux_fs):int(e2 / fs * aux_fs)])) * 1e6
        tx.append(entry)

    # ------------------------------------------------------------------ report
    result = {
        'capture': cap_path.name,
        'sample_rate_hz': fs,
        'board_channel': board_name,
        'aux_channel': aux_name,
        'expected_airtime_ms': airtime * 1000,
        'baseline_W': baseline,
        'post_init_W': post_init,
        'init_uptick_W': post_init - baseline,
        'sleep_W': sleep_p,
        'standby_W': standby_p,
        'rx_W': rx_p,
        'sleep_to_standby_edges': toggles,
        'tx': tx,
        'aux': aux,
    }
    with open(out_dir / 'power_profile.json', 'w') as f:
        json.dump(result, f, indent=2)

    with open(out_dir / 'profile_snippet.py', 'w') as f:
        f.write('# Extracted from %s (%s), board channel %s.\n'
                '# All power values in watts. Radio-only: the MCU busy-wait baseline of\n'
                '# %r W (measured before radio init) has been subtracted, so these values\n'
                '# represent only the radio part of the MCU (including the persistent\n'
                '# SUBGHZ-subsystem overhead that appears at radio init). The MCU itself\n'
                '# must be modeled separately.\n'
                % (cap_path.name, f'{fs:.0f} Hz', board_name, baseline))
        f.write('_SLEEP_POWER_USAGE = %r\n' % (sleep_p - baseline))
        f.write('_STANDBY_POWER_USAGE = %r\n' % (standby_p - baseline))
        f.write('_RX_POWER_USAGE = {  # by bandwidth [kHz]\n')
        for bw, v in rx_p.items():
            f.write('    %s: %r,\n' % (bw, v - baseline))
        f.write('}\n_TX_POWER_USAGE = {  # by TX power [dBm]\n')
        for e in tx:
            f.write('    %d: %r,\n' % (e['dbm'], e['plateau_W'] - baseline))
        f.write('}\n_TX_STARTUP_TIME = {  # by TX power [dBm], seconds\n')
        for e in tx:
            f.write('    %d: %r,\n' % (e['dbm'], e['startup_ms'] / 1000))
        f.write('}\n')

    # ------------------------------------------------------------------ plots
    ds = max(int(fs / 2000), 1)  # ~2 kHz for plotting
    td, pd_ = t[::ds], p1[::ds]

    n_ax = 3 if p2 is not None else 2
    fig, axes = plt.subplots(n_ax, 1, figsize=(16, 10), sharex=True)
    axes[0].plot(td, pd_ * 1000, lw=0.6)
    axes[0].set_ylabel(f'{board_name} [mW]')
    axes[1].plot(td, pd_ * 1000, lw=0.6)
    axes[1].set_ylim(min(baseline, sleep_p) * 1000 - 5, max(rx_p.values()) * 1000 + 10)
    axes[1].set_ylabel(f'{board_name} zoom [mW]')
    for ax in axes[:2]:
        ax.axvline(t_init, color='k', ls=':', lw=1)
        for m in markers:
            ax.axvline(m, color='tab:red', ls=':', lw=1)
        for x in (t_sleep0, t_tog0, t_stby0, t_rx0):
            ax.axvline(x, color='tab:green', ls=':', lw=0.8)
        ax.grid(alpha=0.3)
    axes[1].text(t_init, axes[1].get_ylim()[1], ' init', va='top')
    if p2 is not None:
        ds2 = max(int(aux_fs / 2000), 1)
        t2 = np.arange(len(p2)) / aux_fs
        axes[2].plot(t2[::ds2], p2[::ds2] * 1e6, lw=0.6, color='tab:orange')
        axes[2].set_ylabel(f'{aux_name} [µW]')
        axes[2].grid(alpha=0.3)
    axes[-1].set_xlabel('time [s]')
    fig.tight_layout()
    fig.savefig(out_dir / 'overview.png', dpi=110)

    fig, (a1, a2) = plt.subplots(1, 2, figsize=(14, 5))
    dbms = [e['dbm'] for e in tx]
    a1.plot(dbms, [e['plateau_W'] * 1000 for e in tx], 'o-')
    a1.set_xlabel('TX power [dBm]')
    a1.set_ylabel('measured power [mW]')
    a1.set_title('TX plateau power (LP PA 0-15, HP PA 16-22)')
    a1.grid(alpha=0.3)
    a2.plot(dbms, [e['duration_ms'] for e in tx], 'o-')
    a2.axhline(airtime * 1000, color='k', ls='--', lw=1, label='expected airtime')
    a2.set_xlabel('TX power [dBm]')
    a2.set_ylabel('pulse duration [ms]')
    a2.set_title('TX pulse duration')
    a2.legend()
    a2.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(out_dir / 'tx_sweep.png', dpi=110)

    fig, (a1, a2) = plt.subplots(1, 2, figsize=(14, 5))
    if toggles:
        tc = toggles[0]['t']
        i0, i1 = int((tc - 0.05) * fs), int((tc + 0.10) * fs)
        a1.plot((t[i0:i1] - tc) * 1000, p1[i0:i1] * 1000, lw=0.5)
        a1.set_title('sleep -> standby edge')
        a1.set_xlabel('time from edge [ms]')
        a1.set_ylabel('[mW]')
        a1.grid(alpha=0.3)
    ts = tx[-1]['t_start']
    i0, i1 = int((ts - 0.002) * fs), int((ts + 0.004) * fs)
    a2.plot((t[i0:i1] - ts) * 1000, p1[i0:i1] * 1000, lw=0.7)
    a2.set_title(f'TX rising edge at {tx[-1]["dbm"]} dBm')
    a2.set_xlabel('time from 50% crossing [ms]')
    a2.set_ylabel('[mW]')
    a2.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(out_dir / 'edges.png', dpi=110)

    # ------------------------------------------------------------------ console summary
    print(f'\nbaseline:   {baseline*1000:8.3f} mW   (MCU busy-wait, radio untouched)')
    print(f'post-init:  {post_init*1000:8.3f} mW   (uptick {(post_init-baseline)*1000:+.3f} mW)')
    print(f'sleep:      {sleep_p*1000:8.3f} mW')
    print(f'standby:    {standby_p*1000:8.3f} mW   (radio {(standby_p-sleep_p)*1000:+.3f} mW vs sleep)')
    for bw, v in rx_p.items():
        print(f'rx {bw:>3} kHz: {v*1000:8.3f} mW')
    for e in tx:
        print(f'tx {e["dbm"]:2d} dBm: {e["plateau_W"]*1000:8.3f} mW  '
              f'dur {e["duration_ms"]:8.3f} ms  startup {e["startup_ms"]*1000:7.1f} µs')
    for tog in toggles:
        print(f'sleep->standby rise (10-90%): {tog["rise_10_90_ms"]:.3f} ms at t={tog["t"]:.3f}')
    print(f'\noutputs written to {out_dir}/')


if __name__ == '__main__':
    main()
