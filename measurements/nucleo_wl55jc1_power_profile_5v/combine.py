"""
Average the boards of all runs into the values of NucleoWL55PowerProfile.

    python measurements/nucleo_wl55jc1_power_profile_5v/combine.py

Prints every value as its mean over the boards, with the lowest and highest single measurement.
A board that was measured in more than one run counts once, with the mean of its runs.
"""
import json
import os

HERE = os.path.dirname(os.path.realpath(__file__))
RUNS = ['run-1', 'run-2', 'run-3', 'run-4']
# These draw 32 and 141 mW extra whenever their CPU runs or their radio is awake, see README.md.
EXCLUDED = ['002200355553500B20393256', '002F00383133510836303739']


def load():
    """ {board: [its result in every run it is part of]} """
    boards = {}
    for run in RUNS:
        result = json.load(open(os.path.join(HERE, run, 'power_profile.json')))
        for serial, board in result['boards'].items():
            if serial not in EXCLUDED:
                boards.setdefault(serial, []).append(board)
    return boards


def combine(boards, value):
    """ (mean over the boards, lowest, highest) of value(result of one run of one board). """
    per_board = [[value(run) for run in runs] for runs in boards.values()]
    means = [sum(values) / len(values) for values in per_board]
    everything = [v for values in per_board for v in values]
    return sum(means) / len(means), min(everything), max(everything)


def main():
    boards = load()
    print('boards:', ', '.join(f'{s} ({len(runs)}x)' for s, runs in boards.items()))
    first = next(iter(boards.values()))[0]

    def line(name, value, scale=1e3, unit='mW', digits=3):
        mean, low, high = combine(boards, value)
        print(f'{name:28s} {mean * scale:9.{digits}f} {unit}   ({low * scale:.{digits}f} to {high * scale:.{digits}f})')

    line('board sleep', lambda b: b['level_w']['sleep']['mean'])
    for state in first['delta_w']:
        line(state + ' over sleep', lambda b: b['delta_w'][state]['mean'])
    line('sleep to standby rise', lambda b: b['sleep_to_standby_rise_s']['mean'], 1e6, 'us', 0)
    for sweep in ('tx_middleware', 'tx_ping_pong'):
        for dbm in first[sweep]:
            line(f'{sweep} {dbm} dBm', lambda b: b[sweep][dbm]['delta_w']['mean'])
        for dbm in first[sweep]:
            line(f'{sweep} {dbm} dBm startup', lambda b: b[sweep][dbm]['startup_s']['mean'], 1e6, 'us', 0)


if __name__ == '__main__':
    main()
