"""
Gateway of the *only unicast* method (section 3.1 and Figure 1 of the paper).

The gateway updates the nodes one after the other. Every frame it sends is acknowledged by the
node and it does not move on to the next fragment (or the next node) before that acknowledgement
arrived, retransmitting on a timeout ("stop and wait").
"""
from simulator.environment import simulation_env as sim

from frames import (
    BinaryFragment, ResetConfirmed, ResetRequest, StatusRequest, StatusResponse,
    WaitingForBinaryFragment,
)
from gateway_base import NodeResult, NodeUnreachable, StopAndWaitGateway, UpdateFailed

__all__ = ["UnicastGateway", "NodeResult", "NodeUnreachable", "UpdateFailed"]


class UnicastGateway(StopAndWaitGateway):

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

    async def _update_node_once(self, address: int, result: NodeResult) -> None:
        # (a) Initial stage
        confirmed = await self.exchange(address, ResetRequest(), ResetConfirmed, result)
        if confirmed.firmware_version == self.new_version:
            self.logger.info(f"node {address} already runs version {self.new_version}")
            return

        # (b) Sending of the binary
        result.binary_start = sim.current_time()
        await self._send_fragments(
            address, self.chunks, BinaryFragment, WaitingForBinaryFragment, result,
        )
        status = await self.exchange(address, StatusRequest(), StatusResponse, result)
        result.binary_end = sim.current_time()
        if status.binary_fragments != len(self.chunks):
            raise UpdateFailed(
                f"node reports {status.binary_fragments}/{len(self.chunks)} binary fragments"
            )

        # (c) to (e)
        await self._finish_update(address, result)
