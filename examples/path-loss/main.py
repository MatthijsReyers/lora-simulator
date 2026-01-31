#!/usr/bin/env python3
import sys, random
import pandas as pd

sys.path.append('.')

from simulator.lora.radio import LoraRadio
from simulator.environment import simulation_env as sim
from simulator.lora.enums.bandwidth import Bandwidth
from simulator.lora.enums.code_rate import CodeRate
from simulator.lora.enums.spreading_factor import SpreadingFactor
from simulator.lora.phy_layer import LoraPhyLayer

SP = SpreadingFactor.SF7
BW = Bandwidth.KHz125
CR = CodeRate.CR4_5

data = {
    "distance": [],
    "rx_power"  : [],
}

class Node:
    def __init__(self, pos):
        self.radio = LoraRadio(position=pos)
        sim.create_task(self.run())
    async def run(self):
        self.radio.set_tx_config(power=8, spreading_factor=SP, bandwidth=BW, code_rate=CR)
        await sim.sleep(self.radio.position[0] + 0.1)
        print(f"Send packet at {self.radio.position[0]}m")
        data["distance"].append(self.radio.position[0])
        await self.radio.transmit_data(b'Hello, World!')

class Receiver:
    def __init__(self):
        self.radio = LoraRadio(position=(0,0))
        sim.create_task(self.run())
    async def run(self):
        self.radio.set_rx_config(spreading_factor=SP, bandwidth=BW, code_rate=CR)
        try:
            while sim.is_running():
                (packet, meta) = await self.radio.receive_data_wait(metadata=True)
                data["rx_power"].append(meta.rx_power)
                print(f"Received packet: {packet.id}, rx={meta.rx_power:.2f}dbm")
        except TimeoutError as e:
            return

if __name__ == "__main__":
    phy = LoraPhyLayer(
        path_loss_exponent=3,
        path_loss_sigma=2,
    )

    nodes = [ Node(pos=(i, 0)) for i in range(10) ]
    nodes += [ Node(pos=(i, 0)) for i in range(10, 100, 10) ]
    nodes += [ Node(pos=(i, 0)) for i in range(100, 1000, 50) ]
    nodes += [ Node(pos=(i, 0)) for i in range(1000, 10000, 100) ]
    nodes += [ Node(pos=(i, 0)) for i in range(10000, 100000, 1000) ]
    
    receiver = Receiver()
    
    sim.run(1000000000)

    pd.DataFrame(data).to_csv("path_loss_data.csv", index=False)