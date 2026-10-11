"""
A firmware recipient (end device). It keeps its receiver on at the gateway's channel and
follows the SF schedule of the scheme, and it accounts every FUOTA frame the way the paper's
energy model does (Section V): a frame whose preamble was acquired costs the whole frame
duration of receiver time, a frame whose preamble was not acquired (too weak, or hit during
the preamble) only the preamble duration.

The accounting hooks into the radio's reception callback so that lost frames are seen too; in
"model" interference mode the statistical interference model is applied there as well.
"""
from __future__ import annotations

import logging
import math
import random
from dataclasses import dataclass, field, fields

from simulator.environment import simulation_env as sim
from simulator.lora.client_radio import LoraClientRadio
from simulator.lora.packet_metadata import PacketMetadata

from coding import make_decoder
from interference import InterferenceModel
from scenario import Scenario
from session import GATEWAY_POSITION, Session

# How long before a round switch the receiver retunes, so that it is never still tuned to the
# previous SF in the very tick the first frame of the new round starts.
RETUNE_GUARD = 0.001


class RecipientRadio(LoraClientRadio):
    """ A client radio that reports every finished packet (received or not) to its owner. """

    def __init__(self, position: tuple[float, float], on_packet):
        super().__init__(position=position)
        self.log_packets = False
        self._on_packet = on_packet

    async def _on_receive_end(self, packet_id: int) -> None:
        metadata = self._rx_chains[0].packets_in_transit.get(packet_id)
        await super()._on_receive_end(packet_id)
        if metadata is not None:
            await self._on_packet(metadata)


@dataclass
class RecipientStats:
    attempts: int = 0            # FUOTA frames at the recipient's SF while it was listening
    preamble_acquired: int = 0
    received: int = 0            # coded fragments delivered to the decoder
    lost_noise: int = 0          # below the sensitivity (fading)
    lost_collision_preamble: int = 0   # simulated interferer hit the preamble
    lost_collision_payload: int = 0    # simulated interferer hit the payload
    lost_model_preamble: int = 0       # statistical interference model, preamble
    lost_model_payload: int = 0        # statistical interference model, payload
    lost_other: int = 0          # missed start/end, interrupted (should stay 0)
    rx_time: float = 0.0         # seconds of receiver time per the paper's model, Σ e_i / p_r
    frames_per_sf: dict[int, int] = field(default_factory=dict)


class Recipient:
    def __init__(
        self,
        node: int,
        position: tuple[float, float],
        scenario: Scenario,
        session: Session,
        rng: random.Random,
        interference: InterferenceModel | None,
    ):
        self.node = node
        self.position = position
        self.distance = math.hypot(*position)
        self.scenario = scenario
        self.session = session
        self.rng = rng
        self.interference = interference
        self.decoder = make_decoder(
            scenario.effective_code, scenario.fragments, scenario.raptor_fail_first,
            scenario.raptor_fail_next, rng,
        )
        self.stats = RecipientStats()
        self.sf = session.assigned_sf.get(node, session.sf_of_frame(0))
        self.decode_time: float | None = None
        self.decode_sf: int | None = None
        self.radio_energy_at_decode: float | None = None
        self.ack_airtime = scenario.ack_airtime(scenario.uplink_sf_for(self.distance))
        self.radio = RecipientRadio(position=position, on_packet=self._on_packet)
        self.logger = logging.getLogger(f"fuota.recipient-{node}")
        sim.create_task(self.run(), name=f"recipient-{node}")

    # ── Radio control ─────────────────────────────────────────────────────────────────────

    def _tune(self, sf: int) -> None:
        s = self.scenario
        self.sf = sf
        self.radio.set_rx_config(
            bandwidth=s.bandwidth, spreading_factor=sf, code_rate=s.code_rate,
            preamble_len=s.preamble_len, max_payload_len=255, frequency=s.gateway_frequency,
        )

    async def run(self) -> None:
        self._tune(self.sf)
        await self.radio.receive()
        # Follow the SF schedule of the proposed scheme. Nothing else to do: the accounting
        # happens in the radio callback, and the session end is observed by the gateway.
        for (time, sf) in self.session.round_switches():
            if self.session.done:
                break
            await sim.sleep_until(time - RETUNE_GUARD)
            if self.session.done:
                break
            self._tune(sf)

    async def send_ack(self) -> None:
        """ The acknowledgement of Section III, sent with the recipient's own uplink SF. """
        s = self.scenario
        self.radio.set_tx_config(
            power=s.tx_power, bandwidth=s.bandwidth,
            spreading_factor=s.uplink_sf_for(self.distance), code_rate=s.code_rate,
            preamble_len=s.preamble_len, frequency=s.gateway_frequency,
        )
        await self.radio.transmit_data_blocking(b"ACK".ljust(s.ack_payload, b"\0"))
        await self.radio.off()

    # ── Accounting ────────────────────────────────────────────────────────────────────────

    async def _on_packet(self, meta: PacketMetadata) -> None:
        packet = meta.packet
        if packet.tx_location != GATEWAY_POSITION or self.decoder.decoded:
            return
        # The frame was queued for the upper layer by the radio, but the accounting below is
        # all the upper layer there is.
        self.radio.flush_rx_queue()
        sf = int(packet.config.spreading_factor)
        if sf != self.sf:
            # GB recipients ignore the other groups' frames; for the proposed scheme this would
            # mean the retune guard failed, which the `lost_other` counter would reveal.
            if self.scenario.scheme in ("gb_e", "gb_l"):
                return
        s = self.scenario
        stats = self.stats
        stats.attempts += 1
        stats.frames_per_sf[sf] = stats.frames_per_sf.get(sf, 0) + 1
        frame_time = s.frame_airtime(sf)
        preamble_time = s.preamble_airtime(sf)

        if meta.missed_start or meta.interrupted or meta.missed_end or sf != self.sf:
            stats.lost_other += 1
            stats.rx_time += preamble_time
            return
        if meta.demodulate_failure:
            stats.lost_noise += 1
            stats.rx_time += preamble_time
            return
        if not meta.received_preamble:
            # A simulated interferer collided with the preamble.
            stats.lost_collision_preamble += 1
            stats.rx_time += preamble_time
            return
        stats.preamble_acquired += 1
        stats.rx_time += frame_time
        if meta.collision:
            stats.lost_collision_payload += 1
            return
        if self.interference is not None:
            outcome = self.interference.resolve(sf, packet.rssi, frame_time, preamble_time)
            if outcome == "preamble":
                # Acquisition would have failed: only the preamble was demodulated after all.
                stats.preamble_acquired -= 1
                stats.rx_time += preamble_time - frame_time
                stats.lost_model_preamble += 1
                return
            if outcome == "payload":
                stats.lost_model_payload += 1
                return
        stats.received += 1
        if self.decoder.add_fragment():
            await self._decoded(sf)

    async def _decoded(self, sf: int) -> None:
        now = sim.current_time()
        self.decode_time = now - self.session.start_time
        self.decode_sf = sf
        self.radio_energy_at_decode = self.radio.power_consumer.get_total_energy_consumed()
        self.session.report_decoded(self.node, now)
        self.logger.debug(f"{now:.1f} node {self.node} decoded after {self.stats.attempts} attempts")
        await sim.start_child_task(self.send_ack(), name=f"ack-{self.node}")

    # ── Results ───────────────────────────────────────────────────────────────────────────

    @property
    def decoded(self) -> bool:
        return self.decoder.decoded

    def energy_normalised(self, with_control: bool) -> float:
        """ E_T(d0) / (p_r × normalisation): the paper's normalised energy consumption. """
        s = self.scenario
        energy = self.stats.rx_time
        if with_control:
            energy += s.control_airtime + s.tx_rx_power_ratio * self.ack_airtime
        return energy / s.energy_normalisation()

    def row(self) -> dict:
        row = {
            "node": self.node,
            "distance": self.distance,
            "x": self.position[0],
            "y": self.position[1],
            "assigned_sf": self.session.assigned_sf.get(self.node),
            "decoded": self.decoded,
            "delivery_time": self.decode_time,
            "decode_sf": self.decode_sf,
            "ec": self.energy_normalised(with_control=True),
            "ec_without_control": self.energy_normalised(with_control=False),
            "rx_time": self.stats.rx_time,
            "radio_energy_j": self.radio_energy_at_decode,
            "uplink_sf": self.scenario.uplink_sf_for(self.distance),
        }
        for f in fields(RecipientStats):
            if f.name not in ("rx_time", "frames_per_sf"):
                row[f.name] = getattr(self.stats, f.name)
        return row
