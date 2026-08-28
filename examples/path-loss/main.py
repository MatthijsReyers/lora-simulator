#!/usr/bin/env python3
import logging
import sys, random
import pandas as pd

sys.path.append('.')

from simulator.path_loss.log_distance_path_loss import log_distance_path_loss
from simulator.lora.client_radio import LoraClientRadio
from simulator.environment import simulation_env as sim
from simulator.lora.enums.bandwidth import Bandwidth
from simulator.lora.enums.code_rate import CodeRate
from simulator.lora.enums.spreading_factor import SpreadingFactor
from simulator.lora.phy_layer import LoraPhyLayer

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

SP = SpreadingFactor.SF7
BW = Bandwidth.KHz125
CR = CodeRate.CR4_5

data = {
    "distance": [],
    "rssi"  : [],
}

class Node:
    def __init__(self, pos):
        self.radio = LoraClientRadio(position=pos)
        sim.create_task(self.run())
    async def run(self):
        self.radio.set_tx_config(power=8, spreading_factor=SP, bandwidth=BW, code_rate=CR)
        await sim.sleep(self.radio.position[0] + 0.1)
        print(f"Send packet at {self.radio.position[0]}m")
        data["distance"].append(self.radio.position[0])
        await self.radio.transmit_data(b'Hello, World!')

class Receiver:
    def __init__(self):
        self.radio = LoraClientRadio(position=(0,0))
        sim.create_task(self.run())
    async def run(self):
        self.radio.set_rx_config(spreading_factor=SP, bandwidth=BW, code_rate=CR)
        try:
            while sim.is_running():
                (packet, meta) = await self.radio.receive_data_wait(metadata=True)
                data["rssi"].append(packet.rssi)
                print(f"Received packet: {packet.id}, rssi={packet.rssi:.2f}dbm")
        except TimeoutError as e:
            return

if __name__ == "__main__":
    phy = LoraPhyLayer(path_loss=log_distance_path_loss(exponent=3, sigma=2))

    receiver = Receiver()

    nodes = [ Node(pos=(i, 0)) for i in range(10) ]
    nodes += [ Node(pos=(i, 0)) for i in range(10, 100, 10) ]
    nodes += [ Node(pos=(i, 0)) for i in range(100, 1000, 50) ]
    nodes += [ Node(pos=(i, 0)) for i in range(1000, 10000, 100) ]
    nodes += [ Node(pos=(i, 0)) for i in range(10000, 100000, 1000) ]
    nodes += [ Node(pos=(i, 0)) for i in range(100000, 1000000, 10000) ]
    
    sim.run(1000000000)

    level = logging.DEBUG

    ch = logging.StreamHandler()
    ch.setLevel(level)
    formatter = logging.Formatter("%(levelname)s;%(message)s")
    ch.setFormatter(formatter)

    logger.addHandler(ch)

    sim.logger.setLevel(logging.INFO)
    sim.logger.addHandler(ch)

    phy_layer = LoraPhyLayer()
    phy_layer.logger.setLevel(level)
    phy_layer.logger.addHandler(ch)

    nodes[0].radio.logger.setLevel(level)
    nodes[0].radio.logger.addHandler(ch)

    receiver.radio.logger.setLevel(level)
    receiver.radio.logger.addHandler(ch)

    pd.DataFrame(data).to_csv("path_loss_data.csv", index=False)