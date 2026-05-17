#!/usr/bin/env python3
"""
p-CSMA (p-persistent Carrier Sense Multiple Access) simulation.

In p-CSMA:
1. When a node has a packet to send, it senses the channel.
2. If the channel is idle, it transmits with probability p (defers with probability 1-p).
3. If the channel is busy, it waits until the channel becomes idle and repeats step 2.
4. If transmission is deferred, it waits one slot time and repeats the process.

This implementation uses an event-driven wait_for_channel_idle() method so that
nodes block until the channel is free instead of polling.  The effective propagation
delay a ≈ 0 (one simulation tick ≈ 1 µs vs packet time ≈ 46 ms), matching the
idealised Kleinrock-Tobagi (1975) model.
"""
import sys, math, random
import pandas as pd

sys.path.append('.')

from simulator.lora.phy_layer import LoraPhyLayer
from simulator.lora.radio import LoraRadio
from simulator.environment import simulation_env as sim
from simulator.exceptions import SimulatorException
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

# Slot time: one simulation tick.  With the default tick of 1 µs and a packet
# time of ~46 ms this gives a normalised propagation delay of a ≈ 2.2e-5 which
# is effectively zero — matching the textbook Kleinrock-Tobagi idealisation.
SLOT_TIME = 0.000001  # 1 µs (= simulator tick size)

# Total simulation duration (in seconds)
SIM_DURATION = 200

RESULTS_CSV = f"examples/p-csma/results.csv"

# When RESULTS_SINGLE is set, append only to that file (for parallel runs)
RESULTS_SINGLE = None
if len(sys.argv) > 3:
    RESULTS_SINGLE = sys.argv[3]

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
        except (TimeoutError, SimulatorException):
            return

    async def _pcsma_transmit(self):
        """
        p-persistent CSMA with event-driven channel sensing.

        Phase 1 — busy-wait:  block until the channel transitions to idle
        using wait_for_channel_idle() (O(1) — no polling).

        Phase 2 — idle contention:  draw the number of deferred slots from
        a geometric distribution.  Each "slot" is one simulation tick (≈ 0).

        Phase 3 — vulnerability window:  advance one tick before transmitting
        so that other committed nodes can also start in the same tick
        (→ collision if multiple).
        """
        global tx_attempts
        
        while True:
            # Phase 1: wait for channel to be idle (event-driven).
            await self.radio.wait_for_channel_idle()
            
            # Phase 2: channel is idle — p-persistent contention.
            if P >= 1.0:
                defer_slots = 0
            else:
                u = random.random() or 1e-10  # avoid log(0)
                defer_slots = int(math.log(u) / math.log(1.0 - P))
            
            if defer_slots > 0:
                await sim.sleep(SLOT_TIME * defer_slots)
                # Re-check: another node may have transmitted during deferral
                if self.radio.carrier_sense_instant():
                    continue  # go back to busy-wait
            
            # Phase 3: committed — vulnerability window (one tick).
            await sim.advance_tick()
            
            # Transmit (committed — do NOT re-check channel).
            tx_attempts += 1
            payload = random.randbytes(PACKET_SIZE)
            await self.radio.transmit_data_blocking(payload)
            return


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
        except (TimeoutError, SimulatorException):
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

    row = {
        "p": P,
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
