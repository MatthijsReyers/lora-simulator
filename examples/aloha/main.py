#!/usr/bin/env python3
import sys, random
import pandas as pd

sys.path.append('.')

from simulator.lora.phy_layer import LoraPhyLayer
from simulator.lora.client_radio import LoraClientRadio
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
G = float(sys.argv[1].replace(',', '.')) if len(sys.argv) > 1 else 4.0

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

# Total simulation duration (in seconds)
SIM_DURATION = 200

RESULTS_CSV = "examples/aloha/results.csv"

TX_RATE = G / (NUM_NODES * PACKET_TIME)

tx_attempts = 0
successful_rx = 0


class Node:
    def __init__(self, node_id: int):
        self.node_id = node_id
        self.radio = LoraClientRadio()
        sim.create_task(self.run())

    async def run(self):
        self.radio.set_rx_config(spreading_factor=SP, bandwidth=BW, code_rate=CR)
        self.radio.set_tx_config(power=4, spreading_factor=SP, bandwidth=BW, code_rate=CR)
        try:
            while sim.is_running():
                backoff = random.expovariate(TX_RATE)
                await sim.sleep(backoff)
                global tx_attempts
                tx_attempts += 1
                payload = random.randbytes(PACKET_SIZE)
                await self.radio.transmit_data_blocking(payload)
        except TimeoutError as e:
            return


class Receiver:
    def __init__(self, nodes: list[Node]):
        self.radio = LoraClientRadio()
        self.nodes = nodes
        sim.create_task(self.run())

    async def run(self):
        self.radio.set_rx_config(spreading_factor=SP, bandwidth=BW, code_rate=CR)
        try:
            while sim.is_running():
                packet = await self.radio.receive_data_wait()
                global successful_rx
                successful_rx += 1
        except TimeoutError as e:
            return


if __name__ == "__main__":
    # ALOHA throughput analysis assumes that any overlapping packets cause a collision and full
    # packet loss so we disable the capture effect for this experiment.
    phy = LoraPhyLayer()
    phy.enable_capture_effect = False

    nodes = [ Node(node_id=i) for i in range(NUM_NODES) ]
    receiver = Receiver(nodes)
    sim.run(SIM_DURATION)

    print(f"Total TX attempts: {tx_attempts}")
    print(f"Total successful RX: {successful_rx}")

    G_measured = tx_attempts * PACKET_TIME / SIM_DURATION
    S = successful_rx * PACKET_TIME / SIM_DURATION

    print(f"Offered load G: {G_measured:.4f}")
    print(f"Throughput S: {S:.4f}")

    try: 
        df = pd.read_csv(RESULTS_CSV, index_col=False)
        df = pd.concat([df, pd.DataFrame([{
            "sent": tx_attempts,
            "received": successful_rx,
            "G": G_measured,
            "S": S,
        }])], ignore_index=True)
    except FileNotFoundError:
        df = pd.DataFrame({
            "sent": [tx_attempts],
            "received": [successful_rx],
            "G": [G_measured],
            "S": [S],
        })
    df.to_csv(RESULTS_CSV, index=False)
