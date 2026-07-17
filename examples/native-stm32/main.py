#!/usr/bin/env python3
import sys, logging, os, glob

sys.path.append('.')

from simulator.environment import simulation_env as sim
from simulator.native_node.stm32_node import STM32Node
from simulator.lora.radio import LoraRadio


logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)


async def send_task(radio: LoraRadio):
    radio.set_tx_config(power=14, code_rate=5)
    radio.set_rx_config(code_rate=5)
    await sim.sleep(1)
    await radio.transmit_data_blocking(b'Hello, world!')


if __name__ == '__main__':
    HEADERS_DIR = './examples/native-stm32/firmware'
    MAIN_FILE = f'{HEADERS_DIR}/main.c'
    SOURCES = [
        f for f in glob.glob(f'{HEADERS_DIR}/*.c')
        if os.path.basename(f) != 'main.c'
    ]
    
    node1 = STM32Node(MAIN_FILE, extra_source_files=SOURCES, include_dirs=[HEADERS_DIR])

    radio = LoraRadio() # Radio must be initialized before node is started.
    sim.create_task(send_task(radio), 'HelloWorldTask')

    # node1.logger.setLevel(logging.DEBUG)
    # node1.logger.addHandler(logging.StreamHandler(sys.stdout))

    # sim.logger.setLevel(logging.DEBUG)
    # sim.logger.addHandler(logging.StreamHandler(sys.stdout))

    # node2 = STM32Node('./main.c')
    # node2.load_source('./examples/native-stm32/main.c
    
    sim.run(30)
