"""
Gateway of the *only broadcast* method (section 3.3 and Figure 3 of the paper).

Identical to the broadcast + unicast method except for the delivery of the pending chunks: the
chunks a node is missing are broadcast (so the other nodes can pick them up as well) instead of
sent in acknowledged unicast frames, and the node is asked for its bitmap again until it reports
that all chunks were received.
"""
from broadcast_unicast.gateway import BroadcastGateway
from gateway_base import NodeResult, NodeUnreachable
from scenario import DutyCycle, Scenario


class BroadcastOnlyGateway(BroadcastGateway):

    def __init__(
        self,
        nodes: dict[int, float],
        firmware: bytes,
        scenario: Scenario,
        duty_cycle: DutyCycle,
        rounds: int = 1,
        max_repair_rounds: int = 1_000,
        **kwargs,
    ):
        """ :param max_repair_rounds: Bitmap/broadcast repair rounds before giving up a node. """
        super().__init__(nodes, firmware, scenario, duty_cycle, rounds=rounds, **kwargs)
        self.max_repair_rounds = max_repair_rounds

    async def _update_node_once(self, address: int, result: NodeResult) -> None:
        # (c) Sending of the pending chunks, in broadcast frames until the node has them all.
        for _round in range(self.max_repair_rounds):
            missing = await self.request_bitmap(address, result)
            if not missing:
                break
            result.repair_rounds += 1
            for number in missing:
                await self.broadcast(self.fragment(number))
                result.frames_sent += 1
        else:
            raise NodeUnreachable(
                f"node {address} still misses chunks after {self.max_repair_rounds} repair rounds"
            )
        await self._complete_binary(address, result)
