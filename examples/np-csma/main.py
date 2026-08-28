#!/usr/bin/env python3
"""
Non-persistent CSMA (Carrier Sense Multiple Access) simulation.

In non-persistent CSMA:
1. When a node has a packet to send, it senses the channel.
2. If the channel is idle, it transmits immediately.
3. If the channel is busy, it waits a random backoff time before sensing again.

The key distinction from p-persistent CSMA is step 3: instead of persistently
waiting for the channel to become idle, the node backs off for a random duration.
This reduces collisions at the cost of slightly higher idle time.

The Kleinrock-Tobagi (1975) closed-form throughput for non-persistent CSMA is:
    S = G * exp(-a*G) / (G*(1 + 2*a) + exp(-a*G))

With propagation delay a ≈ 0, this simplifies to: S = G / (1 + G).
"""
import sys, random
import pandas as pd

sys.path.append('.')

from simulator.lora.phy_layer import LoraPhyLayer
from simulator.lora.client_radio import LoraClientRadio
from simulator.environment import simulation_env as sim
from simulator.exceptions import SimulatorException
from simulator.lora.enums.bandwidth import Bandwidth
from simulator.lora.enums.code_rate import CodeRate
from simulator.lora.enums.spreading_factor import SpreadingFactor
from simulator.lora.airtime import estimate_airtime

# All nodes must use the exact same radio configuration
SP = SpreadingFactor.SF7
BW = Bandwidth.KHz125
CR = CodeRate.CR4_5

# Target offered load (real value is computed from collected data)
G = float(sys.argv[1].replace(',', '.')) if len(sys.argv) > 1 else 1.0

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

RESULTS_CSV = "examples/np-csma/results.csv"

# When RESULTS_SINGLE is set, append only to that file (for parallel runs)
RESULTS_SINGLE = None
if len(sys.argv) > 2:
    RESULTS_SINGLE = sys.argv[2]

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
                # Wait for a packet to be ready (exponential inter-arrival)
                backoff = random.expovariate(TX_RATE)
                await sim.sleep(backoff)

                # Non-persistent CSMA: sense and transmit or backoff
                await self._np_csma_transmit()
        except (TimeoutError, SimulatorException):
            return

    async def _np_csma_transmit(self):
        """
        Non-persistent CSMA protocol (a ≈ 0):

        1. Sense the channel (instantaneous ideal carrier sense).
        2. If idle → transmit immediately.
        3. If busy → back off for a random time, then re-sense.

        The backoff time is drawn from an exponential distribution with the
        same mean as the packet inter-arrival time. This matches the standard
        non-persistent CSMA model where re-scheduled transmissions are
        indistinguishable from new arrivals.
        """
        global tx_attempts

        while True:
            # Sense the channel
            if not self.radio.carrier_sense_instant():
                # Channel idle → commit to transmit after one tick
                # (vulnerability window for simultaneous sense)
                await sim.advance_tick()
                tx_attempts += 1
                payload = random.randbytes(PACKET_SIZE)
                await self.radio.transmit_data_blocking(payload)
                return
            else:
                # Channel busy → random backoff (non-persistent behaviour)
                backoff = random.expovariate(TX_RATE)
                await sim.sleep(backoff)


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
        except (TimeoutError, SimulatorException):
            return


if __name__ == "__main__":
    # Disable capture effect for fair comparison with analytical model
    phy = LoraPhyLayer()
    phy.enable_capture_effect = False

    nodes = [Node(node_id=i) for i in range(NUM_NODES)]
    receiver = Receiver(nodes)
    sim.run(SIM_DURATION)

    print(f"Total TX attempts: {tx_attempts}")
    print(f"Total successful RX: {successful_rx}")

    G_measured = tx_attempts * PACKET_TIME / SIM_DURATION
    S = successful_rx * PACKET_TIME / SIM_DURATION

    print(f"Offered load G: {G_measured:.4f}")
    print(f"Throughput S: {S:.4f}")

    row = {
        "sent": tx_attempts,
        "received": successful_rx,
        "G_measured": G_measured,
        "G": G,
        "S": S,
    }

    out_file = RESULTS_SINGLE or RESULTS_CSV
    try:
        df = pd.read_csv(out_file, index_col=False)
        df = pd.concat([df, pd.DataFrame([row])], ignore_index=True)
    except FileNotFoundError:
        df = pd.DataFrame([row])
    df.to_csv(out_file, index=False)
