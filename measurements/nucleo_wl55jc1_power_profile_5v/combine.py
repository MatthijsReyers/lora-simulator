"""
Average the boards of several runs into the values of NucleoWL55PowerProfile.

    python measurements/nucleo_wl55jc1_power_profile_5v/combine.py

Prints every value as its mean over the boards, with the lowest and highest board.
"""
import json
import os

HERE = os.path.dirname(os.path.realpath(__file__))
RUNS = ['run-1', 'run-2']
# Draws about 32 mW extra whenever its CPU runs or its radio is awake, see README.md.
EXCLUDED = ['002200355553500B20393256']


def main():
    boards = {}
    for run in RUNS:
        result = json.load(open(os.path.join(HERE, run, 'power_profile.json')))
        boards.update({s: b for s, b in result['boards'].items() if s not in EXCLUDED})
    print('boards:', ', '.join(boards))

    def line(name, values, scale=1e3, unit='mW', digits=3):
        mean = sum(values) / len(values)
        print(f'{name:28s} {mean * scale:9.{digits}f} {unit}   ({min(values) * scale:.{digits}f} to {max(values) * scale:.{digits}f})')

    line('board sleep', [b['level_w']['sleep']['mean'] for b in boards.values()])
    for state in next(iter(boards.values()))['delta_w']:
        line(state + ' over sleep', [b['delta_w'][state]['mean'] for b in boards.values()])
    line('sleep to standby rise', [b['sleep_to_standby_rise_s']['mean'] for b in boards.values()], 1e6, 'us', 0)
    for sweep in ('tx_middleware', 'tx_ping_pong'):
        for dbm in next(iter(boards.values()))[sweep]:
            line(f'{sweep} {dbm} dBm', [b[sweep][dbm]['delta_w']['mean'] for b in boards.values()])
        for dbm in next(iter(boards.values()))[sweep]:
            line(f'{sweep} {dbm} dBm startup', [b[sweep][dbm]['startup_s']['mean'] for b in boards.values()], 1e6, 'us', 0)


if __name__ == '__main__':
    main()
