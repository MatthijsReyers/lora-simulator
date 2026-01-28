#!/usr/bin/env python3
import sys, logging

sys.path.append('.')

from simulator.environment import simulation_env as sim
from simulator.native_node.stm32_node import STM32Node


logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

if __name__ == '__main__':
    node1 = STM32Node('./examples/native-stm32/main.c')
    
    # node1.logger.setLevel(logging.DEBUG)
    # node1.logger.addHandler(logging.StreamHandler(sys.stdout))

    # sim.logger.setLevel(logging.DEBUG)
    # sim.logger.addHandler(logging.StreamHandler(sys.stdout))

    # node2 = STM32Node('./main.c')
    # node2.load_source('./examples/native-stm32/main.c
    
    sim.run(30)
