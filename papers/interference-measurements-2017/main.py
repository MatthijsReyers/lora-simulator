#!/usr/bin/env python3
import logging
import asyncio, random, sys
sys.path.append('.') # To allow importing the simulator package while running from root folder.
from simulator.lora.enums.bandwidth import Bandwidth
from simulator.lora.enums.code_rate import CodeRate
from simulator.lora.enums.spreading_factor import SpreadingFactor
from simulator.lora.radio import LoraRadio
from simulator.lora.phy_layer import LoraPhyLayer
from simulator.environment import simulation_env as sim

MEASUREMENTS = 1200

class Node:
    def __init__(self, tx_offset, tx_pwr, bandwidth, sf, cr, preamble_len):
        self.radio = LoraRadio()
        self.tx_offset = tx_offset
        self.radio.set_rx_config(
            bandwidth=bandwidth,
            spreading_factor=sf,
            code_rate=cr,
            preamble_len=preamble_len
        )
        self.radio.set_tx_config(
            power=tx_pwr,
            bandwidth=bandwidth,
            spreading_factor=sf,
            code_rate=cr,
            preamble_len=preamble_len
        )
        sim.create_task(self.run())

    async def run(self):
        for i in range(MEASUREMENTS):
            await sim.sleep_until((1+i) * 1000 + self.tx_offset)
            await self.radio.transmit_data_blocking(
                bytes([ i for i in range(17) ])
            )

class Receiver:
    def __init__(self, bandwidth, sf, cr, preamble_len):
        self.radio = LoraRadio()
        self.radio.set_rx_config(
            bandwidth=bandwidth,
            spreading_factor=sf,
            code_rate=cr,
            preamble_len=preamble_len
        )
        sim.create_task(self.run())
    async def run(self):
        while sim.is_running():
            try:
                _packet = await self.radio.receive_data_wait()
            except asyncio.TimeoutError:
                break


if __name__ == '__main__':
    offset_ms = int(sys.argv[1]) if len(sys.argv) > 1 else 100
    preamble_len = int(sys.argv[2]) if len(sys.argv) > 2 else 8
    SF = SpreadingFactor(int(sys.argv[3])) if len(sys.argv) > 3 else SpreadingFactor.SF12
    tx_delta = int(sys.argv[4]) if len(sys.argv) > 4 else 0

    BW = Bandwidth.KHz125
    CR = CodeRate.CR4_8

    node1 = Node(tx_offset=0, tx_pwr=20-tx_delta, bandwidth=BW, sf=SF, cr=CR, preamble_len=preamble_len)
    node2 = Node(tx_offset=(offset_ms / 1000), tx_pwr=20, bandwidth=BW, sf=SF, cr=CR, preamble_len=preamble_len)
    receiver = Receiver(bandwidth=BW, sf=SF, cr=CR, preamble_len=preamble_len)
    sim.run(simulation_length=1000000000)

    phy = LoraPhyLayer()

    bw = BW.to_khz()
    cr = CR.to_denominator()
    sf = SF.value

    settings = f'bw{bw}_sf{sf}_cr{cr}_pre{preamble_len}_txd{tx_delta}_{offset_ms}'

    receiver.radio.packets_log.to_csv(
        f'./papers/interference-measurements-2017/data/{settings}_receiver_packets.csv', 
        index=False
    )
    phy.packets_log.to_csv(
        f'./papers/interference-measurements-2017/data/{settings}_phy_packets.csv', 
        index=False
    )
