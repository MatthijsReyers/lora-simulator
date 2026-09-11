"""
End node of the *only unicast* method (section 3.1 of the paper).

The node is a passive party: it keeps its radio in receive mode, ignores frames that are not
addressed to it and answers every frame from the gateway according to its state, which consists
of the number of received binary fragments (`bin_frags` in the paper), the number of received
checksum fragments (`check_frags`) and the running firmware version.
"""
from frames import (
    BinaryFragment, ResetConfirmed, ResetRequest, WaitingForBinaryFragment, MiWiFrame,
    GatewayMessage, NodeMessage,
)
from node_base import OtaNode


class UnicastNode(OtaNode):

    def _reset_update_state(self) -> None:
        super()._reset_update_state()
        self._received_last_fragment = False

    @property
    def binary_complete(self) -> bool:
        return self._received_last_fragment

    async def handle_update_message(
        self, frame: MiWiFrame, message: GatewayMessage,
    ) -> NodeMessage | None:
        match message:
            case ResetRequest():
                self._reset_update_state()
                return ResetConfirmed(firmware_version=self.firmware_version)

            case BinaryFragment(number=number, data=data, last=last):
                # Stop and wait: only the fragment we are waiting for is stored, anything else
                # (usually a retransmission after a lost acknowledgement) just gets the same
                # answer again.
                if number == len(self.chunks) and not self.binary_complete:
                    self.chunks.append(data)
                    self._received_last_fragment = last
                return WaitingForBinaryFragment(expected=len(self.chunks))

        raise ValueError(f"Node cannot handle {message!r}")
