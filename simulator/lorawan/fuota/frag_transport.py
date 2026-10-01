"""TS004 v2.0.0 Fragmented Data Block Transport package — wire codecs (FPort 201).

Pure encoders/decoders for the application-layer commands of the LoRa Alliance
specification *LoRaWAN Fragmented Data Block Transport*, **TS004-2.0.0** (FINAL,
April 2022). This module has **no dependency on the simulator runtime** (no
devices, no frames, no scheduling) apart from :mod:`simulator.lorawan.fuota` —
it only turns dataclasses into octets and back, so that the device-side and
server-side Applications built on top can be tested independently.

Scope (TS004-2.0.0 §3, Tables 1–24):

===== ============================== ========== ========== =================
CID   Command                        Direction  Addressing Payload (octets)
===== ============================== ========== ========== =================
0x00  ``PackageVersionReq``          downlink   unicast    0
0x00  ``PackageVersionAns``          uplink     unicast    2
0x01  ``FragSessionStatusReq``       downlink   uni/multi  1
0x01  ``FragSessionStatusAns``       uplink     unicast    1 **or** 4
0x02  ``FragSessionSetupReq``        downlink   unicast    16
0x02  ``FragSessionSetupAns``        uplink     unicast    1
0x03  ``FragSessionDeleteReq``       downlink   unicast    1
0x03  ``FragSessionDeleteAns``       uplink     unicast    1
0x04  ``FragDataBlockReceivedReq``   **uplink** unicast    1
0x04  ``FragDataBlockReceivedAns``   downlink   unicast    1
0x08  ``DataFragment``               downlink   uni/multi  2 + FragSize
===== ============================== ========== ========== =================

Note the unusual direction of CID 0x04: the **end-device** is the requester
(§3.5). Only CID 0x01 and CID 0x08 may be received on a multicast address.

Conventions (§1.3, identical in TS004 and TS005):

- **All multi-octet fields are little endian.**
- RFU bits SHALL be transmitted as 0 and SHALL be silently ignored on receive.
  Every ``decode_payload`` here masks RFU bits away rather than rejecting them.
- A message MAY carry several concatenated commands ``CID | payload | …``,
  executed first-to-last (§3). The one exception is ``DataFragment``, which
  SHALL be the only command in its message payload (§3.6) — the parsers in this
  module therefore stop after a ``DataFragment`` and hand it the whole rest of
  the payload.

Version caveat
--------------
These layouts are **v2.0.0 only**; TS004-2.0.0 §6 states the package is not
compatible with v1.0.0. Relative to v1.0.0: ``FragSessionSetupReq`` grew
``SessionCnt`` + ``MIC`` (10 → 16 octets), the ``Control`` byte gained
``AckReception`` (pushing ``FragAlgo`` to bits 5:3), CID 0x04 is new, and the
``DataFragment`` index ``N`` now starts at **1** instead of 0.

Deviation from :mod:`simulator.lorawan.mac_commands`
----------------------------------------------------
The CID is declared as a :class:`typing.ClassVar` rather than a dataclass field.
It is a constant of each command type, never varies per instance, and keeping it
out of the field list means the generated ``__init__`` takes the spec's fields in
spec order (``DataFragment(1, 5, payload)`` rather than ``DataFragment(cid, …)``).
As requested for this package, malformed input raises :class:`ValueError` instead
of tripping an ``assert``.
"""

from __future__ import annotations

import logging
import random as _random
from dataclasses import dataclass, field, replace
from enum import IntEnum
from typing import ClassVar

from simulator.lorawan.fuota.crypto import compute_data_block_mic
from simulator.lorawan.fuota.fragmentation import MAX_NB_FRAG

__all__ = [
    "DATA_FRAGMENT_HEADER_SIZE",
    "FRAGMENTATION_FPORT",
    "MAX_FRAG_SESSIONS",
    "MAX_MISSING_FRAG",
    "MAX_NB_FRAG_RECEIVED",
    "PACKAGE_IDENTIFIER",
    "PACKAGE_VERSION",
    "DataFragment",
    "FragCID",
    "FragCommand",
    "FragCommandType",
    "FragDataBlockReceivedAns",
    "FragDataBlockReceivedReq",
    "FragSessionDeleteAns",
    "FragSessionDeleteReq",
    "FragSessionSetupAns",
    "FragSessionSetupReq",
    "FragSessionStatusAns",
    "FragSessionStatusReq",
    "PackageVersionAns",
    "PackageVersionReq",
    "block_ack_delay_seconds",
    "encode_commands",
    "max_fragment_payload",
    "parse_downlink_commands",
    "parse_uplink_commands",
]

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Package identity (TS004-2.0.0 §2.1, §3.1)
# ---------------------------------------------------------------------------

#: RECOMMENDED default FPort for this package (§2.1). An implementation MAY use
#: any unassigned port, but once declared that port SHALL NOT be used for
#: anything else.
FRAGMENTATION_FPORT = 201

#: ``PackageIdentifier`` reported in ``PackageVersionAns`` (§3.1, Table 3).
PACKAGE_IDENTIFIER = 3

#: ``PackageVersion`` reported in ``PackageVersionAns`` (§3.1, Table 3).
PACKAGE_VERSION = 2

#: Number of simultaneous fragmentation sessions; ``FragIndex`` is 0..3 (§3.3).
MAX_FRAG_SESSIONS = 4

#: Octets of ``DataFragment`` overhead on air: 1 CID + 2 ``Index&N`` (§3.6).
DATA_FRAGMENT_HEADER_SIZE = 3

#: ``NbFragReceived`` is 14 bits (§3.2, Table 8). The spec does not define
#: wrap-around for a very redundant session, so this module saturates.
MAX_NB_FRAG_RECEIVED = (1 << 14) - 1

#: ``MissingFrag`` is one octet and SHALL saturate at 255 (§3.2).
MAX_MISSING_FRAG = 0xFF


class FragCID(IntEnum):
    """Command identifiers of the fragmentation package (TS004-2.0.0 §3, Table 1).

    Request and answer of a pair share the same CID; the direction of the frame
    disambiguates them — except that for CID 0x04 the roles are swapped, the
    *request* travelling uplink (§3.5).
    """

    PACKAGE_VERSION = 0x00
    FRAG_SESSION_STATUS = 0x01
    FRAG_SESSION_SETUP = 0x02
    FRAG_SESSION_DELETE = 0x03
    FRAG_DATA_BLOCK_RECEIVED = 0x04
    DATA_FRAGMENT = 0x08


# ---------------------------------------------------------------------------
# Base
# ---------------------------------------------------------------------------


@dataclass
class FragCommand:
    """Base class for every TS004 fragmentation package command."""

    #: CID octet prefixed to the payload on the wire. Constant per command type.
    cid: ClassVar[FragCID]

    def encode_payload(self) -> bytes:
        """Encode the command payload, **without** the leading CID octet."""
        return b""

    def encode(self) -> bytes:
        """Encode the full command: CID octet followed by the payload."""
        return bytes([self.cid]) + self.encode_payload()


def _check_frag_index(frag_index: int) -> int:
    if not 0 <= frag_index < MAX_FRAG_SESSIONS:
        raise ValueError(
            f"frag_index must be 0..{MAX_FRAG_SESSIONS - 1}, got {frag_index}"
        )
    return frag_index


def _check_len(name: str, data: bytes, expected: int) -> None:
    if len(data) != expected:
        raise ValueError(f"{name} payload must be {expected} octets, got {len(data)}")


# ---------------------------------------------------------------------------
# PackageVersion (CID 0x00) — TS004-2.0.0 §3.1, Tables 2 and 3
# ---------------------------------------------------------------------------


@dataclass
class PackageVersionReq(FragCommand):
    """Server asks the device which package version it implements (§3.1, Table 2).

    No payload: the whole command on the wire is the single octet ``0x00``.
    This CID-0x00 pattern is common to every LoRa Alliance application-layer
    package (TS003/TS004/TS005).
    """

    cid: ClassVar[FragCID] = FragCID.PACKAGE_VERSION

    @staticmethod
    def decode_payload(data: bytes) -> PackageVersionReq:
        """Decode the (empty) payload (§3.1, Table 2)."""
        _check_len("PackageVersionReq", data, 0)
        return PackageVersionReq()


@dataclass
class PackageVersionAns(FragCommand):
    """Device reports package identity (§3.1, Table 3).

    Payload: ``PackageIdentifier`` (1) ``| PackageVersion`` (1), i.e. ``03 02``
    for TS004-2.0.0.
    """

    package_identifier: int = PACKAGE_IDENTIFIER
    package_version: int = PACKAGE_VERSION

    cid: ClassVar[FragCID] = FragCID.PACKAGE_VERSION

    def encode_payload(self) -> bytes:
        return bytes([self.package_identifier & 0xFF, self.package_version & 0xFF])

    @staticmethod
    def decode_payload(data: bytes) -> PackageVersionAns:
        """Decode a 2-octet ``PackageVersionAns`` payload (§3.1, Table 3)."""
        _check_len("PackageVersionAns", data, 2)
        return PackageVersionAns(package_identifier=data[0], package_version=data[1])


# ---------------------------------------------------------------------------
# FragSessionStatus (CID 0x01) — TS004-2.0.0 §3.2, Tables 4–8
# ---------------------------------------------------------------------------


@dataclass
class FragSessionStatusReq(FragCommand):
    """Server requests a defragmentation status report (§3.2, Tables 4 and 5).

    Payload, one octet ``FragStatusReqParam``::

        bits 7:3  RFU
        bits 2:1  FragIndex
        bit  0    Participants

    ``Participants = 0`` → only receivers still missing fragments SHALL answer;
    ``= 1`` → all receivers answer, including those that already reconstructed
    the block. Receivers spread their answers over
    ``rand() * 2**(BlockAckDelay + 4)`` seconds (see :func:`block_ack_delay_seconds`).

    May be sent on a multicast address.
    """

    frag_index: int = 0
    participants: bool = False

    cid: ClassVar[FragCID] = FragCID.FRAG_SESSION_STATUS

    def encode_payload(self) -> bytes:
        _check_frag_index(self.frag_index)
        return bytes([(self.frag_index << 1) | int(self.participants)])

    @staticmethod
    def decode_payload(data: bytes) -> FragSessionStatusReq:
        """Decode a 1-octet ``FragStatusReqParam`` (§3.2, Table 5)."""
        _check_len("FragSessionStatusReq", data, 1)
        return FragSessionStatusReq(
            frag_index=(data[0] >> 1) & 0x03,
            participants=bool(data[0] & 0x01),
        )


@dataclass
class FragSessionStatusAns(FragCommand):
    """Device reports its defragmentation status (§3.2, Tables 6, 7 and 8).

    **Variable length: 1 or 4 octets.** Payload::

        offset 0  1  Status
        offset 1  2  Received&index   (conditional)
        offset 3  1  MissingFrag      (conditional)

    ``Status``::

        bits 7:3  RFU
        bit  2    Session does not exist
        bit  1    MICError
        bit  0    MemoryError

    When ``Session does not exist`` is set the device SHALL NOT include
    ``Received&index`` and ``MissingFrag``, making the payload **1 octet**
    (§3.2). Every parser must special-case this.

    ``Received&index`` is a 16-bit little-endian word::

        bits 15:14  FragIndex
        bits 13:0   NbFragReceived

    ``NbFragReceived`` counts *all* fragments received for the session since it
    was created — coded, uncoded and repeated. Its 14-bit width can be exceeded
    by a very redundant session; TS004 does not define the behaviour, so this
    module saturates at :data:`MAX_NB_FRAG_RECEIVED` on encode.

    ``MissingFrag`` is the number of uncoded fragments still missing, i.e. the
    minimum number of further *independent* coded fragments needed. 0 once the
    block is reassembled; SHALL be set to 255 if more than 255 are missing.

    ``MICError`` is meaningful **only when ``MissingFrag == 0``**; it SHALL be 0
    otherwise (§3.2).

    Note: the v2.0.0 ``Status`` byte defines exactly these three bits. There is
    no separate "not enough matrix memory" flag — a defragmentation aborted for
    lack of parity-matrix memory is reported through ``MemoryError`` (bit 0).
    Nor is there a ``SessionCnt`` field: the anti-replay counter appears only in
    ``FragSessionSetupReq`` (§3.3).

    **Spec gap:** ``FragIndex`` is carried *only* inside ``Received&index``, so
    the 1-octet "session does not exist" form does not identify which session it
    refers to. TS004 leaves this unresolved; the server has to correlate the
    answer with the ``FragSessionStatusReq`` it sent. :meth:`decode_payload`
    therefore reports ``frag_index = 0`` for that form, and :attr:`frag_index`
    is simply not transmitted when :attr:`session_does_not_exist` is set.
    """

    frag_index: int = 0
    nb_frag_received: int = 0
    missing_frag: int = 0
    memory_error: bool = False
    mic_error: bool = False
    session_does_not_exist: bool = False

    cid: ClassVar[FragCID] = FragCID.FRAG_SESSION_STATUS

    @property
    def status_byte(self) -> int:
        """The ``Status`` octet (§3.2, Table 7)."""
        return (
            (int(self.session_does_not_exist) << 2)
            | (int(self.mic_error) << 1)
            | int(self.memory_error)
        )

    @property
    def received_and_index(self) -> int:
        """The 16-bit ``Received&index`` word (§3.2, Table 8), saturating."""
        nb = min(max(self.nb_frag_received, 0), MAX_NB_FRAG_RECEIVED)
        return (self.frag_index << 14) | nb

    def encode_payload(self) -> bytes:
        _check_frag_index(self.frag_index)
        if self.session_does_not_exist:
            # §3.2: Received&index and MissingFrag SHALL NOT be included.
            return bytes([self.status_byte])
        missing = min(max(self.missing_frag, 0), MAX_MISSING_FRAG)
        return (
            bytes([self.status_byte])
            + self.received_and_index.to_bytes(2, "little")
            + bytes([missing])
        )

    @staticmethod
    def payload_size(status_byte: int) -> int:
        """Octets of payload implied by a ``Status`` octet (§3.2).

        1 when ``Session does not exist`` (bit 2) is set, otherwise 4. Used by
        :func:`parse_uplink_commands` to walk a concatenated message.
        """
        return 1 if status_byte & 0x04 else 4

    @staticmethod
    def decode_payload(data: bytes) -> FragSessionStatusAns:
        """Decode a 1- or 4-octet ``FragSessionStatusAns`` payload (§3.2)."""
        if not data:
            raise ValueError("FragSessionStatusAns payload must be 1 or 4 octets, got 0")
        status = data[0]
        expected = FragSessionStatusAns.payload_size(status)
        _check_len("FragSessionStatusAns", data, expected)

        if expected == 1:
            return FragSessionStatusAns(
                memory_error=bool(status & 0x01),
                mic_error=bool(status & 0x02),
                session_does_not_exist=True,
            )

        word = int.from_bytes(data[1:3], "little")
        return FragSessionStatusAns(
            frag_index=(word >> 14) & 0x03,
            nb_frag_received=word & MAX_NB_FRAG_RECEIVED,
            missing_frag=data[3],
            memory_error=bool(status & 0x01),
            mic_error=bool(status & 0x02),
            session_does_not_exist=False,
        )


# ---------------------------------------------------------------------------
# FragSessionSetup (CID 0x02) — TS004-2.0.0 §3.3, Tables 9–14
# ---------------------------------------------------------------------------


@dataclass
class FragSessionSetupReq(FragCommand):
    """Server creates a fragmentation session — **16 octets** (§3.3, Table 9).

    ::

        offset  0  1  FragSession
        offset  1  2  NbFrag       (little endian)
        offset  3  1  FragSize
        offset  4  1  Control
        offset  5  1  Padding
        offset  6  4  Descriptor   (little endian)
        offset 10  2  SessionCnt   (little endian)
        offset 12  4  MIC          (as transmitted, cmac[0..3])

    ``FragSession`` (Table 10)::

        bits 7:6  RFU
        bits 5:4  FragIndex
        bits 3:0  McGroupBitMask

    ``McGroupBitMask`` bit *X* allows the TS005 multicast group with
    ``McGroupID = X`` to feed fragments into this session. Unicast is always an
    allowed source and cannot be disabled, so ``0b0000`` means "unicast only".
    Devices without multicast support SHALL ignore the field.

    ``Control`` (Table 11)::

        bit  7    RFU
        bit  6    AckReception
        bits 5:3  FragAlgo
        bits 2:0  BlockAckDelay

    ``AckReception = 1`` makes the device send ``FragDataBlockReceivedReq`` once
    the block is complete. ``FragAlgo = 0`` selects the Annex A FEC code
    (1..7 are RFU). ``BlockAckDelay`` drives the random answer-spreading window,
    see :func:`block_ack_delay_seconds`.

    ``NbFrag`` is ``M``, at most ``2**14 - 1 = 16383`` because ``DataFragment``'s
    ``N`` field is 14 bits. The data block size is
    ``NbFrag * FragSize - Padding``.

    ``Descriptor`` is a freely allocated, vendor-specific 4-octet field; it is
    modelled here as a 32-bit integer so it can be passed straight to
    :func:`~simulator.lorawan.fuota.crypto.compute_data_block_mic`.

    ``SessionCnt`` is the per-``FragIndex`` anti-replay counter. The device
    rejects a setup whose ``SessionCnt <= SessionCntPrev[FragIndex]`` with the
    ``SessionCnt replay`` bit, and commits ``SessionCntPrev`` only upon reception
    of the first ``DataFragment`` of the new session (§3.3).

    ``MIC`` is ``aes128_cmac(DataBlockIntKey, B0 | data block)[0:4]``, computed
    by the server per target device and verified after reassembly. Build it with
    :meth:`compute_mic` / :meth:`with_mic`.
    """

    frag_index: int = 0
    mc_group_bit_mask: int = 0
    nb_frag: int = 1
    frag_size: int = 1
    ack_reception: bool = False
    frag_algo: int = 0
    block_ack_delay: int = 0
    padding: int = 0
    descriptor: int = 0
    session_cnt: int = 0
    mic: bytes = field(default=b"\x00\x00\x00\x00")

    cid: ClassVar[FragCID] = FragCID.FRAG_SESSION_SETUP

    #: Size of the encoded payload in octets, excluding the CID (§3.3, Table 9).
    PAYLOAD_SIZE: ClassVar[int] = 16

    @property
    def frag_session_byte(self) -> int:
        """The ``FragSession`` octet (§3.3, Table 10)."""
        return ((self.frag_index & 0x03) << 4) | (self.mc_group_bit_mask & 0x0F)

    @property
    def control_byte(self) -> int:
        """The ``Control`` octet (§3.3, Table 11)."""
        return (
            (int(self.ack_reception) << 6)
            | ((self.frag_algo & 0x07) << 3)
            | (self.block_ack_delay & 0x07)
        )

    @property
    def block_size(self) -> int:
        """Data block size in octets: ``NbFrag * FragSize - Padding`` (§3.3)."""
        return self.nb_frag * self.frag_size - self.padding

    def _validate(self) -> None:
        _check_frag_index(self.frag_index)
        if not 0 <= self.mc_group_bit_mask <= 0x0F:
            raise ValueError(
                f"mc_group_bit_mask must be 0..15, got {self.mc_group_bit_mask}"
            )
        if not 1 <= self.nb_frag <= MAX_NB_FRAG:
            raise ValueError(f"nb_frag must be 1..{MAX_NB_FRAG}, got {self.nb_frag}")
        if not 0 <= self.frag_size <= 0xFF:
            raise ValueError(f"frag_size must be 0..255, got {self.frag_size}")
        if not 0 <= self.frag_algo <= 0x07:
            raise ValueError(f"frag_algo must be 0..7, got {self.frag_algo}")
        if not 0 <= self.block_ack_delay <= 0x07:
            raise ValueError(
                f"block_ack_delay must be 0..7, got {self.block_ack_delay}"
            )
        if not 0 <= self.padding <= 0xFF:
            raise ValueError(f"padding must be 0..255, got {self.padding}")
        if not 0 <= self.descriptor <= 0xFFFFFFFF:
            raise ValueError(f"descriptor must fit in 32 bits, got {self.descriptor}")
        if not 0 <= self.session_cnt <= 0xFFFF:
            raise ValueError(f"session_cnt must fit in 16 bits, got {self.session_cnt}")
        if len(self.mic) != 4:
            raise ValueError(f"mic must be 4 octets, got {len(self.mic)}")

    def encode_payload(self) -> bytes:
        self._validate()
        return (
            bytes([self.frag_session_byte])
            + self.nb_frag.to_bytes(2, "little")
            + bytes([self.frag_size, self.control_byte, self.padding])
            + self.descriptor.to_bytes(4, "little")
            + self.session_cnt.to_bytes(2, "little")
            + bytes(self.mic)
        )

    @staticmethod
    def decode_payload(data: bytes) -> FragSessionSetupReq:
        """Decode a 16-octet ``FragSessionSetupReq`` payload (§3.3, Table 9)."""
        _check_len("FragSessionSetupReq", data, FragSessionSetupReq.PAYLOAD_SIZE)
        control = data[4]
        return FragSessionSetupReq(
            frag_index=(data[0] >> 4) & 0x03,
            mc_group_bit_mask=data[0] & 0x0F,
            nb_frag=int.from_bytes(data[1:3], "little"),
            frag_size=data[3],
            ack_reception=bool(control & 0x40),
            frag_algo=(control >> 3) & 0x07,
            block_ack_delay=control & 0x07,
            padding=data[5],
            descriptor=int.from_bytes(data[6:10], "little"),
            session_cnt=int.from_bytes(data[10:12], "little"),
            mic=bytes(data[12:16]),
        )

    # -- MIC helpers -------------------------------------------------------

    def compute_mic(self, data_block_int_key: bytes, data_block: bytes) -> bytes:
        """Compute this session's data block MIC (§3.3, Table 12).

        ``MIC = aes128_cmac(DataBlockIntKey, B0 | data block)[0:4]`` with ``B0``
        binding ``SessionCnt``, ``FragIndex``, ``Descriptor`` and the block
        length — all taken from ``self``. See
        :func:`simulator.lorawan.fuota.crypto.compute_data_block_mic` for the
        exact ``B0`` layout and for the padded/un-padded ambiguity: pass the
        **un-padded** data block (``NbFrag * FragSize - Padding`` octets), which
        is the interoperable reading.

        Args:
            data_block_int_key: The target device's ``DataBlockIntKey``.
            data_block: Exactly the octets covered by the MIC.

        Returns:
            The 4-octet ``MIC`` field.
        """
        return compute_data_block_mic(
            data_block_int_key=data_block_int_key,
            data_block=data_block,
            session_cnt=self.session_cnt,
            frag_index=self.frag_index,
            descriptor=self.descriptor,
        )

    def with_mic(
        self, data_block_int_key: bytes, data_block: bytes
    ) -> FragSessionSetupReq:
        """Return a copy of this command with :meth:`compute_mic` filled in.

        The MIC is per-device (``DataBlockIntKey`` is device-specific), so a
        server building a multicast session calls this once per target device
        with otherwise identical parameters.
        """
        return replace(
            self, mic=self.compute_mic(data_block_int_key, data_block)
        )

    def verify_mic(self, data_block_int_key: bytes, data_block: bytes) -> bool:
        """Device-side check of the reassembled block against :attr:`mic` (§3.3)."""
        return self.compute_mic(data_block_int_key, data_block) == bytes(self.mic)


@dataclass
class FragSessionSetupAns(FragCommand):
    """Device answers a session setup — 1 octet ``StatusBitMask`` (§3.3, Tables 13/14).

    ::

        bits 7:6  FragIndex
        bit  5    RFU
        bit  4    SessionCnt replay
        bit  3    Wrong Descriptor
        bit  2    FragIndex unsupported
        bit  1    Not enough Memory
        bit  0    FragAlgo unsupported

    **If any of bits [0:4] is 1 the request was NOT accepted** (§3.3); see
    :attr:`accepted`. ``SessionCnt replay`` is new in v2.0.0 and signals
    ``SessionCnt <= SessionCntPrev[FragIndex]``.

    Note the ``FragIndex`` echo sits in bits 7:6 here, unlike every other
    command in the package where it is in the low bits.
    """

    frag_index: int = 0
    frag_algo_unsupported: bool = False
    not_enough_memory: bool = False
    frag_index_unsupported: bool = False
    wrong_descriptor: bool = False
    session_cnt_replay: bool = False

    cid: ClassVar[FragCID] = FragCID.FRAG_SESSION_SETUP

    #: Mask of the error bits [0:4] (§3.3).
    ERROR_MASK: ClassVar[int] = 0x1F

    @property
    def accepted(self) -> bool:
        """True when none of the error bits [0:4] is set (§3.3)."""
        return not (
            self.frag_algo_unsupported
            or self.not_enough_memory
            or self.frag_index_unsupported
            or self.wrong_descriptor
            or self.session_cnt_replay
        )

    def encode_payload(self) -> bytes:
        _check_frag_index(self.frag_index)
        return bytes(
            [
                (self.frag_index << 6)
                | (int(self.session_cnt_replay) << 4)
                | (int(self.wrong_descriptor) << 3)
                | (int(self.frag_index_unsupported) << 2)
                | (int(self.not_enough_memory) << 1)
                | int(self.frag_algo_unsupported)
            ]
        )

    @staticmethod
    def decode_payload(data: bytes) -> FragSessionSetupAns:
        """Decode a 1-octet ``StatusBitMask`` (§3.3, Table 14)."""
        _check_len("FragSessionSetupAns", data, 1)
        b = data[0]
        return FragSessionSetupAns(
            frag_index=(b >> 6) & 0x03,
            frag_algo_unsupported=bool(b & 0x01),
            not_enough_memory=bool(b & 0x02),
            frag_index_unsupported=bool(b & 0x04),
            wrong_descriptor=bool(b & 0x08),
            session_cnt_replay=bool(b & 0x10),
        )


# ---------------------------------------------------------------------------
# FragSessionDelete (CID 0x03) — TS004-2.0.0 §3.4, Tables 15–18
# ---------------------------------------------------------------------------


@dataclass
class FragSessionDeleteReq(FragCommand):
    """Server deletes a fragmentation session (§3.4, Tables 15 and 16).

    Payload, one octet ``Param``: bits 7:2 RFU, bits 1:0 ``FragIndex``.
    """

    frag_index: int = 0

    cid: ClassVar[FragCID] = FragCID.FRAG_SESSION_DELETE

    def encode_payload(self) -> bytes:
        _check_frag_index(self.frag_index)
        return bytes([self.frag_index])

    @staticmethod
    def decode_payload(data: bytes) -> FragSessionDeleteReq:
        """Decode a 1-octet ``Param`` (§3.4, Table 16)."""
        _check_len("FragSessionDeleteReq", data, 1)
        return FragSessionDeleteReq(frag_index=data[0] & 0x03)


@dataclass
class FragSessionDeleteAns(FragCommand):
    """Device answers a session delete (§3.4, Tables 17 and 18).

    Payload, one octet ``Status``::

        bits 7:3  RFU
        bit  2    Session does not exist
        bits 1:0  FragIndex

    Bit 2 set means the command was not accepted because no session with that
    ``FragIndex`` existed in the device.
    """

    frag_index: int = 0
    session_does_not_exist: bool = False

    cid: ClassVar[FragCID] = FragCID.FRAG_SESSION_DELETE

    @property
    def accepted(self) -> bool:
        """True when the session existed and was deleted (§3.4)."""
        return not self.session_does_not_exist

    def encode_payload(self) -> bytes:
        _check_frag_index(self.frag_index)
        return bytes([(int(self.session_does_not_exist) << 2) | self.frag_index])

    @staticmethod
    def decode_payload(data: bytes) -> FragSessionDeleteAns:
        """Decode a 1-octet ``Status`` (§3.4, Table 18)."""
        _check_len("FragSessionDeleteAns", data, 1)
        return FragSessionDeleteAns(
            frag_index=data[0] & 0x03,
            session_does_not_exist=bool(data[0] & 0x04),
        )


# ---------------------------------------------------------------------------
# FragDataBlockReceived (CID 0x04) — TS004-2.0.0 §3.5, Tables 19–22
# ---------------------------------------------------------------------------


@dataclass
class FragDataBlockReceivedReq(FragCommand):
    """Device signals that the data block is fully received — **uplink** (§3.5).

    New in v2.0.0. Sent **only if** ``AckReception`` was set in the session's
    ``FragSessionSetupReq``.

    Payload, one octet ``Status`` (Table 20)::

        bits 7:3  RFU
        bit  2    MICError
        bits 1:0  FragIndex

    ``MICError = 1`` means the device-computed MIC and the one from
    ``FragSessionSetupReq`` differ and the block will not be processed.

    For multicast deliveries the delay between the last enabling fragment and
    this uplink — and the delay between retransmissions, which the application
    is responsible for — SHALL be randomised with the session's
    ``BlockAckDelay`` (:func:`block_ack_delay_seconds`).

    Note there is no ``SessionCnt`` or MIC field in this command: the single
    status octet is the whole v2.0.0 payload (Table 19).
    """

    frag_index: int = 0
    mic_error: bool = False

    cid: ClassVar[FragCID] = FragCID.FRAG_DATA_BLOCK_RECEIVED

    def encode_payload(self) -> bytes:
        _check_frag_index(self.frag_index)
        return bytes([(int(self.mic_error) << 2) | self.frag_index])

    @staticmethod
    def decode_payload(data: bytes) -> FragDataBlockReceivedReq:
        """Decode a 1-octet ``Status`` (§3.5, Table 20)."""
        _check_len("FragDataBlockReceivedReq", data, 1)
        return FragDataBlockReceivedReq(
            frag_index=data[0] & 0x03,
            mic_error=bool(data[0] & 0x04),
        )


@dataclass
class FragDataBlockReceivedAns(FragCommand):
    """Server acknowledges ``FragDataBlockReceivedReq`` — **downlink** (§3.5).

    Payload, one octet ``Param`` (Table 22): bits 7:2 RFU, bits 1:0
    ``FragIndex``, which SHALL match the request being acknowledged.
    """

    frag_index: int = 0

    cid: ClassVar[FragCID] = FragCID.FRAG_DATA_BLOCK_RECEIVED

    def encode_payload(self) -> bytes:
        _check_frag_index(self.frag_index)
        return bytes([self.frag_index])

    @staticmethod
    def decode_payload(data: bytes) -> FragDataBlockReceivedAns:
        """Decode a 1-octet ``Param`` (§3.5, Table 22)."""
        _check_len("FragDataBlockReceivedAns", data, 1)
        return FragDataBlockReceivedAns(frag_index=data[0] & 0x03)


# ---------------------------------------------------------------------------
# DataFragment (CID 0x08) — TS004-2.0.0 §3.6, Tables 23 and 24
# ---------------------------------------------------------------------------


@dataclass
class DataFragment(FragCommand):
    """One coded fragment of the data block (§3.6, Tables 23 and 24).

    Payload::

        offset 0  2             Index&N  (little endian)
        offset 2  0:MaxAppPl-3  P^N_M    (FragSize octets)

    ``Index&N`` (Table 24)::

        bits 15:14  FragIndex
        bits 13:0   N

    ``N`` is the index of the coded fragment and **is incremented starting from
    1** (it started at 0 in v1.0.0 — a breaking change). More than ``M`` coded
    fragments MAY be transmitted for redundancy.

    Worked wire example, ``FragIndex = 1``, ``N = 5``::

        Index&N = (1 << 14) | 5 = 0x4005  ->  octets 05 40
        full command payload: 08 05 40 <FragSize octets>

    Reception rules carried by the Application layer, not by this codec:
    a multicast frame SHALL be dropped unless its group was enabled through
    ``McGroupBitMask``; duplicates are silently discarded; once the block is
    reconstructed every further message using that ``FragIndex`` is dropped
    until the session is deleted and a new one set up.

    This is the **only** command permitted in its message payload (§3), which is
    why the parsers consume the entire remaining payload as the fragment and
    then stop.
    """

    frag_index: int = 0
    index_n: int = 1
    payload: bytes = b""

    cid: ClassVar[FragCID] = FragCID.DATA_FRAGMENT

    @property
    def index_and_n(self) -> int:
        """The 16-bit ``Index&N`` word (§3.6, Table 24)."""
        return (self.frag_index << 14) | self.index_n

    def encode_payload(self) -> bytes:
        _check_frag_index(self.frag_index)
        if not 1 <= self.index_n <= MAX_NB_FRAG:
            raise ValueError(
                f"index_n (N) is 1-based and 14 bits wide: must be 1..{MAX_NB_FRAG}, "
                f"got {self.index_n}"
            )
        return self.index_and_n.to_bytes(2, "little") + bytes(self.payload)

    @staticmethod
    def decode_payload(data: bytes) -> DataFragment:
        """Decode a ``DataFragment`` payload; the rest is the fragment (§3.6)."""
        if len(data) < 2:
            raise ValueError(
                f"DataFragment payload needs at least the 2-octet Index&N header, "
                f"got {len(data)}"
            )
        word = int.from_bytes(data[0:2], "little")
        index_n = word & MAX_NB_FRAG
        if index_n == 0:
            raise ValueError("DataFragment N is 1-based in TS004 v2.0.0, got 0")
        return DataFragment(
            frag_index=(word >> 14) & 0x03,
            index_n=index_n,
            payload=bytes(data[2:]),
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def block_ack_delay_seconds(
    block_ack_delay: int, rng: _random.Random | None = None
) -> float:
    """Draw the random answer-spreading delay of §3.3::

        delay = rand() * 2**(BlockAckDelay + 4)   seconds

    with ``rand()`` a uniform real in ``[0, 1]``. The result is therefore in
    ``[0, 2**(BlockAckDelay + 4))``: 16 s for ``BlockAckDelay = 0`` up to
    2048 s for ``BlockAckDelay = 7``.

    The same draw governs (a) spreading ``FragSessionStatusAns`` after a
    multicast ``FragSessionStatusReq`` (§3.2), (b) the delay before
    ``FragDataBlockReceivedReq`` and between its retransmissions (§3.5), and
    (c) the delay before an automatic reboot at the end of a session, so that a
    fleet does not restart synchronously (§3.3).

    Args:
        block_ack_delay: The 3-bit ``BlockAckDelay`` field, 0..7.
        rng: Random source; defaults to the :mod:`random` module's shared
            generator. Pass a seeded :class:`random.Random` for reproducible
            simulations.

    Returns:
        A delay in seconds.

    Raises:
        ValueError: if ``block_ack_delay`` is outside 0..7.
    """
    if not 0 <= block_ack_delay <= 7:
        raise ValueError(f"block_ack_delay must be 0..7, got {block_ack_delay}")
    source = rng if rng is not None else _random
    return source.random() * float(1 << (block_ack_delay + 4))


def max_fragment_payload(frm_payload_limit: int) -> int:
    """Largest usable ``FragSize`` for a given application payload limit (§3.6).

    A ``DataFragment`` message costs 1 CID octet plus the 2-octet ``Index&N``
    header, so the fragment itself is at most ``MaxAppPl - 3`` octets
    (:data:`DATA_FRAGMENT_HEADER_SIZE`). TS004 gives no other numeric bound on
    ``FragSize``; the real constraint is the region's DR-dependent maximum
    application payload.

    Args:
        frm_payload_limit: ``MaxAppPl``, the maximum LoRaWAN application payload
            size in octets for the data rate in use.

    Returns:
        The maximum ``FragSize``, clamped at 0 for limits below the header size.
    """
    return max(0, frm_payload_limit - DATA_FRAGMENT_HEADER_SIZE)


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------

FragCommandType = (
    PackageVersionReq
    | PackageVersionAns
    | FragSessionStatusReq
    | FragSessionStatusAns
    | FragSessionSetupReq
    | FragSessionSetupAns
    | FragSessionDeleteReq
    | FragSessionDeleteAns
    | FragDataBlockReceivedReq
    | FragDataBlockReceivedAns
    | DataFragment
)

#: Fixed payload sizes of the server -> device (downlink) commands, in octets.
#: ``DATA_FRAGMENT`` is absent because it is variable length and consumes the
#: remainder of the message (§3.6).
_DOWNLINK_PAYLOAD_SIZES: dict[FragCID, int] = {
    FragCID.PACKAGE_VERSION: 0,  # PackageVersionReq
    FragCID.FRAG_SESSION_STATUS: 1,  # FragSessionStatusReq
    FragCID.FRAG_SESSION_SETUP: FragSessionSetupReq.PAYLOAD_SIZE,
    FragCID.FRAG_SESSION_DELETE: 1,  # FragSessionDeleteReq
    FragCID.FRAG_DATA_BLOCK_RECEIVED: 1,  # FragDataBlockReceivedAns
}

#: Fixed payload sizes of the device -> server (uplink) commands, in octets.
#: ``FRAG_SESSION_STATUS`` is absent because ``FragSessionStatusAns`` is 1 or 4
#: octets depending on its Status bit 2 (§3.2).
_UPLINK_PAYLOAD_SIZES: dict[FragCID, int] = {
    FragCID.PACKAGE_VERSION: 2,  # PackageVersionAns
    FragCID.FRAG_SESSION_SETUP: 1,  # FragSessionSetupAns
    FragCID.FRAG_SESSION_DELETE: 1,  # FragSessionDeleteAns
    FragCID.FRAG_DATA_BLOCK_RECEIVED: 1,  # FragDataBlockReceivedReq
}


def _decode_cid(data: bytes, pos: int) -> FragCID | None:
    try:
        return FragCID(data[pos])
    except ValueError:
        logger.warning(
            "TS004 FPort %d: unknown command identifier 0x%02X at offset %d; "
            "stopping, %d octet(s) ignored",
            FRAGMENTATION_FPORT,
            data[pos],
            pos,
            len(data) - pos,
        )
        return None


def parse_downlink_commands(data: bytes) -> list[FragCommandType]:
    """Parse a server -> device FRMPayload of the fragmentation package (§3).

    Handles several concatenated commands. Parsing stops — with a warning — on
    an unknown CID, on a truncated command, or on a command whose fixed payload
    does not decode; commands already parsed are returned.

    A ``DataFragment`` (CID 0x08) consumes the whole remaining payload and ends
    the walk, since §3.6 makes it the only command allowed in its message.
    """
    commands: list[FragCommandType] = []
    pos = 0

    while pos < len(data):
        cid = _decode_cid(data, pos)
        if cid is None:
            break
        pos += 1

        if cid is FragCID.DATA_FRAGMENT:
            try:
                commands.append(DataFragment.decode_payload(data[pos:]))
            except ValueError as exc:
                logger.warning("TS004: malformed DataFragment, dropped (%s)", exc)
            break  # §3.6: DataFragment is the only command in its message.

        size = _DOWNLINK_PAYLOAD_SIZES[cid]
        payload = data[pos : pos + size]
        if len(payload) < size:
            logger.warning(
                "TS004: truncated downlink command CID 0x%02X: need %d octet(s), "
                "got %d",
                cid,
                size,
                len(payload),
            )
            break
        pos += size

        try:
            match cid:
                case FragCID.PACKAGE_VERSION:
                    commands.append(PackageVersionReq.decode_payload(payload))
                case FragCID.FRAG_SESSION_STATUS:
                    commands.append(FragSessionStatusReq.decode_payload(payload))
                case FragCID.FRAG_SESSION_SETUP:
                    commands.append(FragSessionSetupReq.decode_payload(payload))
                case FragCID.FRAG_SESSION_DELETE:
                    commands.append(FragSessionDeleteReq.decode_payload(payload))
                case FragCID.FRAG_DATA_BLOCK_RECEIVED:
                    commands.append(FragDataBlockReceivedAns.decode_payload(payload))
        except ValueError as exc:
            logger.warning("TS004: malformed downlink CID 0x%02X (%s)", cid, exc)
            break

    return commands


def parse_uplink_commands(data: bytes) -> list[FragCommandType]:
    """Parse a device -> server FRMPayload of the fragmentation package (§3).

    Handles several concatenated commands, including the variable-length
    ``FragSessionStatusAns`` (1 octet when its ``Session does not exist`` bit is
    set, 4 otherwise — §3.2). Parsing stops with a warning on an unknown CID or
    a truncated command.

    ``DataFragment`` never travels uplink, so CID 0x08 is reported as unknown
    here.
    """
    commands: list[FragCommandType] = []
    pos = 0

    while pos < len(data):
        cid = _decode_cid(data, pos)
        if cid is None:
            break
        if cid is FragCID.DATA_FRAGMENT:
            logger.warning(
                "TS004: DataFragment (CID 0x08) is downlink-only; stopping uplink parse"
            )
            break
        pos += 1

        if cid is FragCID.FRAG_SESSION_STATUS:
            if pos >= len(data):
                logger.warning("TS004: truncated FragSessionStatusAns (no Status octet)")
                break
            size = FragSessionStatusAns.payload_size(data[pos])
        else:
            size = _UPLINK_PAYLOAD_SIZES[cid]

        payload = data[pos : pos + size]
        if len(payload) < size:
            logger.warning(
                "TS004: truncated uplink command CID 0x%02X: need %d octet(s), got %d",
                cid,
                size,
                len(payload),
            )
            break
        pos += size

        try:
            match cid:
                case FragCID.PACKAGE_VERSION:
                    commands.append(PackageVersionAns.decode_payload(payload))
                case FragCID.FRAG_SESSION_STATUS:
                    commands.append(FragSessionStatusAns.decode_payload(payload))
                case FragCID.FRAG_SESSION_SETUP:
                    commands.append(FragSessionSetupAns.decode_payload(payload))
                case FragCID.FRAG_SESSION_DELETE:
                    commands.append(FragSessionDeleteAns.decode_payload(payload))
                case FragCID.FRAG_DATA_BLOCK_RECEIVED:
                    commands.append(FragDataBlockReceivedReq.decode_payload(payload))
        except ValueError as exc:
            logger.warning("TS004: malformed uplink CID 0x%02X (%s)", cid, exc)
            break

    return commands


def encode_commands(commands: list[FragCommand]) -> bytes:
    """Encode a list of fragmentation package commands into one FRMPayload (§3).

    Commands are concatenated ``CID | payload | CID | payload | …`` and are
    executed by the receiver first-to-last. Per §3.6 a ``DataFragment`` SHALL be
    the only command in its message; this helper does not enforce that, so that
    tests can build deliberately malformed payloads.
    """
    buf = bytearray()
    for cmd in commands:
        buf.extend(cmd.encode())
    return bytes(buf)
