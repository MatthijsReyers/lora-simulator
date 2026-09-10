"""
What the end nodes of the three update methods have in common: the receive loop, the storage
of the received chunks, the running firmware version, and the handling of the frames that are
the same for every method (checksum fragments, status requests, the flash command and the
handshake after the reboot).
"""
import asyncio, hashlib
from abc import abstractmethod

from simulator.environment import simulation_env as sim

from frames import (
    BROADCAST_ADDRESS,
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
        sim.create_task(self.run(), name=f"node-{address}")

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
                packet = await self.radio.receive_data_wait()
            except asyncio.TimeoutError:
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
