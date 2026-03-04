#!/usr/bin/env python3
import logging
import asyncio, random, sys
sys.path.append('.') # To allow importing the simulator package while running from root folder.
from simulator.lora.phy_layer import LoraPhyLayer
from simulator.lora.radio import LoraRadio
from simulator.environment import simulation_env as sim

SF = 7
BW = 125
CR = 5

class Receiver:
    def __init__(self):
        self.radio = LoraRadio()
        self.radio.set_rx_config(spreading_factor=SF, bandwidth=BW, code_rate=CR)
        sim.create_task(self.run())
    async def run(self):
        while sim.is_running():
            try:
                packet = await self.radio.receive_data_wait()
                print(f'{sim.current_time():.4f} Node {id(self) % 1000} Received packet:', packet.payload, packet.airtime)
            except asyncio.TimeoutError:
                print(f'{sim.current_time():.4f} Node {id(self) % 1000} Receive timed out')

class Node:
    def __init__(self, power: int, interval: float = 0.5):
        self.radio = LoraRadio()
        self.radio.set_tx_config(power=power, spreading_factor=SF, bandwidth=BW, code_rate=CR)
        self.interval = interval
        sim.create_task(self.run())
    async def run(self):
        await sim.sleep(self.radio._radio_id * 0.001)
        while sim.is_running():
            await self.radio.transmit_data_blocking(b"BeepBoop")
            await sim.sleep(self.interval)


if __name__ == "__main__":
    phy = LoraPhyLayer()

    node1 = Node(power=20, interval=0.5)
    node2 = Node(power=15, interval=0.22)
    receiver = Receiver()
    
    # receiver.radio.logger.setLevel('DEBUG')
    # receiver.radio.logger.addHandler(logging.StreamHandler(sys.stdout))
    
    # Run the simulation for a sufficient length of time to allow all counters to complete
    sim.run(simulation_length=4)

    out_dir = './examples/collisions'

    phy.packets_log.to_csv(f'{out_dir}/packets.csv', index=False)
    node1.radio.packets_log.to_csv(f'{out_dir}/node1_packets.csv', index=False)
    node2.radio.packets_log.to_csv(f'{out_dir}/node2_packets.csv', index=False)
    receiver.radio.packets_log.to_csv(f'{out_dir}/receiver_packets.csv', index=False)