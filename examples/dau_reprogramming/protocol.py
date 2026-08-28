"""
Packet formats, timing helpers and tunable parameters for the DAU transport
protocol (see README.md for how this maps onto the paper).

All times that are put on the air are absolute simulation timestamps encoded as
unsigned 32 bit millisecond counters. Real hardware would derive these from the
LoRaWAN Class B beacon clock, here we simply assume the nodes are synchronised.
"""
from dataclasses import dataclass, field
from enum import IntEnum
import math

from simulator.lora.airtime import estimate_airtime, symbol_airtime
from simulator.lora.enums.spreading_factor import SpreadingFactor


# ── Tunable protocol parameters ─────────────────────────────────────────────

BANDWIDTH     = 125    # kHz, single frequency channel (the simulator PHY is
                       # single-frequency, see README for what that means for
                       # the "logical channels" of the paper)
CODE_RATE     = 5      # 4/5
PREAMBLE_LEN  = 8

CONTROL_SF    = 12     # SF used for session/group control traffic, this has to
                       # be the most robust SF since every node must hear it
GROUP_SFS     = [7, 8, 9, 10, 11, 12]  # candidate SF groups, low to high

FRAGMENT_SIZE   = 48   # firmware bytes per fragment
BLOCK_FRAGMENTS = 8    # K, source fragments per coding block
PARITY_PER_ROUND = 2   # R, coded (parity) fragments appended to each block pass

ANNOUNCE_REPEATS = 3   # how often the session announcement is repeated
GROUP_START_REPEATS = 2

MAX_BLOCK_ROUNDS = 6   # give up on a block after this many NACK/repair rounds
MAX_GROUP_ROUNDS = 4   # give up on a group after this many full passes

GUARD          = 0.020  # seconds of guard time between scheduled transmissions
NACK_GUARD     = 0.010  # extra guard time at the end of a NACK slot

NACK_PAYLOAD   = b"\x00"  # the NACK burst carries no information, only energy


# ── Airtime / slot helpers ──────────────────────────────────────────────────

def airtime(payload_len: int, sf: int) -> float:
    """ Time on air of a `payload_len` byte packet sent at spreading factor `sf`. """
    return estimate_airtime(
        payload_len=payload_len,
        bandwidth=BANDWIDTH,
        spreading_factor=sf,
        code_rate=CODE_RATE,
        preamble_len=PREAMBLE_LEN,
    )


def nack_slot_duration(sf: int) -> float:
    """
        Length of a single NACK logical channel slot. A slot has to be long
        enough for the whole NACK burst plus some guard time, the gateway only
        samples the first few symbols of it with CAD.
    """
    return airtime(len(NACK_PAYLOAD), sf) + NACK_GUARD


def cad_duration(sf: int) -> float:
    """ Time the radio spends doing a single channel activity detection. """
    return symbol_airtime(BANDWIDTH, sf) * 6


def to_ms(seconds: float) -> int:
    """ Absolute simulation time in seconds -> the u32 millisecond wire format. """
    return round(seconds * 1000)


def from_ms(milliseconds: int) -> float:
    """ The u32 millisecond wire format -> absolute simulation time in seconds. """
    return milliseconds / 1000.0


def sf_max_distance(sf: int, tx_power: int, noise_floor: float) -> float:
    """
        Largest distance at which a packet sent at `tx_power` dBm is still
        demodulable at spreading factor `sf`, using the simulator's default
        log-distance path loss model with exponent 2. Only used by main.py to
        place nodes in sensible spots, the protocol itself never uses this.
    """
    link_budget = tx_power - noise_floor - SpreadingFactor(sf).minimum_snr()
    return 10 ** (link_budget / 20.0)


# ── Wire format ─────────────────────────────────────────────────────────────

class DauPacketType(IntEnum):
    SESSION_ANNOUNCE = 1
    PROBE            = 2
    GROUP_START      = 3
    FRAGMENT         = 4
    NACK_WINDOW      = 5
    GROUP_STATUS     = 6


class FragmentKind(IntEnum):
    SOURCE = 0
    PARITY = 1


class NackScope(IntEnum):
    BLOCK = 0   # one logical channel per source fragment of a block
    GROUP = 1   # a single logical channel meaning "I am still incomplete"


@dataclass
class SessionAnnounce:
    """
        Broadcast at CONTROL_SF, this is the "metadata beacon" of the paper. It
        tells every node how the firmware image is chopped up and when the SF
        probing round starts.
    """
    session_id: int
    blob_len: int
    frag_size: int
    block_frags: int
    num_blocks: int
    probe_start_ms: int
    probe_slot_ms: int
    pkt_type: DauPacketType = DauPacketType.SESSION_ANNOUNCE

    def to_bytes(self) -> bytes:
        return (
            bytes([self.pkt_type])
            + self.session_id.to_bytes(2, "big")
            + self.blob_len.to_bytes(4, "big")
            + bytes([self.frag_size, self.block_frags])
            + self.num_blocks.to_bytes(2, "big")
            + self.probe_start_ms.to_bytes(4, "big")
            + self.probe_slot_ms.to_bytes(4, "big")
        )

    @classmethod
    def from_bytes(cls, data: bytes) -> "SessionAnnounce":
        assert data[0] == cls.pkt_type, "Not a SessionAnnounce"
        return cls(
            session_id=int.from_bytes(data[1:3], "big"),
            blob_len=int.from_bytes(data[3:7], "big"),
            frag_size=data[7],
            block_frags=data[8],
            num_blocks=int.from_bytes(data[9:11], "big"),
            probe_start_ms=int.from_bytes(data[11:15], "big"),
            probe_slot_ms=int.from_bytes(data[15:19], "big"),
        )


@dataclass
class Probe:
    """
        Sent once per candidate SF during the probing round. A node joins the
        group of the *lowest* SF whose probe it managed to demodulate.
    """
    session_id: int
    sf: int
    pkt_type: DauPacketType = DauPacketType.PROBE

    def to_bytes(self) -> bytes:
        return bytes([self.pkt_type]) + self.session_id.to_bytes(2, "big") + bytes([self.sf])

    @classmethod
    def from_bytes(cls, data: bytes) -> "Probe":
        assert data[0] == cls.pkt_type, "Not a Probe"
        return cls(session_id=int.from_bytes(data[1:3], "big"), sf=data[3])


@dataclass
class GroupStart:
    """
        Announces the dissemination round of one SF group. Sent at CONTROL_SF so
        that nodes of *every* group hear it: members of `sf` follow the round,
        everybody else goes back to sleep until `estimated_end_ms`.
    """
    session_id: int
    sf: int
    data_start_ms: int
    estimated_end_ms: int
    pkt_type: DauPacketType = DauPacketType.GROUP_START

    def to_bytes(self) -> bytes:
        return (
            bytes([self.pkt_type])
            + self.session_id.to_bytes(2, "big")
            + bytes([self.sf])
            + self.data_start_ms.to_bytes(4, "big")
            + self.estimated_end_ms.to_bytes(4, "big")
        )

    @classmethod
    def from_bytes(cls, data: bytes) -> "GroupStart":
        assert data[0] == cls.pkt_type, "Not a GroupStart"
        return cls(
            session_id=int.from_bytes(data[1:3], "big"),
            sf=data[3],
            data_start_ms=int.from_bytes(data[4:8], "big"),
            estimated_end_ms=int.from_bytes(data[8:12], "big"),
        )


@dataclass
class Fragment:
    """
        One piece of the firmware image. Either a source fragment (`index` is
        its position within the block) or a parity fragment (the XOR of the
        source fragments selected by `seed`, see coding.py).
    """
    session_id: int
    block: int
    kind: FragmentKind
    index: int
    seed: int
    payload: bytes
    pkt_type: DauPacketType = DauPacketType.FRAGMENT

    HEADER_LEN = 9

    def to_bytes(self) -> bytes:
        return (
            bytes([self.pkt_type])
            + self.session_id.to_bytes(2, "big")
            + self.block.to_bytes(2, "big")
            + bytes([self.kind, self.index])
            + self.seed.to_bytes(2, "big")
            + self.payload
        )

    @classmethod
    def from_bytes(cls, data: bytes) -> "Fragment":
        assert data[0] == cls.pkt_type, "Not a Fragment"
        return cls(
            session_id=int.from_bytes(data[1:3], "big"),
            block=int.from_bytes(data[3:5], "big"),
            kind=FragmentKind(data[5]),
            index=data[6],
            seed=int.from_bytes(data[7:9], "big"),
            payload=data[9:],
        )


@dataclass
class NackWindow:
    """
        Opens a CAD-NACK window. The window is split into `channels` logical
        channels; a node that wants to complain about logical channel `c`
        transmits a content-free burst in it. The gateway only runs CAD on each
        channel, so colliding bursts are a feature rather than a problem.
    """
    session_id: int
    scope: NackScope
    block: int
    channels: int
    start_ms: int
    slot_ms: int
    pkt_type: DauPacketType = DauPacketType.NACK_WINDOW

    def to_bytes(self) -> bytes:
        return (
            bytes([self.pkt_type])
            + self.session_id.to_bytes(2, "big")
            + bytes([self.scope])
            + self.block.to_bytes(2, "big")
            + bytes([self.channels])
            + self.start_ms.to_bytes(4, "big")
            + self.slot_ms.to_bytes(4, "big")
        )

    @classmethod
    def from_bytes(cls, data: bytes) -> "NackWindow":
        assert data[0] == cls.pkt_type, "Not a NackWindow"
        return cls(
            session_id=int.from_bytes(data[1:3], "big"),
            scope=NackScope(data[3]),
            block=int.from_bytes(data[4:6], "big"),
            channels=data[6],
            start_ms=int.from_bytes(data[7:11], "big"),
            slot_ms=int.from_bytes(data[11:15], "big"),
        )


@dataclass
class GroupStatus:
    """
        Closes a pass over a group, sent at the group's own SF. Either the group
        is repeated (because the group level NACK window was not silent) or the
        group is done and the node should return to CONTROL_SF at `next_ms`.
    """
    session_id: int
    sf: int
    repeat: bool
    next_ms: int
    pkt_type: DauPacketType = DauPacketType.GROUP_STATUS

    def to_bytes(self) -> bytes:
        return (
            bytes([self.pkt_type])
            + self.session_id.to_bytes(2, "big")
            + bytes([self.sf, int(self.repeat)])
            + self.next_ms.to_bytes(4, "big")
        )

    @classmethod
    def from_bytes(cls, data: bytes) -> "GroupStatus":
        assert data[0] == cls.pkt_type, "Not a GroupStatus"
        return cls(
            session_id=int.from_bytes(data[1:3], "big"),
            sf=data[3],
            repeat=bool(data[4]),
            next_ms=int.from_bytes(data[5:9], "big"),
        )


_PARSERS = {
    DauPacketType.SESSION_ANNOUNCE: SessionAnnounce,
    DauPacketType.PROBE:            Probe,
    DauPacketType.GROUP_START:      GroupStart,
    DauPacketType.FRAGMENT:         Fragment,
    DauPacketType.NACK_WINDOW:      NackWindow,
    DauPacketType.GROUP_STATUS:     GroupStatus,
}


def packet_from_bytes(data: bytes):
    """ Parse a received payload, returns None for anything we do not recognise. """
    if len(data) < 1:
        return None
    try:
        return _PARSERS[DauPacketType(data[0])].from_bytes(data)
    except (ValueError, KeyError, AssertionError, IndexError):
        return None
