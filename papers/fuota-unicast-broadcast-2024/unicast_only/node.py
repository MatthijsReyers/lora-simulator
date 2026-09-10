"""
End node of the *only unicast* method (section 3.1 of the paper).

The node is a passive party: it keeps its radio in receive mode, ignores frames that are not
addressed to it and answers every frame from the gateway according to its state, which consists
of the number of received binary fragments (`bin_frags` in the paper), the number of received
checksum fragments (`check_frags`) and the running firmware version.
"""
import asyncio, hashlib

from simulator.environment import simulation_env as sim

from frames import (
    BinaryFragment, ChecksumFragment, FlashNewVersion, HandshakeRequest, HandshakeResponse,
    ResetConfirmed, ResetRequest, StatusRequest, StatusResponse, WaitingForBinaryFragment,
    WaitingForChecksumFragment, GatewayMessage, NodeMessage, decode_gateway_message,
)
from scenario import DutyCycle, OtaDevice, Scenario


class UnicastNode(OtaDevice):

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
        self.binary_fragments = 0
        self.checksum_fragments = 0
        self.binary = bytearray()
        self.checksum = bytearray()
        self.binary_complete = False
        self.checksum_complete = False
        self.checksum_valid = False

    async def run(self) -> None:
        while True:
            try:
                packet = await self.radio.receive_data_wait()
            except asyncio.TimeoutError:
                return  # Simulation is over.

            frame = self.decode_frame(packet.payload)
            if frame.destination != self.address:
                continue

            message = decode_gateway_message(frame.payload)
            self.frames_handled += 1
            self.logger.debug(f"{sim.current_time():.3f} node {self.address} got {message!r:.60}")

            await sim.sleep(self.scenario.processing_delay)
            reply = await self.handle(message)
            if reply is not None:
                await self.transmit(frame.source, reply.to_bytes())

    async def handle(self, message: GatewayMessage) -> NodeMessage | None:
        match message:
            case ResetRequest():
                self._reset_update_state()
                return ResetConfirmed(firmware_version=self.firmware_version)

            case BinaryFragment(number=number, data=data, last=last):
                # Stop and wait: only the fragment we are waiting for is stored, anything else
                # (usually a retransmission after a lost acknowledgement) just gets the same
                # answer again.
                if number == self.binary_fragments and not self.binary_complete:
                    self.binary += data
                    self.binary_fragments += 1
                    self.binary_complete = last
                return WaitingForBinaryFragment(expected=self.binary_fragments)

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

        raise ValueError(f"Node cannot handle {message!r}")

    def _verify_checksum(self) -> None:
        self.checksum_valid = hashlib.sha256(self.binary).digest() == bytes(self.checksum)

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
        self.firmware_version = self.binary[0]
        self.flash_count += 1
        self._reset_update_state()
        self.logger.info(
            f"{sim.current_time():.3f} node {self.address} rebooted into version "
            f"{self.firmware_version}"
        )
