#!/usr/bin/env python3
import sys

sys.path.append('.')

from simulator.environment import simulation_env as sim
from simulator.native_node.stm32_node import STM32Node

if __name__ == '__main__':
    FIRMWARE_DIR = './examples/native-stm32-ping-pong/firmware'
    MAIN_FILE = f'{FIRMWARE_DIR}/main.c'

    # Two boards running the same firmware, 100 meters apart.
    node1 = STM32Node(MAIN_FILE, include_dirs=[FIRMWARE_DIR], position=(0.0, 0.0))
    node2 = STM32Node(MAIN_FILE, include_dirs=[FIRMWARE_DIR], position=(100.0, 0.0))

    # Uncomment to trace the radio calls made by the firmware of the first node
    # ===================================================================
    # import logging
    # node1.logger.addHandler(logging.StreamHandler(sys.stdout))
    # node1.logger.setLevel(logging.DEBUG)

    sim.run(30)

    print(node1.radio.packets_log)
    print(node2.radio.packets_log)
