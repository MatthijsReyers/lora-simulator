"""
What the end nodes of the three update methods have in common: the receive loop, the storage
of the received chunks, the running firmware version, and the handling of the frames that are
the same for every method (checksum fragments, status requests, the flash command and the
handshake after the reboot).
"""
import asyncio, hashlib
from abc import abstractmethod
from typing import Callable

from simulator.environment import simulation_env as sim
from simulator.exceptions import SimulatorException
from simulator.lora.enums.radio_state import RadioState

from frames import (
    BROADCAST_ADDRESS, GATEWAY_ADDRESS, ApplicationData,
    ChecksumFragment, FlashNewVersion, HandshakeRequest, HandshakeResponse, StatusRequest,
    StatusResponse, WaitingForChecksumFragment, MiWiFrame, GatewayMessage, NodeMessage,
    decode_gateway_message,
)
from scenario import DutyCycle, OtaDevice, Scenario


class OtaNode(OtaDevice):

    def __init__(
        self,
        address: int,
        position: tuple[float, float],
        scenario: Scenario,
        duty_cycle: DutyCycle,
    ):
        super().__init__(address, position, scenario, duty_cycle)
        self.firmware_version = scenario.old_version
        self.flash_count = 0
        self.frames_handled = 0
        self._reset_update_state()
        self.uplinks_sent = 0
        # Set by whoever runs the scenario: the uplinks stop with the campaign, as the
        # simulation only ends once nothing is left to happen.
        self.campaign_finished: Callable[[], bool] = lambda: False
        self._transmitting = False
        sim.create_task(self.run(), name=f"node-{address}")
        if scenario.uplink_interval is not None:
            sim.create_task(self.uplinks(), name=f"node-{address}-uplinks")

    async def transmit(self, destination: int, payload: bytes) -> float:
        # The answer to the gateway and the node's own uplink come from two tasks and share
        # one transmitter.
        while self._transmitting:
            await sim.sleep(0.01)
        self._transmitting = True
        try:
            return await super().transmit(destination, payload)
        finally:
            self._transmitting = False

    async def uplinks(self) -> None:
        """
            The node's own traffic: a small frame to the gateway every `uplink_interval`, at
            this node's moment within the interval.
        """
        interval = self.scenario.uplink_interval
        assert interval is not None
        moment = 1.0 + (self.address - 1) * interval / self.scenario.nodes
        try:
            while True:
                await sim.sleep_until(moment)
                if not sim.is_running() or self.campaign_finished():
                    return
                # Not while the node is off the air to flash and reboot.
                if self.radio.get_state() != RadioState.OFF:
                    await self.transmit(GATEWAY_ADDRESS, ApplicationData().to_bytes())
                    self.uplinks_sent += 1
                moment += interval
        except SimulatorException:
            return

    def _reset_update_state(self) -> None:
        # The chunks of the binary by fragment number, None for chunks not received (yet).
        self.chunks: list[bytes | None] = []
        self.checksum_fragments = 0
        self.checksum = bytearray()
        self.checksum_complete = False
        self.checksum_valid = False

    @property
    def binary_fragments(self) -> int:
        """ `bin_frags` in the paper, the number of received fragments of the binary. """
        return sum(chunk is not None for chunk in self.chunks)

    @property
    @abstractmethod
    def binary_complete(self) -> bool:
        ...

    def assembled_binary(self) -> bytes:
        return b"".join(chunk or b"" for chunk in self.chunks)

    # ── Receive loop ────────────────────────────────────────────────────────

    async def run(self) -> None:
        while True:
            try:
                # The radio cannot receive while the node's own uplink is on the air; whatever
                # is sent in the meantime is missed.
                while self._transmitting:
                    await sim.sleep(0.01)
                packet = await self.radio.receive_data_wait()
            except (asyncio.TimeoutError, SimulatorException):
                return  # Simulation is over.

            frame = self.decode_frame(packet.payload)
            if frame.destination not in (self.address, BROADCAST_ADDRESS):
                continue

            message = decode_gateway_message(frame.payload)
            self.frames_handled += 1
            self.logger.debug(f"{sim.current_time():.3f} node {self.address} got {message!r:.60}")

            await sim.sleep(self.scenario.processing_delay)
            reply = await self.handle(frame, message)
            if reply is not None:
                await self.transmit(frame.source, reply.to_bytes())

    async def handle(self, frame: MiWiFrame, message: GatewayMessage) -> NodeMessage | None:
        match message:
            case ChecksumFragment(number=number, data=data, last=last):
                if number == self.checksum_fragments and not self.checksum_complete:
                    self.checksum += data
                    self.checksum_fragments += 1
                    self.checksum_complete = last
                    if last:
                        self._verify_checksum()
                return WaitingForChecksumFragment(expected=self.checksum_fragments)

            case StatusRequest():
                return StatusResponse(
                    firmware_version=self.firmware_version,
                    binary_fragments=self.binary_fragments,
                    checksum_fragments=self.checksum_fragments,
                    checksum_valid=self.checksum_valid,
                )

            case FlashNewVersion():
                await self._flash()
                return None

            case HandshakeRequest():
                return HandshakeResponse()

        return await self.handle_update_message(frame, message)

    @abstractmethod
    async def handle_update_message(
        self, frame: MiWiFrame, message: GatewayMessage,
    ) -> NodeMessage | None:
        """ Handles the frames that are specific to the update method. """
        ...

    # ── Flashing ────────────────────────────────────────────────────────────

    def _verify_checksum(self) -> None:
        self.checksum_valid = (
            self.binary_complete and
            hashlib.sha256(self.assembled_binary()).digest() == bytes(self.checksum)
        )

    async def _flash(self) -> None:
        """
            Flashes the stored image and reboots, the node is off the air until it comes back up
            with the new firmware (which resets all of the update state).
        """
        if not self.checksum_valid:
            self.logger.warning(f"{sim.current_time():.3f} node {self.address} refused to flash")
            return
        await self.radio.off()
        await sim.sleep(self.scenario.reboot_delay)
        self.firmware_version = self.assembled_binary()[0]
        self.flash_count += 1
        self._reset_update_state()
        self.logger.info(
            f"{sim.current_time():.3f} node {self.address} rebooted into version "
            f"{self.firmware_version}"
        )
