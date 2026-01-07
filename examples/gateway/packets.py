from dataclasses import dataclass
from enum import Enum

def packet_from_bytes(data: bytes):
    pkt_type = data[0]
    if pkt_type == DemoPacketType.ENROLL_REQUEST.value:
        return EnrollmentRequestPacket.from_bytes(data)
    elif pkt_type == DemoPacketType.ENROLL_RESPONSE.value:
        return EnrollmentResponsePacket.from_bytes(data)
    elif pkt_type == DemoPacketType.DATA.value:
        return DataPacket.from_bytes(data)
    else:
        raise ValueError("Unknown packet type")


class DemoPacketType(Enum):
    ENROLL_REQUEST = 1
    ENROLL_RESPONSE = 2
    DATA = 3


@dataclass
class EnrollmentRequestPacket():
    hardware_id: int
    pkt_type: DemoPacketType = DemoPacketType.ENROLL_REQUEST

    @classmethod
    def from_bytes(cls, data: bytes) -> 'EnrollmentRequestPacket':
        assert data[0] == cls.pkt_type.value, "Not an EnrollmentRequestPacket"
        hardware_id = int.from_bytes(data[1:5], byteorder='big')
        return cls(hardware_id=hardware_id)
    
    def to_bytes(self) -> bytes:
        data = bytearray()
        data.append(self.pkt_type.value)
        data.extend(self.hardware_id.to_bytes(4, byteorder='big'))
        return bytes(data)


@dataclass
class EnrollmentResponsePacket():
    node_id: int
    hardware_id: int
    pkt_type: DemoPacketType = DemoPacketType.ENROLL_RESPONSE

    @classmethod
    def from_bytes(cls, data: bytes) -> 'EnrollmentResponsePacket':
        assert data[0] == cls.pkt_type.value, "Not an EnrollmentResponsePacket"
        node_id = int.from_bytes(data[1:5], byteorder='big')
        hardware_id = int.from_bytes(data[5:9], byteorder='big')
        return cls(node_id=node_id, hardware_id=hardware_id)
    
    def to_bytes(self) -> bytes:
        data = bytearray()
        data.append(self.pkt_type.value)
        data.extend(self.node_id.to_bytes(4, byteorder='big'))
        data.extend(self.hardware_id.to_bytes(4, byteorder='big'))
        return bytes(data)


@dataclass
class DataPacket():
    node_id: int
    payload: bytes
    pkt_type: DemoPacketType = DemoPacketType.DATA

    @classmethod
    def from_bytes(cls, data: bytes) -> 'DataPacket':
        assert data[0] == cls.pkt_type.value, "Not an DataPacket"
        node_id = int.from_bytes(data[1:5], byteorder='big')
        payload = data[5:]
        return cls(node_id=node_id, payload=payload)
    
    def to_bytes(self) -> bytes:
        data = bytearray()
        data.append(self.pkt_type.value)
        data.extend(self.node_id.to_bytes(4, byteorder='big'))
        data.extend(self.payload)
        return bytes(data)