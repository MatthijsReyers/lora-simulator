"""
Gateway of the *broadcast + unicast* method (section 3.2 and Figure 2 of the paper).

The update starts with a unicast announcement to every node, then the whole binary is sent in
broadcast frames (one or more rounds, nobody acknowledges anything), and finally every node is
asked for a bitmap of the chunks it received and the missing ones are delivered in unicast with
the stop and wait procedure of the only unicast method. The checksum, flashing and confirmation
stages are the same as for the only unicast method.
"""
from simulator.environment import simulation_env as sim

from frames import (
    AllChunksReceived, BinaryFragmentAck, Bitmap, BroadcastStartConfirmation,
    BroadcastStartRequest, SendMeTheBitmap, StatusRequest, StatusResponse,
)
from gateway_base import NodeResult, NodeUnreachable, StopAndWaitGateway, UpdateFailed
from scenario import DutyCycle, Scenario


class BroadcastGateway(StopAndWaitGateway):
    """
        Shared driver of the two broadcast methods: stage (a), the announcement, and stage (b),
        the broadcast rounds, are identical for both. Subclasses implement `_update_node_once`
        for the delivery of the pending chunks (stage (c)).
    """

    def __init__(
        self,
        nodes: dict[int, float],
        firmware: bytes,
        scenario: Scenario,
        duty_cycle: DutyCycle,
        rounds: int = 1,
        **kwargs,
    ):
        """ :param rounds: `B` in the paper, how often the whole binary is broadcast. """
        super().__init__(nodes, firmware, scenario, duty_cycle, **kwargs)
        assert rounds >= 0, "Number of broadcast rounds cannot be negative"
        self.rounds = rounds
        self.broadcast_start: float | None = None
        self.broadcast_end: float | None = None

    async def run(self) -> None:
        # Give the nodes a moment to boot and put their radios in receive mode.
        await sim.sleep(1.0)
        self.start_time = sim.current_time()
        self.logger.info(
            f"updating {len(self.nodes)} nodes with {len(self.chunks)} fragments of "
            f"{self.scenario.chunk_size} bytes, {self.rounds} broadcast round(s)"
        )
        pending = await self._announce()
        await self._broadcast_rounds(pending)
        for address in pending:
            await self.update_node(address)
        self.finish_time = sim.current_time()
        self.logger.info(f"{self.finish_time:.1f} all nodes done")

    async def _announce(self) -> list[int]:
        """
            Stage (a): tells every node (in unicast) that an update is coming so it can clear
            its bitmap. Returns the nodes that need the update and answered.
        """
        pending = []
        request = BroadcastStartRequest(
            firmware_version=self.new_version,
            chunks=len(self.chunks),
            chunk_size=self.scenario.chunk_size,
        )
        for address in self.nodes:
            result = self.new_result(address)
            try:
                confirmation = await self.exchange(
                    address, request, BroadcastStartConfirmation, result,
                )
            except NodeUnreachable:
                self.logger.error(f"{sim.current_time():.1f} node {address} is unreachable")
                result.end = sim.current_time()
                continue
            if confirmation.firmware_version == self.new_version:
                self.logger.info(f"node {address} already runs version {self.new_version}")
                result.updated = True
                result.end = sim.current_time()
                continue
            pending.append(address)
        return pending

    async def _broadcast_rounds(self, pending: list[int]) -> None:
        """ Stage (b): the whole binary in broadcast frames, `rounds` times. """
        self.broadcast_start = sim.current_time()
        for address in pending:
            self.results[address].binary_start = self.broadcast_start
        if pending:
            for _round in range(self.rounds):
                for number in range(len(self.chunks)):
                    await self.broadcast(self.fragment(number))
        self.broadcast_end = sim.current_time()
        self.logger.info(
            f"{self.broadcast_end:.1f} broadcast stage took "
            f"{self.broadcast_end - self.broadcast_start:.1f}s"
        )

    async def request_bitmap(self, address: int, result: NodeResult) -> list[int]:
        """ Asks a node which chunks it is still missing. """
        reply = await self.exchange(
            address, SendMeTheBitmap(), (Bitmap, AllChunksReceived), result,
            reply_len=Bitmap.length_for(len(self.chunks)),
        )
        missing = [] if isinstance(reply, AllChunksReceived) else reply.missing(len(self.chunks))
        if result.missing_after_broadcast is None:
            result.missing_after_broadcast = len(missing)
        return missing

    async def _complete_binary(self, address: int, result: NodeResult) -> None:
        """ Confirms the node has the whole binary, then runs the shared final stages. """
        status = await self.exchange(address, StatusRequest(), StatusResponse, result)
        result.binary_end = sim.current_time()
        if status.binary_fragments != len(self.chunks):
            raise UpdateFailed(
                f"node reports {status.binary_fragments}/{len(self.chunks)} binary fragments"
            )
        await self._finish_update(address, result)


class BroadcastUnicastGateway(BroadcastGateway):

    async def _update_node_once(self, address: int, result: NodeResult) -> None:
        # (c) Sending of the pending chunks, in unicast with stop and wait.
        for number in await self.request_bitmap(address, result):
            await self.exchange(
                address, self.fragment(number), BinaryFragmentAck, result,
                accept=lambda ack, number=number: ack.number == number,
            )
        await self._complete_binary(address, result)
