#!/usr/bin/env python3
import sys, time, asyncio, logging
import logging

sys.path.append('.')

from simulator.lora.packet import LoraPacket
from simulator.lora.phy_layer import LoraPhyLayer
from simulator.lora.radio import LoraRadio
from simulator.environment import simulation_env as sim
import random

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

class Node:
    def __init__(self, code_rate):
        self.radio = LoraRadio()
        self.code_rate = code_rate
        sim.create_task(self.recv())
        sim.create_task(self.send())

    async def recv(self):
        self.radio.set_rx_config(spreading_factor=7, bandwidth=125, code_rate=self.code_rate)
        while sim.is_running():
            try:
                packet = await self.radio.receive_data_wait()
                print(f'{sim.current_time():.4f} Node {id(self) % 1000} Received packet:', packet.payload)
                if packet.payload == b"Ping":
                    print(f'{sim.current_time():.4f} Node {id(self) % 1000} Sending pong')
                    await self.radio.transmit_data_blocking(b"Pong")
                    await self.radio.receive(continuous=True)
            except asyncio.TimeoutError:
                print(f'{sim.current_time():.4f} Node {id(self) % 1000} Receive timed out')

    async def send(self):
        self.radio.set_tx_config(power=14, spreading_factor=7, bandwidth=125, code_rate=self.code_rate)
        await sim.sleep(random.random() * 15)
        for _ in range(2):
            await sim.sleep(15)
            print(f'{sim.current_time():.4f} Node {id(self) % 1000} Sending ping')
            await self.radio.transmit_data_blocking(b"Ping")
            await self.radio.receive(continuous=True)


if __name__ == "__main__":

    level = logging.INFO
    
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

    node1 = Node(code_rate=7)
    node2 = Node(code_rate=7)

    node1.radio.logger.setLevel(level)
    node1.radio.logger.addHandler(ch)
    
    node2.radio.logger.setLevel(level)
    node2.radio.logger.addHandler(ch)

    sim.run(simulation_length=120)

