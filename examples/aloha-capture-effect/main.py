#!/usr/bin/env python3
"""
ALOHA with capture effect — configured to approximate the measurement setup
from Chasserat, Accettura & Berthou (WiMob 2022):
  - 10 devices at 14 dBm TX power
  - Devices placed at varying distances so that RSSI at the gateway spans
    approximately -101 to -113 dBm (matching the paper's observed range)
  - Indoor NLOS path loss model (exponent=5.0, sigma=0)
"""
import sys, random, math
import pandas as pd

sys.path.append('.')

from simulator.lora.phy_layer import LoraPhyLayer
from simulator.lora.client_radio import LoraClientRadio
from simulator.environment import simulation_env as sim
from simulator.lora.enums.bandwidth import Bandwidth
from simulator.lora.enums.code_rate import CodeRate
from simulator.lora.enums.spreading_factor import SpreadingFactor
from simulator.lora.airtime import estimate_airtime
from simulator.path_loss.log_distance_path_loss import log_distance_path_loss

# All nodes must use the exact same radio configuration
SP = SpreadingFactor.SF7
BW = Bandwidth.KHz125
CR = CodeRate.CR4_5

# Target offered load (real value is computed from collected data)
G = float(sys.argv[1].replace(',', '.')) if len(sys.argv) > 1 else 4.0

# Number of nodes (matching Chasserat et al. 2022 validation setup)
NUM_NODES = 10

# TX power in dBm (matching Chasserat et al. 2022)
TX_POWER = 14

# Packet size chosen to achieve the maximum ToA for SF7 (~389 ms),
# matching the Chasserat et al. (2022) experimental setup.
PACKET_SIZE = 246

# The time it takes to send one packet
PACKET_TIME = estimate_airtime(
    payload_len=PACKET_SIZE,
    spreading_factor=SP,
    bandwidth=BW,
    code_rate=CR,
)

# Total simulation duration (in seconds)
SIM_DURATION = 200

RESULTS_CSV = "examples/aloha-capture-effect/results.csv"

TX_RATE = G / (NUM_NODES * PACKET_TIME)

# Place 10 nodes at distances that produce RSSI from ~-101 to ~-113 dBm
# at the receiver. With path_loss_exponent=5.0 and the simulator's default
# RSSI formula (RSSI = tx_power - path_loss), distances 200m–347m produce
# the target range.
PATH_LOSS_EXPONENT = 5.0
D_MIN = 200.0   # closest node → RSSI ≈ -101 dBm
D_MAX = 347.0   # farthest node → RSSI ≈ -113 dBm
NODE_DISTANCES = [D_MIN + i * (D_MAX - D_MIN) / (NUM_NODES - 1) for i in range(NUM_NODES)]

tx_attempts = 0
successful_rx = 0


class Node:
    def __init__(self, node_id: int, distance: float):
        self.node_id = node_id
        self.radio = LoraClientRadio(position=(distance, 0.0))
        sim.create_task(self.run())

    async def run(self):
        self.radio.set_rx_config(spreading_factor=SP, bandwidth=BW, code_rate=CR)
        self.radio.set_tx_config(power=TX_POWER, spreading_factor=SP, bandwidth=BW, code_rate=CR)
        try:
            while sim.is_running():
                backoff = random.expovariate(TX_RATE)
                await sim.sleep(backoff)
                global tx_attempts
                tx_attempts += 1
                payload = random.randbytes(PACKET_SIZE)
                await self.radio.transmit_data_blocking(payload)
        except TimeoutError:
            return


class Receiver:
    def __init__(self, nodes: list[Node]):
        self.radio = LoraClientRadio(position=(0.0, 0.0))
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
    # Setup PHY with indoor NLOS path loss (no random shadowing so each
    # device gets a deterministic, consistent RSSI based on its distance).
    phy = LoraPhyLayer(
        path_loss=log_distance_path_loss(exponent=PATH_LOSS_EXPONENT, sigma=0.0)
    )
    phy.enable_capture_effect = True

    nodes = [Node(node_id=i, distance=d) for i, d in enumerate(NODE_DISTANCES)]
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
