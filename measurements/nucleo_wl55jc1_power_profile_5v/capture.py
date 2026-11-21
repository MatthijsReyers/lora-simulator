"""
One capture of the power profile sequence (firmware/main.c): build it, flash the connected
NUCLEO-WL55JC1 boards, and record all Joulescopes (raw .jls) and the UART logs into
measurements/nucleo_wl55jc1_power_profile_5v/<run-name>/.

Every board gets its own BOARD_INDEX (its position in --boards), the firmware shows it as a
number of blips after boot, which is how extract_power_profile.py knows which Joulescope sits on
which board.

Usage (from the lora-simulator checkout, with the simulator's virtualenv python):
    python measurements/nucleo_wl55jc1_power_profile_5v/capture.py run-1
    python measurements/nucleo_wl55jc1_power_profile_5v/capture.py run-2 --boards <stlink serial> ...

Without --boards every connected ST-LINK is used. The firmware is built in an STM32CubeMX project
set up as described in examples/native-stm32-ping-pong/README.md (HW_PROJECT), its Src/main.c is
put back afterwards.
"""
import argparse
import glob
import json
import os
import re
import subprocess
import sys
import termios
import threading
import time

HERE = os.path.dirname(os.path.realpath(__file__))
SIM_REPO = os.path.realpath(os.path.join(HERE, '..', '..'))
FIRMWARE = os.path.join(HERE, 'firmware', 'main.c')
HW_PROJECT = '/home/matt/Memsys/EWS_Firmware/.claude/worktrees/ping-pong'
TOOLCHAIN = ('/opt/stm32cubeide/plugins/com.st.stm32cube.ide.mcu.externaltools.gnu-tools-for-stm32'
             '.14.3.rel1.linux64_1.0.100.202602081740/tools/bin')
UART = '/dev/serial/by-id/usb-STMicroelectronics_STLINK-V3_{}-if01'
SEQUENCE_SECONDS = 172  # boot and 3 cycles of the sequence, with margin


def run(cmd, **kwargs):
    result = subprocess.run(cmd, capture_output=True, text=True, **kwargs)
    if result.returncode != 0:
        sys.exit(f"FAILED: {' '.join(cmd)}\n{result.stdout}\n{result.stderr}")
    return result.stdout


def uart_logger(serial, path, stop):
    """ Write every line the board prints to `path`, prefixed with the host time (unix seconds). """
    fd = os.open(UART.format(serial), os.O_RDONLY | os.O_NOCTTY | os.O_NONBLOCK)
    attrs = termios.tcgetattr(fd)
    attrs[0] = 0                                                # iflag: raw
    attrs[1] = 0                                                # oflag
    attrs[2] = termios.CS8 | termios.CREAD | termios.CLOCAL     # cflag: 8N1
    attrs[3] = 0                                                # lflag
    attrs[4] = attrs[5] = termios.B115200
    termios.tcsetattr(fd, termios.TCSANOW, attrs)
    termios.tcflush(fd, termios.TCIFLUSH)
    buffer = b''
    with open(path, 'w') as out:
        while not stop.is_set():
            try:
                data = os.read(fd, 4096)
            except BlockingIOError:
                data = b''
            if not data:
                time.sleep(0.001)
                continue
            buffer += data
            while b'\n' in buffer:
                line, buffer = buffer.split(b'\n', 1)
                out.write(f"{time.time():.4f} {line.decode('ascii', 'replace').rstrip()}\n")
                out.flush()
    os.close(fd)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('name')
    parser.add_argument('--boards', nargs='+', help='ST-LINK serials, in BOARD_INDEX order (default: all connected)')
    parser.add_argument('-n', '--note', action='append', default=[], help='remark to store in params.json')
    args = parser.parse_args()

    boards = args.boards or sorted(
        re.search(r'STLINK-V3_(\w+)-if01', path).group(1) for path in glob.glob(UART.format('*')))
    assert boards, 'no ST-LINK found'
    run_dir = os.path.join(HERE, args.name)
    os.makedirs(run_dir, exist_ok=False)

    # ---- Build and flash, one binary per board (BOARD_INDEX differs)
    source = open(FIRMWARE).read()
    project_main = os.path.join(HW_PROJECT, 'Src/main.c')
    original_main = open(project_main).read()
    env = dict(os.environ, PATH=TOOLCHAIN + ':' + os.environ['PATH'])
    try:
        for index, serial in enumerate(boards):
            with open(project_main, 'w') as f:
                f.write(re.sub(r'^(#define BOARD_INDEX\s+)\d+', lambda m: m.group(1) + str(index), source, flags=re.M))
            run(['ninja', '-C', 'build/Debug'], cwd=HW_PROJECT, env=env)
            binary = os.path.join(run_dir, f'firmware_{index}.bin')
            run(['arm-none-eabi-objcopy', '-O', 'binary',
                 os.path.join(HW_PROJECT, 'build/Debug/I2C_TwoBoards_ComIT.elf'), binary], env=env)
            run(['st-flash', '--serial', serial, '--connect-under-reset', 'write', binary, '0x08000000'])
    finally:
        with open(project_main, 'w') as f:
            f.write(original_main)
    with open(os.path.join(run_dir, 'main.c'), 'w') as f:
        f.write(source)
    print('flashed', boards, flush=True)

    # ---- Capture
    stop = threading.Event()
    threads = []
    for serial in boards:
        thread = threading.Thread(target=uart_logger, args=(serial, os.path.join(run_dir, f'uart_{serial}.log'), stop))
        thread.start()
        threads.append(thread)

    stats_csv = os.path.join(run_dir, 'joulescope_stats.csv')
    capture_start = time.time()
    capture = subprocess.Popen(
        [sys.executable, os.path.join(SIM_REPO, 'measurements/joulescope_dump.py'), '-d', str(SEQUENCE_SECONDS + 6),
         '-r', '100', '-o', stats_csv, '--jls', os.path.join(run_dir, 'joulescope.jls')],
        stderr=subprocess.PIPE, text=True,
    )
    # Wait until the Joulescopes deliver data before starting the boards.
    while capture.poll() is None:
        if os.path.exists(stats_csv) and os.path.getsize(stats_csv) > 2000:
            break
        time.sleep(0.05)
    time.sleep(1.0)

    # Reset all boards at (nearly) the same moment: they transmit together and receive together,
    # so no board receives while another one transmits.
    resets = [subprocess.Popen(['st-flash', '--serial', s, '--connect-under-reset', 'reset'],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) for s in boards]
    reset_time = time.time()
    problems = ['st-flash reset failed' for reset in resets if reset.wait() != 0]
    print('boards reset, capturing', flush=True)

    capture_stderr = capture.communicate()[1]
    if capture.returncode != 0:
        problems.append(f'joulescope_dump.py failed: {capture_stderr[-500:]}')
    stop.set()
    for thread in threads:
        thread.join()

    params = {
        'run': args.name,
        'recorded': time.strftime('%Y-%m-%d %H:%M', time.localtime(capture_start)),
        'boards_stlink_serial': boards,  # position = BOARD_INDEX
        'firmware_ids': {s: (re.findall(r'\[([0-9A-F]{4})\]', open(os.path.join(run_dir, f'uart_{s}.log')).read()) or [None])[-1]
                         for s in boards},
        'host_time_capture_start': capture_start,
        'host_time_boards_reset': reset_time,
        'joulescope_measures': 'whole board at its 5 V supply',
        'notes': args.note,
        'problems': problems,
    }
    with open(os.path.join(run_dir, 'params.json'), 'w') as f:
        json.dump(params, f, indent=2)
    print('done', run_dir, problems)


if __name__ == '__main__':
    main()
