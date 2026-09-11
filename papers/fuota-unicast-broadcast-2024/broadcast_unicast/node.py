"""
End node of the *broadcast + unicast* method (section 3.2 of the paper), also used unchanged by
the *only broadcast* method.

The node keeps a bitmap of the chunks it has received. It stores any fragment of the announced
binary it hears, whether it was broadcast or sent to it in unicast, and only acknowledges the
unicast ones.
"""
from frames import (
    AllChunksReceived, BinaryFragment, BinaryFragmentAck, Bitmap, BroadcastStartConfirmation,
    BroadcastStartRequest, SendMeTheBitmap, MiWiFrame, GatewayMessage, NodeMessage,
)
from node_base import OtaNode


class BroadcastNode(OtaNode):

    @property
    def binary_complete(self) -> bool:
        return bool(self.chunks) and all(chunk is not None for chunk in self.chunks)

    async def handle_update_message(
        self, frame: MiWiFrame, message: GatewayMessage,
    ) -> NodeMessage | None:
        match message:
            case BroadcastStartRequest(chunks=chunks):
                self._reset_update_state()
                self.chunks = [None] * chunks
                return BroadcastStartConfirmation(firmware_version=self.firmware_version)

            case BinaryFragment(number=number, data=data):
                # Chunks are stored by their number, repeats (from later broadcast rounds or
                # repairs meant for other nodes) are simply ignored.
                if number < len(self.chunks) and self.chunks[number] is None:
                    self.chunks[number] = data
                if frame.broadcast:
                    return None
                return BinaryFragmentAck(number=number)

            case SendMeTheBitmap():
                if self.binary_complete:
                    return AllChunksReceived()
                return Bitmap(received=[chunk is not None for chunk in self.chunks])

        raise ValueError(f"Node cannot handle {message!r}")
