"""
The gateway: broadcasts rateless coded fragments at the maximum duty cycle, either cycling
through the SFs (proposed scheme, Section III), at a single SF (FSF benchmark) or group after
group (GB benchmarks, Section VI). It learns that a recipient is done through the session
object, i.e. acknowledgements are assumed to arrive (the recipients do transmit them, but the
gateway does not need to receive them).
"""
from __future__ import annotations

import logging
import struct

from simulator.environment import simulation_env as sim
from simulator.lora.client_radio import LoraClientRadio

from scenario import Scenario
from session import GATEWAY_POSITION, Session


class Gateway:
    def __init__(self, scenario: Scenario, session: Session, max_duration: float):
        self.scenario = scenario
        self.session = session
        self.max_duration = max_duration
        self.radio = LoraClientRadio(position=GATEWAY_POSITION)
        self.radio.log_packets = False
        # The gateway never listens, so it sits in standby between frames (and the PHY layer
        # then does not bother it with the interferers' frames).
        self.radio.set_rx_config(rx_continuous=False, frequency=scenario.gateway_frequency)
        self.frames_sent = 0
        self.logger = logging.getLogger("fuota.gateway")
        sim.create_task(self.run(), name="gateway")

    def _configure(self, sf: int) -> None:
        s = self.scenario
        self.radio.set_tx_config(
            power=s.tx_power, bandwidth=s.bandwidth, spreading_factor=sf, code_rate=s.code_rate,
            preamble_len=s.preamble_len, frequency=s.gateway_frequency,
        )

    def _payload(self, index: int) -> bytes:
        """
            A coded fragment: `fragment_size` bytes carrying the transmission index. With a
            rateless code every frame is a new combination, so the recipients only need to know
            that it is a fragment; the index is there for debugging.
        """
        return struct.pack(">I", index).ljust(self.scenario.fragment_size, b"\0")

    async def _transmit(self, sf: int, start: float) -> None:
        self._configure(sf)
        await sim.sleep_until(start)
        await self.radio.transmit_data_blocking(self._payload(self.frames_sent))
        self.frames_sent += 1
        self.session.frames_sent[sf] = self.session.frames_sent.get(sf, 0) + 1

    def _deadline_passed(self, time: float) -> bool:
        if time - self.session.start_time >= self.max_duration:
            self.logger.warning(f"session cap of {self.max_duration:.0f}s reached, stopping")
            self.session.stop(time)
            return True
        return False

    async def run(self) -> None:
        if self.scenario.scheme in ("gb_e", "gb_l"):
            await self._run_groups()
        else:
            await self._run_schedule()
        await self.radio.off()

    async def _run_schedule(self) -> None:
        """ Proposed scheme and FSF: frame n uses `session.sf_of_frame(n)`, back to back at DC_max. """
        next_start = self.session.start_time
        while not self.session.done and not self._deadline_passed(next_start):
            sf = self.session.sf_of_frame(self.frames_sent)
            await self._transmit(sf, next_start)
            next_start += self.scenario.frame_period(sf)

    async def _run_groups(self) -> None:
        """ GB: serve the group with the lowest SF until every member decoded, then the next. """
        next_start = self.session.start_time
        for sf in self.session.groups():
            while not self.session.group_done(sf) and not self._deadline_passed(next_start):
                await self._transmit(sf, next_start)
                next_start += self.scenario.frame_period(sf)
            if self.session.done:
                break
