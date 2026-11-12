"""
    On-air frames of the EWS radio protocol, as far as the FUOTA exchange needs them.

    Only the sizes and the handful of fields the two sides act on are modelled. Every frame starts
    with the 3 byte protocol header; there is no encryption, MIC or application CRC.
"""
from dataclasses import dataclass

HEADER_LEN = 3
MAX_FRAME_LEN = 255
MP_HEADER_LEN = 2  # PacketID, u16 little endian
MP_MAX_DATA = MAX_FRAME_LEN - HEADER_LEN - MP_HEADER_LEN  # 250
MP_MISSING_MAX_IDS = 125

CMD_SET_PING_SCHEDULE = 0x10
SCHEDULE_CONFIG_LEN = 7

CMD_MP_START = 0xB0
CMD_MP_HIGHEST = 0xB1
CMD_MP_MISSING = 0xB2
CMD_MP_CANCEL = 0xBC

_NAMES = {
    CMD_SET_PING_SCHEDULE: "ACK+SET_PING_SCHED",
    CMD_MP_START: "MP_START",
    CMD_MP_HIGHEST: "MP_HIGHEST",
    CMD_MP_MISSING: "MP_MISSING",
    CMD_MP_CANCEL: "MP_CANCEL",
}


@dataclass
class Frame:
    net_id: int
    downlink: bool = False
    multipacket: bool = False
    no_ack: bool = False
    retry: bool = False
    body: bytes = b""

    def encode(self) -> bytes:
        flags = (self.retry << 3) | (self.downlink << 2) | (self.multipacket << 1) | self.no_ack
        header = bytes([
            self.net_id & 0x3F,  # protocol version 0 in the two top bits
            (self.net_id >> 6) & 0xFF,
            (((self.net_id >> 14) & 0x0F) << 4) | flags,
        ])
        return header + self.body

    @staticmethod
    def decode(data: bytes) -> "Frame":
        assert len(data) >= HEADER_LEN, "Frame is shorter than the protocol header"
        net_id = (data[0] & 0x3F) | (data[1] << 6) | ((data[2] >> 4) << 14)
        return Frame(
            net_id=net_id,
            retry=bool(data[2] & 0x08),
            downlink=bool(data[2] & 0x04),
            multipacket=bool(data[2] & 0x02),
            no_ack=bool(data[2] & 0x01),
            body=data[HEADER_LEN:],
        )

    @property
    def name(self) -> str:
        if self.multipacket:
            return "FRAGMENT"
        if not self.body:
            return "ACK" if self.no_ack else "PING"
        return _NAMES.get(self.body[0], "UNKNOWN")

    @property
    def command(self) -> int | None:
        """ The command byte of a non-fragment frame that has one. """
        if self.multipacket or not self.body:
            return None
        return self.body[0]

    @property
    def u16(self) -> int:
        """ TotalPackets of MP_START, HighestID of MP_HIGHEST, PacketID of a fragment. """
        offset = 0 if self.multipacket else 1
        return int.from_bytes(self.body[offset:offset + 2], "little")

    @property
    def schedule_start(self) -> float:
        """ Seconds from 00:00 at which the schedule of SET_PING_SCHEDULE starts. """
        bits = int.from_bytes(self.body[1:1 + SCHEDULE_CONFIG_LEN], "little")
        return ((bits >> 1) & 0xF_FFFF) / 10

    @property
    def missing_ids(self) -> list[int]:
        ids = self.body[1:]
        return [int.from_bytes(ids[i:i + 2], "little") for i in range(0, len(ids), 2)]


def ping(net_id: int, retry: bool = False) -> Frame:
    return Frame(net_id, retry=retry)


def uplink_ack(net_id: int) -> Frame:
    """ Sensor's bare ACK: accepts MP_START and acknowledges a complete burst. """
    return Frame(net_id, no_ack=True)


def mp_missing(net_id: int, ids: list[int]) -> Frame:
    assert 0 < len(ids) <= MP_MISSING_MAX_IDS
    body = bytes([CMD_MP_MISSING]) + b"".join(i.to_bytes(2, "little") for i in ids)
    return Frame(net_id, no_ack=True, body=body)


def downlink_ack(net_id: int) -> Frame:
    return Frame(net_id, downlink=True, no_ack=True)


def set_ping_schedule(net_id: int, start: float, interval: float) -> Frame:
    """
        The ACK of a ping carrying one command: a new ping schedule (ScheduleConfig, 56 bits),
        enabled all day. The sensor confirms it with a bare ACK.
    """
    bits = 1 | (round(start * 10) & 0xF_FFFF) << 1 | 8639 << 21 | (round(interval / 10) & 0x3FFF) << 35
    body = bytes([CMD_SET_PING_SCHEDULE]) + bits.to_bytes(8, "little")[:SCHEDULE_CONFIG_LEN]
    return Frame(net_id, downlink=True, body=body)


def mp_start(net_id: int, total_packets: int) -> Frame:
    body = bytes([CMD_MP_START]) + total_packets.to_bytes(2, "little")
    return Frame(net_id, downlink=True, body=body)


def mp_highest(net_id: int, highest_id: int) -> Frame:
    body = bytes([CMD_MP_HIGHEST]) + highest_id.to_bytes(2, "little")
    return Frame(net_id, downlink=True, body=body)


def mp_cancel(net_id: int) -> Frame:
    return Frame(net_id, downlink=True, body=bytes([CMD_MP_CANCEL]))


def fragment(net_id: int, packet_id: int, data_len: int, last_of_burst: bool) -> Frame:
    """ Only the last fragment of a burst asks for an answer (no_ack = 0). """
    assert 0 < data_len <= MP_MAX_DATA
    body = packet_id.to_bytes(2, "little") + bytes(data_len)
    return Frame(net_id, downlink=True, multipacket=True, no_ack=not last_of_burst, body=body)
