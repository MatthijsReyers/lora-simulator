#!/usr/bin/env python3
"""
p-CSMA (p-persistent Carrier Sense Multiple Access) simulation.

In p-CSMA:
1. When a node has a packet to send, it senses the channel.
2. If the channel is idle, it transmits with probability p (defers with probability 1-p).
3. If the channel is busy, it waits until the channel becomes idle and repeats step 2.
4. If transmission is deferred, it waits one slot time and repeats the process.
"""
import sys, random
import pandas as pd

sys.path.append('.')

from simulator.lora.phy_layer import LoraPhyLayer
from simulator.lora.radio import LoraRadio
from simulator.environment import simulation_env as sim
from simulator.lora.enums.bandwidth import Bandwidth
from simulator.lora.enums.code_rate import CodeRate
from simulator.lora.enums.spreading_factor import SpreadingFactor
from simulator.lora.airtime import estimate_airtime, preamble_airtime

# All nodes must use the exact same radio configuration
SP = SpreadingFactor.SF7
BW = Bandwidth.KHz125
CR = CodeRate.CR4_5

# Target offered load (real value is computed from collected data)
G = float(sys.argv[1].replace(',', '.')) if len(sys.argv) > 1 else 1.0

# Persistence probability for p-CSMA (probability of transmitting when channel is idle)
P = float(sys.argv[2].replace(',', '.')) if len(sys.argv) > 2 else 0.3

# Number of nodes to use for the simulation
NUM_NODES = 100

# All packets must have the same size
PACKET_SIZE = 20

# The time it takes to send one packet
PACKET_TIME = estimate_airtime(
    payload_len=PACKET_SIZE,
    spreading_factor=SP,
    bandwidth=BW,
    code_rate=CR,
)

# Slot time for p-CSMA (time to wait when deferring)
SLOT_TIME = PACKET_TIME * 0.1

# Total simulation duration (in seconds)
SIM_DURATION = 200

RESULTS_CSV = f"examples/p-csma/results.csv"

TX_RATE = G / (NUM_NODES * PACKET_TIME)

tx_attempts = 0
successful_rx = 0


class Node:
    def __init__(self, node_id: int):
        self.node_id = node_id
        self.radio = LoraRadio()
        sim.create_task(self.run())

    async def run(self):
        self.radio.set_rx_config(spreading_factor=SP, bandwidth=BW, code_rate=CR)
        self.radio.set_tx_config(power=4, spreading_factor=SP, bandwidth=BW, code_rate=CR)
        try:
            while sim.is_running():
                # Wait for a packet to be ready (exponential inter-arrival)
                backoff = random.expovariate(TX_RATE)
                await sim.sleep(backoff)
                
                # p-CSMA protocol: sense channel before transmitting
                await self._pcsma_transmit()
        except TimeoutError:
            return

    async def _pcsma_transmit(self):
        """Implements p-persistent CSMA transmission logic."""
        global tx_attempts
        
        while True:
            # Sense the channel
            channel_busy = self.radio.carrier_sense_instant()
            
            if not channel_busy:
                # Channel is idle - transmit with probability p
                if random.random() < P:
                    tx_attempts += 1
                    payload = random.randbytes(PACKET_SIZE)
                    await self.radio.transmit_data_blocking(payload)
                    return
                else:
                    # Defer for one slot time
                    await sim.sleep(SLOT_TIME)
            else:
                # Channel is busy - wait until it becomes idle
                # Poll the channel periodically
                await sim.sleep(SLOT_TIME)


class Receiver:
    def __init__(self, nodes: list[Node]):
        self.radio = LoraRadio()
        self.nodes = nodes
        sim.create_task(self.run())

    async def run(self):
        self.radio.set_rx_config(spreading_factor=SP, bandwidth=BW, code_rate=CR)
        try:
            while sim.is_running():
                packet = await self.radio.receive_data_wait()
                global successful_rx
                successful_rx += 1
        except TimeoutError:
            return


if __name__ == "__main__":
    # Disable capture effect for fair comparison with ALOHA analysis
    phy = LoraPhyLayer()
    phy.enable_capture_effect = False

    nodes = [Node(node_id=i) for i in range(NUM_NODES)]
    receiver = Receiver(nodes)
    sim.run(SIM_DURATION)

    print(f"Persistence probability p: {P}")
    print(f"Total TX attempts: {tx_attempts}")
    print(f"Total successful RX: {successful_rx}")

    G_measured = tx_attempts * PACKET_TIME / SIM_DURATION
    S = successful_rx * PACKET_TIME / SIM_DURATION

    print(f"Offered load G: {G_measured:.4f}")
    print(f"Throughput S: {S:.4f}")

    try:
        df = pd.read_csv(RESULTS_CSV, index_col=False)
        df = pd.concat([df, pd.DataFrame([{
            "p": P,
            "sent": tx_attempts,
            "received": successful_rx,
            "G_measured": G_measured,
            "G": G,
            "S": S,
        }])], ignore_index=True)
    except FileNotFoundError:
        df = pd.DataFrame({
            "p": [P],
            "sent": [tx_attempts],
            "received": [successful_rx],
            "G_measured": [G_measured],
            "G": [G],
            "S": [S],
        })
    df.to_csv(RESULTS_CSV, index=False)
