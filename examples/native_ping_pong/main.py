#!/usr/bin/env python3
import sys, logging, glob, os

sys.path.append('.')

from simulator.environment import simulation_env as sim
from simulator.native_node.radio_node import RadioNode

if __name__ == '__main__':
    HEADERS_DIR = './examples/native_ping_pong/firmware'
    MAIN_FILE = f'{HEADERS_DIR}/main.c'
    SOURCES = [
        f for f in glob.glob(f'{HEADERS_DIR}/*.c')
        if os.path.basename(f) != 'main.c'
    ]
    
    node1 = RadioNode(MAIN_FILE, extra_source_files=SOURCES, include_dirs=[HEADERS_DIR])
    node2 = RadioNode(MAIN_FILE, extra_source_files=SOURCES, include_dirs=[HEADERS_DIR])

    # Uncomment to trace radio method calls
    # ===================================================================
    # node1.logger.addHandler(logging.StreamHandler(sys.stdout))
    # node1.logger.setLevel(logging.DEBUG)
    # node1.radio.logger.addHandler(logging.StreamHandler(sys.stdout))
    # node1.radio.logger.setLevel(logging.DEBUG)

    # node2.logger.addHandler(logging.StreamHandler(sys.stdout))
    # node2.logger.setLevel(logging.DEBUG)
    # node2.radio.logger.addHandler(logging.StreamHandler(sys.stdout))
    # node2.radio.logger.setLevel(logging.DEBUG)

    # sim.logger.addHandler(logging.StreamHandler(sys.stdout))
    # sim.logger.setLevel(logging.DEBUG)

    sim.run(20)

    print(node1.radio.packets_log)
    print(node2.radio.packets_log)
