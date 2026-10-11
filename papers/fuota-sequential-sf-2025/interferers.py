"""
Interferers for the "simulated" interference mode: real radios around the recipients that
transmit short frames with pure ALOHA, a random SF and a random channel, as in Section IV.
Their frames go through the simulator's PHY layer, so collisions with FUOTA frames are
resolved by the simulator's own capture model rather than the paper's SIR matrix (see README).
"""
from __future__ import annotations

import random

from simulator.environment import simulation_env as sim
from simulator.lora.client_radio import LoraClientRadio

from scenario import EU868_CHANNELS, SPREADING_FACTORS, Scenario
from session import Session


class Interferer:
    def __init__(self, position: tuple[float, float], scenario: Scenario, session: Session, rng: random.Random):
        self.scenario = scenario
        self.session = session
        self.rng = rng
        self.frames_sent = 0
        self.radio = LoraClientRadio(position=position)
        self.radio.log_packets = False
        # Transmit only: after a frame the radio drops back to standby, where the PHY layer
        # leaves it alone (it never needs to hear anything).
        self.radio.set_rx_config(rx_continuous=False)
        sim.create_task(self.run())

    async def run(self) -> None:
        s = self.scenario
        payload = bytes(s.interferer_payload)
        while True:
            await sim.sleep(self.rng.expovariate(s.interferer_frame_rate))
            if self.session.done:
                break
            self.radio.set_tx_config(
                power=s.tx_power, bandwidth=s.bandwidth,
                spreading_factor=self.rng.choice(SPREADING_FACTORS), code_rate=s.code_rate,
                preamble_len=s.preamble_len,
                frequency=EU868_CHANNELS[self.rng.randrange(s.channels)],
            )
            await self.radio.transmit_data_blocking(payload)
            self.frames_sent += 1
        await self.radio.off()
