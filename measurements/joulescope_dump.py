"""
Dump the output of all connected Joulescopes (JS220) as CSV.

Every row is the average over one statistics window (default 100 ms) of one Joulescope, computed
on the instrument itself from the full 1 MHz sample stream:

    time_s, device, current_A, voltage_V, power_W, current_min_A, current_max_A, energy_J, charge_C

time_s is the end of the window in seconds since the start of the capture, on one clock for all
Joulescopes. energy_J and charge_C count up from zero at the start of the capture. With --jls the raw samples are also recorded to a .jls file that can be opened in
the Joulescope UI.

Usage:
    python measurements/joulescope_dump.py                       # print until Ctrl-C
    python measurements/joulescope_dump.py -d 30 -o capture.csv  # 30 seconds to a file
    python measurements/joulescope_dump.py -r 1000 -d 10         # 1 ms windows
    python measurements/joulescope_dump.py -d 30 --jls raw.jls   # also record the raw samples

Dependencies: pyjoulescope_driver
"""
import argparse
import sys
import time

from pyjoulescope_driver import Driver, Record

SAMPLE_RATE = 1_000_000  # Hz, JS220


def main():
    parser = argparse.ArgumentParser(description='Dump the output of all connected Joulescopes as CSV.')
    parser.add_argument('-r', '--rate', type=float, default=10.0, help='rows per second per Joulescope (default 10)')
    parser.add_argument('-d', '--duration', type=float, help='seconds to capture (default: until Ctrl-C)')
    parser.add_argument('-o', '--output', help='CSV file to write (default: stdout)')
    parser.add_argument('--jls', help='also record the raw current and voltage samples to this .jls file')
    args = parser.parse_args()

    out = open(args.output, 'w') if args.output else sys.stdout
    first = {}  # device -> (energy, charge) at the start of its first row
    start_utc = []  # Joulescope time64 (2^30 ticks per second) that counts as time_s = 0

    def on_statistics(topic, value):
        device = topic.split('/s/')[0]
        utc = value['time']['utc']['value'][1]  # end of this window, same clock for all devices
        energy = value['accumulators']['energy']['value']
        charge = value['accumulators']['charge']['value']
        if device not in first:
            # The first window still has the ranges settling: drop it and count from its end.
            first[device] = (energy, charge)
            if not start_utc:
                start_utc.append(utc)
            return
        energy0, charge0 = first[device]
        signals = value['signals']
        out.write(
            f"{(utc - start_utc[0]) / 2**30:.4f},{device.split('/')[-1]},"
            f"{signals['current']['avg']['value']:.9g},"
            f"{signals['voltage']['avg']['value']:.6g},"
            f"{signals['power']['avg']['value']:.9g},"
            f"{signals['current']['min']['value']:.9g},"
            f"{signals['current']['max']['value']:.9g},"
            f"{energy - energy0:.9g},{charge - charge0:.9g}\n"
        )

    with Driver() as driver:
        devices = driver.find_devices(None, brand='joulescope')
        if not devices:
            sys.exit('No Joulescopes found')
        print(f"Found {len(devices)} Joulescope(s): {', '.join(str(d) for d in devices)}", file=sys.stderr)

        out.write('time_s,device,current_A,voltage_V,power_W,current_min_A,current_max_A,energy_J,charge_C\n')
        for device in devices:
            driver.open(device)
            driver.publish(f'{device}/s/i/range/mode', 'auto')
            driver.publish(f'{device}/s/v/range/mode', 'auto')
            driver.publish(f'{device}/s/stats/scnt', int(round(SAMPLE_RATE / args.rate)))
            driver.publish(f'{device}/s/stats/ctrl', 1)

        recording = None
        if args.jls:
            recording = Record(driver, devices, 'current,voltage')
            recording.open(args.jls)

        # Subscribe to all devices at the same moment so their rows start together.
        for device in devices:
            driver.subscribe(f'{device}/s/stats/value', 'pub', on_statistics)

        try:
            start = time.time()
            while args.duration is None or time.time() - start < args.duration:
                time.sleep(0.05)
        except KeyboardInterrupt:
            pass
        finally:
            for device in devices:
                driver.unsubscribe(f'{device}/s/stats/value', on_statistics)
            if recording:
                recording.close()
            for device in devices:
                driver.close(device)
            out.flush()
            if args.output:
                out.close()


if __name__ == '__main__':
    main()
