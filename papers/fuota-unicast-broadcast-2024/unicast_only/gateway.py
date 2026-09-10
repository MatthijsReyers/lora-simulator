"""
Gateway of the *only unicast* method (section 3.1 and Figure 1 of the paper).

The gateway updates the nodes one after the other. Every frame it sends is acknowledged by the
node and it does not move on to the next fragment (or the next node) before that acknowledgement
arrived, retransmitting on a timeout ("stop and wait").
"""
import asyncio, hashlib
from dataclasses import dataclass
from typing import Type, TypeVar

from simulator.environment import simulation_env as sim

from frames import (
    GATEWAY_ADDRESS,
    BinaryFragment, ChecksumFragment, FlashNewVersion, HandshakeRequest, HandshakeResponse,
    ResetConfirmed, ResetRequest, StatusRequest, StatusResponse, WaitingForBinaryFragment,
    WaitingForChecksumFragment, GatewayMessage, NodeMessage, decode_node_message,
)
from scenario import DutyCycle, OtaDevice, Scenario


# Slack on top of the computed reply deadline for radio turnaround times and the like.
REPLY_GUARD = 0.1

Reply = TypeVar("Reply", bound=NodeMessage)


class UpdateFailed(Exception):
    """ Raised when a stage of the update did not end in the expected node state. """


class NodeUnreachable(UpdateFailed):
    """ Raised when a node did not answer `max_attempts` transmissions of the same frame. """


@dataclass
class NodeResult:
    """ Timing and statistics of the update of a single node. """
    address: int
    distance: float
    start: float
    binary_start: float | None = None
    binary_end: float | None = None
    end: float | None = None
    frames_sent: int = 0
    retransmissions: int = 0
    restarts: int = 0
    updated: bool = False

    @property
    def binary_time(self) -> float | None:
        """ Duration of the *sending of the binary* stage, the metric used by the paper. """
        if self.binary_start is None or self.binary_end is None:
            return None
        return self.binary_end - self.binary_start

    @property
    def total_time(self) -> float | None:
        if self.end is None:
            return None
        return self.end - self.start


class UnicastGateway(OtaDevice):

    def __init__(
        self,
        nodes: dict[int, float],
        firmware: bytes,
        scenario: Scenario,
        duty_cycle: DutyCycle,
        max_attempts: int = 10_000,
        max_restarts: int = 3,
    ):
        """
            :param nodes: The addresses of the nodes to update mapped to their distance to the
                gateway (only used for reporting).
            :param firmware: Image to distribute, its first byte is the version number.
            :param max_attempts: Transmissions of one frame before a node is given up on.
            :param max_restarts: How often the whole procedure is restarted for a node whose
                status did not check out (e.g. a wrong checksum) before giving up on it.
        """
        super().__init__(GATEWAY_ADDRESS, (0.0, 0.0), scenario, duty_cycle)
        self.nodes = nodes
        self.firmware = firmware
        self.new_version = firmware[0]
        self.max_attempts = max_attempts
        self.max_restarts = max_restarts

        size = scenario.chunk_size
        self.chunks = [firmware[i:i + size] for i in range(0, len(firmware), size)]
        checksum = hashlib.sha256(firmware).digest()
        self.checksum_chunks = [checksum[i:i + size] for i in range(0, len(checksum), size)]

        self.results: dict[int, NodeResult] = {}
        self.start_time: float | None = None
        self.finish_time: float | None = None
        sim.create_task(self.run(), name="gateway")

    # ── Top level ───────────────────────────────────────────────────────────

    async def run(self) -> None:
        # Give the nodes a moment to boot and put their radios in receive mode.
        await sim.sleep(1.0)
        self.start_time = sim.current_time()
        self.logger.info(
            f"updating {len(self.nodes)} nodes with {len(self.chunks)} fragments of "
            f"{self.scenario.chunk_size} bytes"
        )
        for address in self.nodes:
            await self.update_node(address)
        self.finish_time = sim.current_time()
        self.logger.info(f"{self.finish_time:.1f} all nodes done")

    async def update_node(self, address: int) -> NodeResult:
        result = NodeResult(
            address=address, distance=self.nodes[address], start=sim.current_time(),
        )
        self.results[address] = result
        for restart in range(self.max_restarts + 1):
            result.restarts = restart
            try:
                await self._update_node_once(address, result)
                result.updated = True
                break
            except NodeUnreachable:
                self.logger.error(f"{sim.current_time():.1f} node {address} is unreachable")
                break
            except UpdateFailed as e:
                self.logger.warning(f"{sim.current_time():.1f} node {address}: {e}, restarting")
        result.end = sim.current_time()
        self.logger.info(
            f"{result.end:.1f} node {address} ({result.distance:.0f}m) "
            f"{'updated' if result.updated else 'FAILED'} in {result.total_time:.1f}s "
            f"(binary stage {result.binary_time or float('nan'):.1f}s, "
            f"{result.retransmissions} retransmissions)"
        )
        return result

    async def _update_node_once(self, address: int, result: NodeResult) -> None:
        # (a) Initial stage
        confirmed = await self.exchange(address, ResetRequest(), ResetConfirmed, result)
        if confirmed.firmware_version == self.new_version:
            self.logger.info(f"node {address} already runs version {self.new_version}")
            return

        # (b) Sending of the binary
        result.binary_start = sim.current_time()
        await self._send_fragments(address, self.chunks, BinaryFragment, WaitingForBinaryFragment, result)
        status = await self.exchange(address, StatusRequest(), StatusResponse, result)
        result.binary_end = sim.current_time()
        if status.binary_fragments != len(self.chunks):
            raise UpdateFailed(
                f"node reports {status.binary_fragments}/{len(self.chunks)} binary fragments"
            )

        # (c) Sending of the checksum
        await self._send_fragments(
            address, self.checksum_chunks, ChecksumFragment, WaitingForChecksumFragment, result,
        )
        status = await self.exchange(address, StatusRequest(), StatusResponse, result)
        if status.checksum_fragments != len(self.checksum_chunks):
            raise UpdateFailed(
                f"node reports {status.checksum_fragments}/{len(self.checksum_chunks)} "
                "checksum fragments"
            )

        # (d) Checksum validation and firmware flashing
        await sim.sleep(self.scenario.checksum_delay)
        status = await self.exchange(address, StatusRequest(), StatusResponse, result)
        if not status.checksum_valid:
            raise UpdateFailed("checksum verification failed")

        for _attempt in range(self.max_attempts):
            await self.transmit(address, FlashNewVersion().to_bytes())
            result.frames_sent += 1

            # (e) Final confirmation, with some slack on top of the reboot time so the first
            # handshake does not land while the node's radio is still coming up.
            await sim.sleep(self.scenario.reboot_delay + 1.0)
            await self.exchange(address, HandshakeRequest(), HandshakeResponse, result)
            status = await self.exchange(address, StatusRequest(), StatusResponse, result)
            if status.firmware_version == self.new_version:
                return
            # The flash command is the only frame without an acknowledgement. When it got lost
            # the node still holds the verified image, so just ask again instead of starting all
            # over (the paper does not say what happens in this case).
            if not status.checksum_valid:
                raise UpdateFailed(f"node still runs version {status.firmware_version}")
            result.retransmissions += 1
            self.logger.debug(f"{sim.current_time():.3f} node {address} did not flash, asking again")

        raise NodeUnreachable(f"node {address} ignored {self.max_attempts} flash commands")

    async def _send_fragments(
        self,
        address: int,
        chunks: list[bytes],
        fragment_type: Type[BinaryFragment] | Type[ChecksumFragment],
        ack_type: Type[WaitingForBinaryFragment] | Type[WaitingForChecksumFragment],
        result: NodeResult,
    ) -> None:
        """
            Sends the chunks one by one, every acknowledgement tells which fragment the node
            wants next so a lost acknowledgement simply results in a retransmission.
        """
        next_fragment = 0
        while next_fragment < len(chunks):
            fragment = fragment_type(
                number=next_fragment,
                data=chunks[next_fragment],
                last=(next_fragment == len(chunks) - 1),
            )
            ack = await self.exchange(address, fragment, ack_type, result)
            if ack.expected <= next_fragment:
                result.retransmissions += 1
            next_fragment = ack.expected

    # ── Stop and wait primitives ────────────────────────────────────────────

    async def exchange(
        self,
        address: int,
        message: GatewayMessage,
        reply_type: Type[Reply],
        result: NodeResult,
    ) -> Reply:
        """
            Transmits `message` to the node until a reply of `reply_type` arrives, or the node
            is declared unreachable.
        """
        payload = message.to_bytes()
        reply_airtime = self.scenario.airtime(self.scenario.header_len + reply_type.LENGTH)

        for _attempt in range(self.max_attempts):
            airtime = await self.transmit(address, payload)
            result.frames_sent += 1
            timeout = self.reply_timeout(airtime, reply_airtime)
            reply = await self.wait_for_reply(address, reply_type, timeout)
            if reply is not None:
                return reply
            result.retransmissions += 1
            self.logger.debug(
                f"{sim.current_time():.3f} no {reply_type.__name__} from node {address}, "
                f"retransmitting {type(message).__name__}"
            )

        raise NodeUnreachable(f"node {address} did not answer {self.max_attempts} transmissions")

    def reply_timeout(self, sent_airtime: float, reply_airtime: float) -> float:
        """
            How long to wait for the node's answer after our own transmission ended. In the
            paper's shared duty cycle model the node first has to wait out the quiet time of the
            frame it just received; with per-device duty cycles it may at worst still be waiting
            out the quiet time of its previous (equally sized) reply.
        """
        if self.duty_cycle.shared:
            wait = self.duty_cycle.quiet_time(sent_airtime)
        else:
            wait = self.duty_cycle.quiet_time(reply_airtime)
        return wait + self.scenario.processing_delay + reply_airtime + REPLY_GUARD

    async def wait_for_reply(
        self, address: int, reply_type: Type[Reply], timeout: float,
    ) -> Reply | None:
        """ Waits for a frame of `reply_type` from `address`, returns None on a timeout. """
        deadline = sim.current_time() + timeout
        while sim.current_time() < deadline:
            try:
                packet = await self.radio.receive_data_within(deadline - sim.current_time())
            except asyncio.TimeoutError:
                return None
            frame = self.decode_frame(packet.payload)
            if frame.destination != self.address or frame.source != address:
                continue
            message = decode_node_message(frame.payload)
            if isinstance(message, reply_type):
                return message
            self.logger.debug(f"{sim.current_time():.3f} ignoring stale {message!r}")
        return None
