"""
Frame formats of the MiWi based OTA update protocol, see Appendix A of the paper.

A frame consists of a MiWi header (frame control, sequence number, PAN id, destination and source
address), the OTA payload and a frame check sequence. The first byte of every OTA payload is a
TYPE field, note that the gateway and the nodes use two different numberings for it, so a payload
can only be decoded when it is known who sent it (which the source address of the frame tells).
"""
from dataclasses import dataclass
from enum import IntEnum


GATEWAY_ADDRESS = 0x0000000000000000
BROADCAST_ADDRESS = 0xFFFF
PAN_ID = 0x1234

# Frame control bit that selects the 2 byte (broadcast) destination address mode.
FC_SHORT_DESTINATION = 0x0001

# Frame control (2) + sequence number (1) + PAN id (2) + destination (8|2) + source (8) + FCS (2)
UNICAST_HEADER_LEN = 23
BROADCAST_HEADER_LEN = 17

# The ns-3 simulations of the paper evidently did not put the MiWi header on the air (see the
# README), the "compact" frame encoding mimics that with a one byte source and destination.
COMPACT_HEADER_LEN = 2
COMPACT_BROADCAST_ADDRESS = 0xFF

# TYPE (1) + the widest fragment number we allow ourselves to use (2), a frame with a firmware
# chunk of `frame_len - UNICAST_HEADER_LEN - FRAGMENT_OVERHEAD` bytes always fits in `frame_len`.
FRAGMENT_OVERHEAD = 3


class GatewayMessageType(IntEnum):
    """ TYPE values of the payloads sent by the gateway (Appendix A.3 - A.5). """
    BINARY_FRAGMENT_1 = 0x01        # fragment number in 1 byte
    BINARY_FRAGMENT_2 = 0x02        # fragment number in 2 bytes
    BINARY_FRAGMENT_3 = 0x03        # fragment number in 3 bytes
    BINARY_LAST_FRAGMENT_1 = 0x04
    BINARY_LAST_FRAGMENT_2 = 0x05
    BINARY_LAST_FRAGMENT_3 = 0x06
    CHECKSUM_FRAGMENT = 0x07
    CHECKSUM_LAST_FRAGMENT = 0x08
    STATUS_REQUEST = 0x09
    FLASH_NEW_VERSION = 0x0A
    RESET_REQUEST = 0x0B
    # Not in the appendix, the MiWi association handshake the gateway uses in the final stage.
    HANDSHAKE_REQUEST = 0x0C
    # Messages of the broadcast methods (sections 3.2 and 3.3), the appendix only lists the
    # frames of the unicast implementation so these numbers are our own.
    BROADCAST_START_REQUEST = 0x0D
    SEND_ME_THE_BITMAP = 0x0E


class NodeMessageType(IntEnum):
    """ TYPE values of the payloads sent by the nodes (Appendix A.6 and section 3.1). """
    WAITING_FOR_BINARY_FRAGMENT = 0x01
    BINARY_FRAGMENT_ACK = 0x02
    WAITING_FOR_CHECKSUM_FRAGMENT = 0x07
    STATUS_RESPONSE = 0x09
    RESET_CONFIRMED = 0x0B
    HANDSHAKE_RESPONSE = 0x0C
    BROADCAST_START_CONFIRMATION = 0x0D
    BITMAP = 0x0E
    ALL_CHUNKS_RECEIVED = 0x0F


@dataclass
class MiWiFrame:
    source: int
    destination: int
    payload: bytes
    sequence: int = 0

    @property
    def broadcast(self) -> bool:
        return self.destination == BROADCAST_ADDRESS

    def to_bytes(self, compact: bool = False) -> bytes:
        if compact:
            destination = COMPACT_BROADCAST_ADDRESS if self.broadcast else self.destination
            return bytes([self.source, destination]) + self.payload

        frame_control = FC_SHORT_DESTINATION if self.broadcast else 0
        out = bytearray()
        out += frame_control.to_bytes(2, 'big')
        out.append(self.sequence & 0xFF)
        out += PAN_ID.to_bytes(2, 'big')
        out += self.destination.to_bytes(2 if self.broadcast else 8, 'big')
        out += self.source.to_bytes(8, 'big')
        out += self.payload
        # The radio already models CRC failures for us, the FCS only takes up its two bytes.
        out += (sum(self.payload) & 0xFFFF).to_bytes(2, 'big')
        return bytes(out)

    @classmethod
    def from_bytes(cls, data: bytes, compact: bool = False) -> 'MiWiFrame':
        if compact:
            destination = BROADCAST_ADDRESS if data[1] == COMPACT_BROADCAST_ADDRESS else data[1]
            return cls(source=data[0], destination=destination, payload=data[2:])

        frame_control = int.from_bytes(data[0:2], 'big')
        sequence = data[2]
        assert int.from_bytes(data[3:5], 'big') == PAN_ID, "Frame from another PAN"
        if frame_control & FC_SHORT_DESTINATION:
            destination = int.from_bytes(data[5:7], 'big')
            offset = 7
        else:
            destination = int.from_bytes(data[5:13], 'big')
            offset = 13
        source = int.from_bytes(data[offset:offset + 8], 'big')
        payload = data[offset + 8:-2]
        return cls(source=source, destination=destination, payload=payload, sequence=sequence)


# ── Messages sent by the gateway ────────────────────────────────────────────

def fragment_number_len(number: int) -> int:
    """ The number of bytes the appendix uses to encode a given fragment number. """
    if number < 0x100:
        return 1
    if number < 0x10000:
        return 2
    return 3


@dataclass
class BinaryFragment:
    number: int
    data: bytes
    last: bool = False

    def to_bytes(self) -> bytes:
        width = fragment_number_len(self.number)
        base = GatewayMessageType.BINARY_LAST_FRAGMENT_1 if self.last else GatewayMessageType.BINARY_FRAGMENT_1
        return bytes([base + width - 1]) + self.number.to_bytes(width, 'big') + self.data

    @classmethod
    def from_bytes(cls, data: bytes) -> 'BinaryFragment':
        kind = GatewayMessageType(data[0])
        last = kind >= GatewayMessageType.BINARY_LAST_FRAGMENT_1
        base = GatewayMessageType.BINARY_LAST_FRAGMENT_1 if last else GatewayMessageType.BINARY_FRAGMENT_1
        width = kind - base + 1
        number = int.from_bytes(data[1:1 + width], 'big')
        return cls(number=number, data=bytes(data[1 + width:]), last=last)


@dataclass
class ChecksumFragment:
    number: int
    data: bytes
    last: bool = False

    def to_bytes(self) -> bytes:
        assert self.number < 0x100, "Checksum fragment numbers are a single byte"
        kind = GatewayMessageType.CHECKSUM_LAST_FRAGMENT if self.last else GatewayMessageType.CHECKSUM_FRAGMENT
        return bytes([kind, self.number]) + self.data

    @classmethod
    def from_bytes(cls, data: bytes) -> 'ChecksumFragment':
        last = data[0] == GatewayMessageType.CHECKSUM_LAST_FRAGMENT
        return cls(number=data[1], data=bytes(data[2:]), last=last)


@dataclass
class StatusRequest:
    def to_bytes(self) -> bytes:
        return bytes([GatewayMessageType.STATUS_REQUEST])


@dataclass
class FlashNewVersion:
    def to_bytes(self) -> bytes:
        return bytes([GatewayMessageType.FLASH_NEW_VERSION])


@dataclass
class ResetRequest:
    def to_bytes(self) -> bytes:
        return bytes([GatewayMessageType.RESET_REQUEST])


@dataclass
class HandshakeRequest:
    def to_bytes(self) -> bytes:
        return bytes([GatewayMessageType.HANDSHAKE_REQUEST])


@dataclass
class BroadcastStartRequest:
    """ Announces an update to a node: the new version and how the binary will be fragmented. """
    firmware_version: int
    chunks: int
    chunk_size: int

    def to_bytes(self) -> bytes:
        return (
            bytes([GatewayMessageType.BROADCAST_START_REQUEST, self.firmware_version]) +
            self.chunks.to_bytes(2, 'big') + self.chunk_size.to_bytes(2, 'big')
        )

    @classmethod
    def from_bytes(cls, data: bytes) -> 'BroadcastStartRequest':
        return cls(
            firmware_version=data[1],
            chunks=int.from_bytes(data[2:4], 'big'),
            chunk_size=int.from_bytes(data[4:6], 'big'),
        )


@dataclass
class SendMeTheBitmap:
    def to_bytes(self) -> bytes:
        return bytes([GatewayMessageType.SEND_ME_THE_BITMAP])


GatewayMessage = (
    BinaryFragment | ChecksumFragment | StatusRequest | FlashNewVersion | ResetRequest |
    HandshakeRequest | BroadcastStartRequest | SendMeTheBitmap
)


def decode_gateway_message(payload: bytes) -> GatewayMessage:
    kind = GatewayMessageType(payload[0])
    if GatewayMessageType.BINARY_FRAGMENT_1 <= kind <= GatewayMessageType.BINARY_LAST_FRAGMENT_3:
        return BinaryFragment.from_bytes(payload)
    if kind in (GatewayMessageType.CHECKSUM_FRAGMENT, GatewayMessageType.CHECKSUM_LAST_FRAGMENT):
        return ChecksumFragment.from_bytes(payload)
    if kind == GatewayMessageType.STATUS_REQUEST:
        return StatusRequest()
    if kind == GatewayMessageType.FLASH_NEW_VERSION:
        return FlashNewVersion()
    if kind == GatewayMessageType.RESET_REQUEST:
        return ResetRequest()
    if kind == GatewayMessageType.HANDSHAKE_REQUEST:
        return HandshakeRequest()
    if kind == GatewayMessageType.BROADCAST_START_REQUEST:
        return BroadcastStartRequest.from_bytes(payload)
    if kind == GatewayMessageType.SEND_ME_THE_BITMAP:
        return SendMeTheBitmap()
    raise ValueError(f"Unknown gateway message type {kind}")


# ── Messages sent by the nodes ──────────────────────────────────────────────
#
# Every node message has a fixed `LENGTH` (payload bytes) so the gateway can compute the airtime
# of the reply it is waiting for.

@dataclass
class WaitingForBinaryFragment:
    """ The acknowledgement of a binary fragment, tells the gateway which fragment to send next. """
    expected: int
    LENGTH = 3

    def to_bytes(self) -> bytes:
        return bytes([NodeMessageType.WAITING_FOR_BINARY_FRAGMENT]) + self.expected.to_bytes(2, 'big')

    @classmethod
    def from_bytes(cls, data: bytes) -> 'WaitingForBinaryFragment':
        return cls(expected=int.from_bytes(data[1:3], 'big'))


@dataclass
class WaitingForChecksumFragment:
    expected: int
    LENGTH = 2

    def to_bytes(self) -> bytes:
        return bytes([NodeMessageType.WAITING_FOR_CHECKSUM_FRAGMENT, self.expected])

    @classmethod
    def from_bytes(cls, data: bytes) -> 'WaitingForChecksumFragment':
        return cls(expected=data[1])


@dataclass
class StatusResponse:
    firmware_version: int
    binary_fragments: int
    checksum_fragments: int
    checksum_valid: bool
    LENGTH = 6

    def to_bytes(self) -> bytes:
        return (
            bytes([NodeMessageType.STATUS_RESPONSE, self.firmware_version]) +
            self.binary_fragments.to_bytes(2, 'big') +
            bytes([self.checksum_fragments, int(self.checksum_valid)])
        )

    @classmethod
    def from_bytes(cls, data: bytes) -> 'StatusResponse':
        return cls(
            firmware_version=data[1],
            binary_fragments=int.from_bytes(data[2:4], 'big'),
            checksum_fragments=data[4],
            checksum_valid=bool(data[5]),
        )


@dataclass
class ResetConfirmed:
    firmware_version: int
    LENGTH = 2

    def to_bytes(self) -> bytes:
        return bytes([NodeMessageType.RESET_CONFIRMED, self.firmware_version])

    @classmethod
    def from_bytes(cls, data: bytes) -> 'ResetConfirmed':
        return cls(firmware_version=data[1])


@dataclass
class HandshakeResponse:
    LENGTH = 1

    def to_bytes(self) -> bytes:
        return bytes([NodeMessageType.HANDSHAKE_RESPONSE])


@dataclass
class BinaryFragmentAck:
    """
        Acknowledgement of a unicast fragment in the broadcast methods, where the pending chunks
        are not sequential so the "expected next" style acknowledgement does not apply.
    """
    number: int
    LENGTH = 3

    def to_bytes(self) -> bytes:
        return bytes([NodeMessageType.BINARY_FRAGMENT_ACK]) + self.number.to_bytes(2, 'big')

    @classmethod
    def from_bytes(cls, data: bytes) -> 'BinaryFragmentAck':
        return cls(number=int.from_bytes(data[1:3], 'big'))


@dataclass
class BroadcastStartConfirmation:
    firmware_version: int
    LENGTH = 2

    def to_bytes(self) -> bytes:
        return bytes([NodeMessageType.BROADCAST_START_CONFIRMATION, self.firmware_version])

    @classmethod
    def from_bytes(cls, data: bytes) -> 'BroadcastStartConfirmation':
        return cls(firmware_version=data[1])


@dataclass
class Bitmap:
    """ One bit per chunk, set when the node has received it. """
    received: list[bool]

    @staticmethod
    def length_for(chunks: int) -> int:
        """ Payload length of the bitmap message for a binary of `chunks` fragments. """
        return 1 + (chunks + 7) // 8

    def to_bytes(self) -> bytes:
        out = bytearray(Bitmap.length_for(len(self.received)))
        out[0] = NodeMessageType.BITMAP
        for (i, got) in enumerate(self.received):
            if got:
                out[1 + i // 8] |= 1 << (i % 8)
        return bytes(out)

    @classmethod
    def from_bytes(cls, data: bytes) -> 'Bitmap':
        received = [bool(byte & (1 << bit)) for byte in data[1:] for bit in range(8)]
        return cls(received=received)

    def missing(self, chunks: int) -> list[int]:
        return [i for i in range(chunks) if not self.received[i]]


@dataclass
class AllChunksReceived:
    LENGTH = 1

    def to_bytes(self) -> bytes:
        return bytes([NodeMessageType.ALL_CHUNKS_RECEIVED])


NodeMessage = (
    WaitingForBinaryFragment | WaitingForChecksumFragment | StatusResponse | ResetConfirmed |
    HandshakeResponse | BinaryFragmentAck | BroadcastStartConfirmation | Bitmap |
    AllChunksReceived
)


def decode_node_message(payload: bytes) -> NodeMessage:
    kind = NodeMessageType(payload[0])
    if kind == NodeMessageType.WAITING_FOR_BINARY_FRAGMENT:
        return WaitingForBinaryFragment.from_bytes(payload)
    if kind == NodeMessageType.BINARY_FRAGMENT_ACK:
        return BinaryFragmentAck.from_bytes(payload)
    if kind == NodeMessageType.WAITING_FOR_CHECKSUM_FRAGMENT:
        return WaitingForChecksumFragment.from_bytes(payload)
    if kind == NodeMessageType.STATUS_RESPONSE:
        return StatusResponse.from_bytes(payload)
    if kind == NodeMessageType.RESET_CONFIRMED:
        return ResetConfirmed.from_bytes(payload)
    if kind == NodeMessageType.HANDSHAKE_RESPONSE:
        return HandshakeResponse()
    if kind == NodeMessageType.BROADCAST_START_CONFIRMATION:
        return BroadcastStartConfirmation.from_bytes(payload)
    if kind == NodeMessageType.BITMAP:
        return Bitmap.from_bytes(payload)
    if kind == NodeMessageType.ALL_CHUNKS_RECEIVED:
        return AllChunksReceived()
    raise ValueError(f"Unknown node message type {kind}")


def message_name(message: GatewayMessage | NodeMessage) -> str:
    """ Short name for logs and CSV output. """
    return type(message).__name__
