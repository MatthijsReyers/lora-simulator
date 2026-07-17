#!/usr/bin/env python3
import sys, logging, glob, os

sys.path.append('.')

from simulator.environment import simulation_env as sim
from simulator.native_node.stm32_node import STM32Node


logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

if __name__ == '__main__':
    HEADERS_DIR = './examples/native-stm32wl-phy-ping-pong/firmware'
    MAIN_FILE = f'{HEADERS_DIR}/main.c'
    SOURCES = [
        f for f in glob.glob(f'{HEADERS_DIR}/*.c')
        if os.path.basename(f) != 'main.c'
    ]
    
    node1 = STM32Node(MAIN_FILE, extra_source_files=SOURCES, include_dirs=[HEADERS_DIR])
    node2 = STM32Node(MAIN_FILE, extra_source_files=SOURCES, include_dirs=[HEADERS_DIR])
    
    node1.logger.setLevel(logging.DEBUG)
    node1.logger.addHandler(logging.StreamHandler(sys.stdout))

    sim.logger.setLevel(logging.DEBUG)
    sim.logger.addHandler(logging.StreamHandler(sys.stdout))

    sim.run(30)
