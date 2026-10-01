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
import math
import random as _random
from collections import defaultdict, deque
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from enum import IntEnum
from typing import TYPE_CHECKING, ClassVar

from simulator.environment import simulation_env as sim
from simulator.exceptions import SimulatorException
from simulator.lorawan.application import Application
from simulator.lorawan.fuota.crypto import (
    compute_data_block_mic,
    derive_data_block_int_key,
)
from simulator.lorawan.fuota.device_app import FuotaDeviceApplication
from simulator.lorawan.fuota.fragmentation import (
    MAX_NB_FRAG,
    FragmentationDecoder,
    FragmentationEncoder,
)
from simulator.lorawan.network_server import NetworkServer, ScheduledMulticastDownlink
from simulator.lorawan.region import max_frm_payload

if TYPE_CHECKING:
    from simulator.lorawan.device import LoRaWanDevice

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
    "FragSessionState",
    "FragSessionStatusAns",
    "FragSessionStatusReq",
    "FragServerSession",
    "FragStatusReport",
    "FragmentationDeviceApplication",
    "FragmentationServerApplication",
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


# ---------------------------------------------------------------------------
# Device-side application (TS004-2.0.0 §3, FPort 201)
# ---------------------------------------------------------------------------


@dataclass
class FragSessionState:
    """One of the (at most four) fragmentation sessions held by a device (§3.3).

    Created by an accepted ``FragSessionSetupReq`` and destroyed by a
    ``FragSessionDeleteReq`` or by a *successful* replacing setup. Holds the
    session parameters as received, the FEC decoder, and the reception
    bookkeeping that ``FragSessionStatusAns`` reports.

    :ivar setup: The accepted ``FragSessionSetupReq`` verbatim — it carries
        ``Padding``, ``Descriptor``, ``SessionCnt`` and the data block ``MIC``
        that are needed once the block is complete.
    :ivar frames_received: ``NbFragReceived``: every ``DataFragment`` fed to the
        decoder for this session, **including coded, uncoded and repeated**
        ones (§3.2).
    :ivar session_cnt_committed: Whether ``SessionCntPrev[FragIndex]`` has been
        written yet. §3.3 commits it on the *first* ``DataFragment``, not at
        setup time.
    :ivar data: The reconstructed, un-padded data block once complete.
    :ivar mic_ok: Result of the ``DataBlockIntKey`` MIC check, or None when the
        device holds no key and could not check.
    :ivar memory_error: Defragmentation was aborted because more uncoded
        fragments were lost than the device's ``Lmax`` allows (§A.4).
    """

    frag_index: int
    setup: FragSessionSetupReq
    decoder: FragmentationDecoder
    frames_received: int = 0
    session_cnt_committed: bool = False
    completed: bool = False
    data: bytes | None = None
    mic_ok: bool | None = None
    memory_error: bool = False
    #: True while a ``FragDataBlockReceivedReq`` is waiting for its answer (§3.5).
    ack_pending: bool = False
    #: Number of ``FragDataBlockReceivedReq`` transmissions made so far.
    ack_attempts: int = 0
    ack_payload: bytes = b""

    @property
    def nb_frag(self) -> int:
        """``NbFrag`` (``M``) of this session."""
        return self.setup.nb_frag

    @property
    def frag_size(self) -> int:
        """``FragSize`` of this session."""
        return self.setup.frag_size

    @property
    def session_cnt(self) -> int:
        """``SessionCnt`` of this session."""
        return self.setup.session_cnt

    @property
    def descriptor(self) -> int:
        """The vendor-specific 4-octet ``Descriptor`` (§3.3)."""
        return self.setup.descriptor

    @property
    def block_ack_delay(self) -> int:
        """``BlockAckDelay``, the 3-bit answer-spreading exponent (§3.3)."""
        return self.setup.block_ack_delay

    @property
    def ack_reception(self) -> bool:
        """``AckReception``: send ``FragDataBlockReceivedReq`` when done (§3.3)."""
        return self.setup.ack_reception

    @property
    def mc_group_bit_mask(self) -> int:
        """Multicast groups allowed to feed this session (§3.3)."""
        return self.setup.mc_group_bit_mask

    def progress(self) -> tuple[int, int]:
        """``(independent fragments accepted, NbFrag)`` — decoding progress.

        The first element is the rank of the parity matrix, not the raw frame
        count: it is what actually has to reach ``NbFrag`` for the block to be
        recoverable.
        """
        return (self.decoder.nb_received, self.nb_frag)

    def missing_uncoded_count(self) -> int:
        """``MissingFrag`` as reported in ``FragSessionStatusAns`` (§3.2).

        Zero once the block is reassembled; otherwise the number of uncoded
        fragments neither received directly nor reconstructed, saturated at
        :data:`MAX_MISSING_FRAG`.
        """
        if self.completed:
            return 0
        return min(len(self.decoder.missing_uncoded()), MAX_MISSING_FRAG)


class FragmentationDeviceApplication(FuotaDeviceApplication):
    """Device side of the TS004-2.0.0 Fragmented Data Block Transport package.

    Registers on :data:`FRAGMENTATION_FPORT` (201) and implements the end-device
    half of §3: it accepts up to four fragmentation sessions, feeds incoming
    ``DataFragment`` messages to a :class:`~simulator.lorawan.fuota.fragmentation.FragmentationDecoder`,
    verifies the reassembled block against the ``DataBlockIntKey`` MIC from
    ``FragSessionSetupReq``, and answers every command the server sends.

    Uplink answers go through :meth:`~simulator.lorawan.fuota.device_app.FuotaDeviceApplication.queue_uplink`,
    so with a :class:`~simulator.lorawan.device.LoRaWanDevice` attached they are
    transmitted from a child task and without one they queue up for
    :meth:`~simulator.lorawan.fuota.device_app.FuotaDeviceApplication.pop_pending_uplink`.

    Implementation decisions where TS004 leaves room (all documented in the
    module's test-suite as well):

    - **Answer spreading.** §3.2 asks a device to spread ``FragSessionStatusAns``
      over ``rand() * 2**(BlockAckDelay + 4)`` seconds because the request may
      have been multicast. ``Application.on_downlink`` only receives the
      plaintext, so the application layer cannot tell a multicast request from a
      unicast one. The delay is therefore applied to **every**
      ``FragSessionStatusAns``; on a unicast request it is harmless, only later.
    - **McGroupBitMask.** For the same reason the device application cannot see
      which address a ``DataFragment`` arrived on, so the §3.6 rule "drop a
      multicast fragment whose group was not enabled in ``McGroupBitMask``"
      cannot be enforced here. The mask is parsed, stored and exposed as
      :attr:`FragSessionState.mc_group_bit_mask` for a caller that does know the
      transport.
    - **Wrong payload length.** A ``DataFragment`` whose payload is not exactly
      ``FragSize`` octets is undefined in TS004; it is dropped silently and
      counted in :attr:`fragments_dropped`.
    - **Session does not exist + Participants.** A ``FragSessionStatusReq`` for
      an unknown ``FragIndex`` is always answered with the 1-octet
      "session does not exist" form, including when ``Participants = 0``: a
      device with no session has certainly not received the block, so it belongs
      to the "still missing fragments" set.

    Reference: LoRaWAN Fragmented Data Block Transport TS004-2.0.0 §3.
    """

    def __init__(
        self,
        device: LoRaWanDevice | None = None,
        *,
        data_block_int_key: bytes | None = None,
        gen_app_key: bytes | None = None,
        lorawan_1_1: bool = False,
        max_sessions: int = MAX_FRAG_SESSIONS,
        max_nb_frag: int | None = None,
        max_block_size: int | None = None,
        lmax: int | None = None,
        descriptor_filter: Callable[[int], bool] | None = None,
        on_block_received: Callable[[int, bytes, int], None] | None = None,
        ack_retry_interval: float | None = None,
        max_ack_retries: int = 3,
        rng: _random.Random | None = None,
    ) -> None:
        """
        :param device: Device to send uplink answers through, or None to keep
            them in the pending queue (how the codec-level tests drive it).
        :param data_block_int_key: The device's ``DataBlockIntKey`` (§3.3). When
            omitted it is derived from *gen_app_key*; with neither the device
            cannot check the data block MIC and reports ``mic_ok = None``.
        :param gen_app_key: ``GenAppKey`` (LoRaWAN 1.0.x) or ``AppKey``
            (1.1+), from which ``DataBlockIntKey`` is derived.
        :param lorawan_1_1: Selects the 1.1 derivation when *gen_app_key* is an
            ``AppKey``. See
            :func:`~simulator.lorawan.fuota.crypto.derive_data_block_int_key`.
        :param max_sessions: Fragmentation sessions the device supports, 1..4.
            A setup for a higher ``FragIndex`` is refused with
            ``FragIndex unsupported``.
        :param max_nb_frag: Largest ``NbFrag`` the device can defragment, or
            None for no limit. Exceeding it is refused with ``Not enough
            Memory``.
        :param max_block_size: Largest ``NbFrag * FragSize`` the device can
            store, or None for no limit. Also refused with ``Not enough
            Memory``.
        :param lmax: Maximum number of uncoded fragments that may be lost among
            the first ``M`` before defragmentation is abandoned (§A.4). None
            disables the check. Exceeding it aborts the session's decoding and
            raises ``MemoryError`` in ``FragSessionStatusAns``.
        :param descriptor_filter: Hook deciding whether a ``Descriptor`` is
            acceptable. Returning False refuses the setup with ``Wrong
            Descriptor``. The encoding of ``Descriptor`` is vendor-specific
            (§3.3), so TS004 defines no criterion of its own.
        :param on_block_received: Called as
            ``(frag_index, data_block, descriptor)`` the moment a block is
            reassembled, before the MIC result is consulted; inspect
            :attr:`sessions` for ``mic_ok``.
        :param ack_retry_interval: Seconds between ``FragDataBlockReceivedReq``
            retransmissions. None (the default) draws a fresh
            ``BlockAckDelay``-based delay each time, which is what §3.5
            requires.
        :param max_ack_retries: Retransmissions after the first
            ``FragDataBlockReceivedReq``. TS004 leaves the count to the
            application.
        :param rng: Random source for the answer-spreading delays.
        """
        super().__init__(device, rng)

        if not 1 <= max_sessions <= MAX_FRAG_SESSIONS:
            raise ValueError(
                f"max_sessions must be 1..{MAX_FRAG_SESSIONS}, got {max_sessions}"
            )

        if data_block_int_key is None and gen_app_key is not None:
            data_block_int_key = derive_data_block_int_key(
                key=gen_app_key, lorawan_1_1=lorawan_1_1
            )
        self.data_block_int_key: bytes | None = data_block_int_key

        self.max_sessions = max_sessions
        self.max_nb_frag = max_nb_frag
        self.max_block_size = max_block_size
        self.lmax = lmax
        self.descriptor_filter = descriptor_filter
        self.on_block_received = on_block_received
        self.ack_retry_interval = ack_retry_interval
        self.max_ack_retries = max_ack_retries

        #: Active sessions by ``FragIndex``.
        self.sessions: dict[int, FragSessionState] = {}
        #: ``SessionCntPrev[FragIndex]``, the non-volatile anti-replay counter of
        #: §3.3. -1 means "no session ever ran", so the first setup may use
        #: ``SessionCnt = 0``.
        self.last_session_cnt: dict[int, int] = {
            i: -1 for i in range(MAX_FRAG_SESSIONS)
        }
        #: Reconstructed data blocks by ``FragIndex``; survives session deletion.
        self.completed_blocks: dict[int, bytes] = {}

        #: ``DataFragment`` messages fed to a decoder.
        self.fragments_received = 0
        #: ``DataFragment`` messages discarded: no session, session already
        #: complete, or a payload that is not ``FragSize`` octets long.
        self.fragments_dropped = 0
        #: Every ``FragSessionSetupAns`` the device produced, newest last.
        self.setup_answers: list[FragSessionSetupAns] = []

    def port(self) -> int:
        """FPort 201 (§2.1)."""
        return FRAGMENTATION_FPORT

    # -- public state ------------------------------------------------------

    def is_complete(self, frag_index: int) -> bool:
        """Whether the block of *frag_index* has been fully reassembled."""
        session = self.sessions.get(frag_index)
        if session is not None:
            return session.completed
        return frag_index in self.completed_blocks

    def progress(self, frag_index: int) -> tuple[int, int]:
        """``(independent fragments accepted, NbFrag)`` for a session.

        ``(0, 0)`` when no such session exists.
        """
        session = self.sessions.get(frag_index)
        return session.progress() if session is not None else (0, 0)

    # -- downlink handling -------------------------------------------------

    async def on_downlink(self, payload: bytes) -> None:
        """Handle one FPort 201 downlink message (§3).

        Several commands may be concatenated; a ``DataFragment`` is always the
        only command in its message.
        """
        for command in parse_downlink_commands(payload):
            match command:
                case DataFragment():
                    await self._handle_data_fragment(command)
                case FragSessionSetupReq():
                    await self._handle_setup(command)
                case FragSessionStatusReq():
                    await self._handle_status_request(command)
                case FragSessionDeleteReq():
                    await self._handle_delete(command)
                case FragDataBlockReceivedAns():
                    self._handle_block_received_ans(command)
                case PackageVersionReq():
                    await self.queue_uplink(
                        encode_commands(
                            [
                                PackageVersionAns(
                                    package_identifier=PACKAGE_IDENTIFIER,
                                    package_version=PACKAGE_VERSION,
                                )
                            ]
                        )
                    )
                case _:
                    logger.debug(
                        f"{sim.current_time():.2f}s  TS004-DEV  ignoring "
                        f"{type(command).__name__} on the device side"
                    )

    # -- FragSessionSetup (§3.3) -------------------------------------------

    async def _handle_setup(self, req: FragSessionSetupReq) -> None:
        answer = self._validate_setup(req)
        self.setup_answers.append(answer)

        if answer.accepted:
            # §3.3: an accepted setup for a FragIndex that already has a session
            # stops that session and clears its context. A refused one does not.
            self.sessions[req.frag_index] = FragSessionState(
                frag_index=req.frag_index,
                setup=req,
                decoder=FragmentationDecoder(req.nb_frag, req.frag_size),
            )
            logger.info(
                f"{sim.current_time():.2f}s  TS004-DEV  session {req.frag_index} set up: "
                f"NbFrag={req.nb_frag} FragSize={req.frag_size} "
                f"Padding={req.padding} SessionCnt={req.session_cnt} "
                f"Descriptor=0x{req.descriptor:08X} "
                f"AckReception={int(req.ack_reception)} "
                f"BlockAckDelay={req.block_ack_delay}"
            )
        else:
            logger.warning(
                f"{sim.current_time():.2f}s  TS004-DEV  session {req.frag_index} setup "
                f"refused: StatusBitMask=0x{answer.encode_payload()[0]:02X}"
            )

        # Setup is unicast only (§3.3), so no answer spreading applies.
        await self.queue_uplink(encode_commands([answer]))

    def _validate_setup(self, req: FragSessionSetupReq) -> FragSessionSetupAns:
        """Apply the §3.3 acceptance rules and build the ``StatusBitMask``."""
        answer = FragSessionSetupAns(frag_index=req.frag_index)

        if req.frag_algo != 0:
            # 0 is the Annex A FEC code; 1..7 are RFU (§3.3, Table 11).
            answer.frag_algo_unsupported = True

        if req.frag_index >= self.max_sessions:
            answer.frag_index_unsupported = True

        if self.max_nb_frag is not None and req.nb_frag > self.max_nb_frag:
            answer.not_enough_memory = True
        if (
            self.max_block_size is not None
            and req.nb_frag * req.frag_size > self.max_block_size
        ):
            answer.not_enough_memory = True

        if self.descriptor_filter is not None and not self.descriptor_filter(
            req.descriptor
        ):
            answer.wrong_descriptor = True

        if req.session_cnt <= self.last_session_cnt.get(req.frag_index, -1):
            # §3.3: SessionCnt <= SessionCntPrev[FragIndex] is a replay.
            answer.session_cnt_replay = True

        return answer

    # -- DataFragment (§3.6) -----------------------------------------------

    async def _handle_data_fragment(self, fragment: DataFragment) -> None:
        session = self.sessions.get(fragment.frag_index)

        if session is None:
            self.fragments_dropped += 1
            logger.debug(
                f"{sim.current_time():.2f}s  TS004-DEV  fragment N={fragment.index_n} "
                f"for unknown session {fragment.frag_index}, dropped"
            )
            return

        if session.completed or session.memory_error:
            # §3.6: once the block is reconstructed the device SHALL drop any
            # further message using that FragIndex until the session is deleted.
            self.fragments_dropped += 1
            return

        if len(fragment.payload) != session.frag_size:
            # Undefined in TS004; drop silently (see the class docstring).
            self.fragments_dropped += 1
            logger.warning(
                f"{sim.current_time():.2f}s  TS004-DEV  fragment N={fragment.index_n} "
                f"is {len(fragment.payload)} octets, expected {session.frag_size}; "
                f"dropped"
            )
            return

        if not session.session_cnt_committed:
            # §3.3: SessionCntPrev is committed on the first DataFragment of the
            # new session, not when the setup is accepted.
            self.last_session_cnt[session.frag_index] = session.session_cnt
            session.session_cnt_committed = True

        self.fragments_received += 1
        session.frames_received += 1
        complete = session.decoder.receive(fragment.index_n, fragment.payload)

        if not complete and self.lmax is not None:
            lost = len(session.decoder.missing_uncoded())
            if lost > self.lmax:
                session.memory_error = True
                logger.warning(
                    f"{sim.current_time():.2f}s  TS004-DEV  session "
                    f"{session.frag_index} aborted: {lost} uncoded fragments lost, "
                    f"Lmax={self.lmax}"
                )
                return

        if complete:
            await self._complete_session(session)

    async def _complete_session(self, session: FragSessionState) -> None:
        """Reassemble, verify the MIC and raise the completion callbacks (§3.3)."""
        data = session.decoder.reconstruct(session.setup.padding)
        session.data = data
        session.completed = True
        self.completed_blocks[session.frag_index] = data

        if self.data_block_int_key is not None:
            session.mic_ok = session.setup.verify_mic(self.data_block_int_key, data)
        else:
            session.mic_ok = None

        logger.info(
            f"{sim.current_time():.2f}s  TS004-DEV  session {session.frag_index} "
            f"complete: {len(data)} octets from {session.frames_received} fragment(s), "
            f"MIC="
            + ("unchecked" if session.mic_ok is None else ("ok" if session.mic_ok else "FAILED"))
        )

        if self.on_block_received is not None:
            self.on_block_received(session.frag_index, data, session.descriptor)

        if session.ack_reception:
            await self._start_block_received(session)

    # -- FragDataBlockReceived (§3.5) --------------------------------------

    def _ack_delay(self, session: FragSessionState) -> float:
        """Delay before (re)transmitting ``FragDataBlockReceivedReq`` (§3.5)."""
        if self.ack_retry_interval is not None:
            return self.ack_retry_interval
        return block_ack_delay_seconds(session.block_ack_delay, self.rng)

    async def _start_block_received(self, session: FragSessionState) -> None:
        """Begin the ``FragDataBlockReceivedReq`` / Ans exchange (§3.5)."""
        session.ack_payload = encode_commands(
            [
                FragDataBlockReceivedReq(
                    frag_index=session.frag_index,
                    mic_error=session.mic_ok is False,
                )
            ]
        )
        session.ack_pending = True

        if self.device is not None and sim.is_running():
            await sim.start_child_task(self._block_received_loop(session))
            return

        # No device attached: queue a single request for pop_pending_uplink().
        await self.queue_uplink(session.ack_payload, delay=self._ack_delay(session))
        session.ack_attempts = 1

    async def _block_received_loop(self, session: FragSessionState) -> None:
        """Send ``FragDataBlockReceivedReq`` until it is answered (§3.5).

        The first delay and every retransmission interval follow the
        ``BlockAckDelay`` rule unless :attr:`ack_retry_interval` overrides it.
        TS004 leaves the retry count to the application: it is
        :attr:`max_ack_retries` here.
        """
        try:
            for attempt in range(self.max_ack_retries + 1):
                delay = self._ack_delay(session)
                if delay > 0:
                    await sim.sleep(delay)
                if not session.ack_pending:
                    return
                await self.queue_uplink(session.ack_payload)
                session.ack_attempts = attempt + 1
            logger.warning(
                f"{sim.current_time():.2f}s  TS004-DEV  session {session.frag_index} "
                f"FragDataBlockReceivedReq unanswered after "
                f"{session.ack_attempts} transmission(s)"
            )
        except SimulatorException:
            return

    def _handle_block_received_ans(self, ans: FragDataBlockReceivedAns) -> None:
        session = self.sessions.get(ans.frag_index)
        if session is None or not session.ack_pending:
            return
        session.ack_pending = False
        logger.info(
            f"{sim.current_time():.2f}s  TS004-DEV  session {ans.frag_index} "
            f"data block reception acknowledged by the server"
        )

    # -- FragSessionStatus (§3.2) ------------------------------------------

    async def _handle_status_request(self, req: FragSessionStatusReq) -> None:
        session = self.sessions.get(req.frag_index)

        if session is None:
            # The 1-octet form; see the class docstring for the Participants
            # reading applied here.
            answer = FragSessionStatusAns(
                frag_index=req.frag_index, session_does_not_exist=True
            )
        else:
            if session.completed and not req.participants:
                # §3.2 Participants = 0: only receivers still missing fragments
                # SHALL answer.
                logger.debug(
                    f"{sim.current_time():.2f}s  TS004-DEV  status request for a "
                    f"completed session {req.frag_index} with Participants=0, "
                    f"staying silent"
                )
                return
            missing = session.missing_uncoded_count()
            answer = FragSessionStatusAns(
                frag_index=req.frag_index,
                nb_frag_received=session.frames_received,
                missing_frag=missing,
                memory_error=session.memory_error,
                # §3.2: MICError is meaningful only when MissingFrag == 0.
                mic_error=missing == 0 and session.mic_ok is False,
            )

        # Always spread the answer: the application layer cannot tell whether
        # the request arrived multicast (see the class docstring).
        delay = block_ack_delay_seconds(
            session.block_ack_delay if session is not None else 0, self.rng
        )
        await self.queue_uplink(encode_commands([answer]), delay=delay)

    # -- FragSessionDelete (§3.4) ------------------------------------------

    async def _handle_delete(self, req: FragSessionDeleteReq) -> None:
        session = self.sessions.pop(req.frag_index, None)
        if session is not None:
            session.ack_pending = False
            logger.info(
                f"{sim.current_time():.2f}s  TS004-DEV  session {req.frag_index} deleted"
            )
        answer = FragSessionDeleteAns(
            frag_index=req.frag_index, session_does_not_exist=session is None
        )
        await self.queue_uplink(encode_commands([answer]))


# ---------------------------------------------------------------------------
# Network-server side application (TS004-2.0.0 §3, FPort 201)
# ---------------------------------------------------------------------------


@dataclass
class FragServerSession:
    """A fragmentation session as the network server tracks it.

    Created by :meth:`FragmentationServerApplication.create_session`, which also
    builds the :class:`~simulator.lorawan.fuota.fragmentation.FragmentationEncoder`
    the fragments are generated from. ``NbFrag`` and ``Padding`` come from the
    encoder, so they always agree with the fragments actually transmitted.

    :ivar redundancy_fragments: Coded fragments planned on top of ``NbFrag``.
        ``nb_frag + redundancy_fragments`` is what :meth:`broadcast_fragments`
        sends by default; more can always be scheduled later with a higher
        ``start_n`` because the encoder generates parity lines on the fly.
    :ivar setup_answers: ``FragSessionSetupAns`` received per device.
    :ivar completed: Devices that reported ``FragDataBlockReceivedReq``.
    :ivar highest_n_sent: Largest ``N`` scheduled so far, so a repair round
        knows where to continue.
    """

    frag_index: int
    encoder: FragmentationEncoder
    data: bytes
    dev_addrs: list[int]
    mc_group_bit_mask: int = 0
    descriptor: int = 0
    session_cnt: int = 0
    block_ack_delay: int = 0
    ack_reception: bool = False
    frag_algo: int = 0
    redundancy_fragments: int = 0
    setup_answers: dict[int, FragSessionSetupAns] = field(default_factory=dict)
    delete_answers: dict[int, FragSessionDeleteAns] = field(default_factory=dict)
    completed: set[int] = field(default_factory=set)
    mic_errors: set[int] = field(default_factory=set)
    fragments_scheduled: int = 0
    highest_n_sent: int = 0

    @property
    def nb_frag(self) -> int:
        """``NbFrag`` (``M``)."""
        return self.encoder.nb_frag

    @property
    def frag_size(self) -> int:
        """``FragSize``."""
        return self.encoder.frag_size

    @property
    def padding(self) -> int:
        """``Padding`` octets in the last uncoded fragment."""
        return self.encoder.padding

    @property
    def total_fragments(self) -> int:
        """Fragments planned in total: ``NbFrag + redundancy_fragments``."""
        return self.nb_frag + self.redundancy_fragments

    def setup_request(self) -> FragSessionSetupReq:
        """The ``FragSessionSetupReq`` for this session, **without** its MIC.

        The MIC is per device (``DataBlockIntKey`` is device specific), so
        :meth:`FragmentationServerApplication.create_session` fills it in with
        :meth:`FragSessionSetupReq.with_mic` once per target.
        """
        return FragSessionSetupReq(
            frag_index=self.frag_index,
            mc_group_bit_mask=self.mc_group_bit_mask,
            nb_frag=self.nb_frag,
            frag_size=self.frag_size,
            ack_reception=self.ack_reception,
            frag_algo=self.frag_algo,
            block_ack_delay=self.block_ack_delay,
            padding=self.padding,
            descriptor=self.descriptor,
            session_cnt=self.session_cnt,
        )


@dataclass
class FragStatusReport:
    """One ``FragSessionStatusAns`` as received by the server (§3.2)."""

    dev_addr: int
    frag_index: int
    nb_frag_received: int
    missing_frag: int
    memory_error: bool
    mic_error: bool
    session_does_not_exist: bool
    time: float

    @property
    def complete(self) -> bool:
        """Whether the device reported the block as fully reassembled."""
        return not self.session_does_not_exist and self.missing_frag == 0


class FragmentationServerApplication(Application):
    """Network-server side of the TS004-2.0.0 fragmentation package (FPort 201).

    Drives a fragmentation session end to end:

    1. :meth:`create_session` splits a data block, queues a per-device
       ``FragSessionSetupReq`` carrying that device's ``DataBlockIntKey`` MIC,
       and keeps the encoder for later.
    2. :meth:`broadcast_fragments` schedules ``DataFragment`` messages on a
       multicast group through
       :meth:`~simulator.lorawan.network_server.NetworkServer.schedule_multicast_downlink`
       — one fragment per frame, ``N`` increasing from ``start_n``.
    3. :meth:`request_status` asks the fleet how far it got;
       :meth:`max_missing` turns the answers into "how many more coded
       fragments do I need to send", which feeds a repair round of
       :meth:`broadcast_fragments` with a higher ``start_n``.
    4. :meth:`delete_session` tears the session down.

    Unicast commands are handed to the network server through
    :meth:`get_downlink`, so they ride on the device's next uplink like any
    other application payload, and several of them may queue up per device.

    Reference: LoRaWAN Fragmented Data Block Transport TS004-2.0.0 §3.
    """

    def __init__(
        self,
        network_server: NetworkServer,
        *,
        key_provider: Callable[[int], bytes | None] | dict[int, bytes] | None = None,
        time_provider: Callable[[], float] | None = None,
    ) -> None:
        """
        :param network_server: The server whose downlink queues and multicast
            scheduler this package drives.
        :param key_provider: Maps a ``DevAddr`` to that device's
            ``DataBlockIntKey`` (§3.3), either as a callable or a dict. A device
            with no key gets a ``FragSessionSetupReq`` with a zero MIC, which the
            device will then fail to verify — useful for negative tests.
        :param time_provider: Source of the timestamps stamped on
            :class:`FragStatusReport`; defaults to the simulation clock.
        """
        self.network_server = network_server
        self.time_provider: Callable[[], float] = (
            time_provider if time_provider is not None else sim.current_time
        )

        if key_provider is None:
            self._key_provider: Callable[[int], bytes | None] = lambda _addr: None
        elif isinstance(key_provider, dict):
            keys = dict(key_provider)
            self._key_provider = keys.get
        else:
            self._key_provider = key_provider

        #: Sessions by ``FragIndex``.
        self.sessions: dict[int, FragServerSession] = {}
        #: Every ``FragSessionStatusAns`` received, oldest first.
        self.status_reports: list[FragStatusReport] = []
        #: Latest report per ``(FragIndex, DevAddr)``.
        self.latest_status: dict[tuple[int, int], FragStatusReport] = {}
        #: Called as ``(dev_addr, command)`` for every parsed uplink command.
        self.on_answer: Callable[[int, FragCommandType], None] | None = None

        self._pending: dict[int, deque[bytes]] = defaultdict(deque)

    def port(self) -> int:
        """FPort 201 (§2.1)."""
        return FRAGMENTATION_FPORT

    # -- sizing ------------------------------------------------------------

    @staticmethod
    def fragment_payload_size_for(data_rate: int, fopts_len: int = 0) -> int:
        """Largest ``FragSize`` that still fits one frame at *data_rate* (§3.6).

        The region's maximum application payload minus the 3 octets of
        ``DataFragment`` overhead. For EU868 with empty FOpts this is 39 octets
        at DR0–DR2, 103 at DR3 and 210 at DR4–DR5.
        """
        return max_fragment_payload(max_frm_payload(data_rate, fopts_len))

    # -- session management ------------------------------------------------

    def create_session(
        self,
        dev_addrs: list[int],
        *,
        frag_index: int,
        data: bytes,
        frag_size: int,
        session_cnt: int,
        mc_group_bit_mask: int = 0,
        redundancy_fragments: int | None = None,
        redundancy_ratio: float | None = None,
        descriptor: int = 0,
        block_ack_delay: int = 0,
        ack_reception: bool = False,
        frag_algo: int = 0,
    ) -> FragServerSession:
        """Create a session and queue a ``FragSessionSetupReq`` per device (§3.3).

        The data block is split with
        :class:`~simulator.lorawan.fuota.fragmentation.FragmentationEncoder`, so
        ``NbFrag`` and ``Padding`` are derived from *data* and *frag_size* rather
        than passed in. Each device gets its own copy of the request, MIC'd with
        its ``DataBlockIntKey``.

        :param dev_addrs: Devices taking part in the session.
        :param frag_index: ``FragIndex``, 0..3.
        :param data: The data block to transport.
        :param frag_size: ``FragSize`` in octets; check it against
            :meth:`fragment_payload_size_for`.
        :param session_cnt: ``SessionCnt``; must be strictly greater than the one
            used for the previous session on this ``FragIndex`` (§3.3).
        :param mc_group_bit_mask: Multicast groups allowed to feed the session.
            Bit *X* enables ``McGroupID = X``; unicast is always allowed.
        :param redundancy_fragments: Coded fragments to plan on top of
            ``NbFrag``. Mutually exclusive with *redundancy_ratio*.
        :param redundancy_ratio: Redundancy as a fraction of ``NbFrag``, rounded
            up (0.2 → 20% extra fragments). §A.3 suggests ``M + 2`` on average
            and ``M + 7`` for 99% success, on top of the expected losses.
        :param descriptor: Vendor-specific 4-octet ``Descriptor``.
        :param block_ack_delay: ``BlockAckDelay``, 0..7, the answer-spreading
            exponent the devices apply.
        :param ack_reception: Ask the devices for ``FragDataBlockReceivedReq``.
        :param frag_algo: ``FragAlgo``; 0 is the only defined value.
        :returns: The new :class:`FragServerSession`.
        """
        if redundancy_fragments is not None and redundancy_ratio is not None:
            raise ValueError(
                "pass either redundancy_fragments or redundancy_ratio, not both"
            )

        encoder = FragmentationEncoder(data, frag_size)

        if redundancy_ratio is not None:
            if redundancy_ratio < 0:
                raise ValueError(
                    f"redundancy_ratio must be >= 0, got {redundancy_ratio}"
                )
            redundancy = math.ceil(encoder.nb_frag * redundancy_ratio)
        else:
            redundancy = redundancy_fragments if redundancy_fragments is not None else 0

        session = FragServerSession(
            frag_index=_check_frag_index(frag_index),
            encoder=encoder,
            data=bytes(data),
            dev_addrs=list(dev_addrs),
            mc_group_bit_mask=mc_group_bit_mask,
            descriptor=descriptor,
            session_cnt=session_cnt,
            block_ack_delay=block_ack_delay,
            ack_reception=ack_reception,
            frag_algo=frag_algo,
            redundancy_fragments=redundancy,
        )
        self.sessions[frag_index] = session

        base = session.setup_request()
        for dev_addr in session.dev_addrs:
            key = self._key_provider(dev_addr)
            request = base.with_mic(key, session.data) if key is not None else base
            if key is None:
                logger.warning(
                    f"{self.time_provider():.2f}s  TS004-NS  no DataBlockIntKey for "
                    f"0x{dev_addr:08X}; FragSessionSetupReq goes out with a zero MIC"
                )
            self._queue(dev_addr, encode_commands([request]))

        logger.info(
            f"{self.time_provider():.2f}s  TS004-NS  session {frag_index} created: "
            f"{len(session.data)} octets -> NbFrag={session.nb_frag} "
            f"FragSize={session.frag_size} Padding={session.padding} "
            f"(+{redundancy} redundancy) for {len(session.dev_addrs)} device(s)"
        )
        return session

    def request_status(
        self, dev_addrs: list[int], frag_index: int, participants: bool = False
    ) -> bytes:
        """Queue a unicast ``FragSessionStatusReq`` to each device (§3.2).

        :param participants: False → only devices still missing fragments answer;
            True → every device answers, a full roll call.
        :returns: The encoded message, which is also what a caller would put on a
            multicast group to poll the whole fleet at once.
        """
        payload = encode_commands(
            [
                FragSessionStatusReq(
                    frag_index=_check_frag_index(frag_index), participants=participants
                )
            ]
        )
        for dev_addr in dev_addrs:
            self._queue(dev_addr, payload)
        logger.info(
            f"{self.time_provider():.2f}s  TS004-NS  status requested for session "
            f"{frag_index} from {len(dev_addrs)} device(s) "
            f"(Participants={int(participants)})"
        )
        return payload

    def delete_session(self, dev_addrs: list[int], frag_index: int) -> bytes:
        """Queue a unicast ``FragSessionDeleteReq`` to each device (§3.4)."""
        payload = encode_commands(
            [FragSessionDeleteReq(frag_index=_check_frag_index(frag_index))]
        )
        for dev_addr in dev_addrs:
            self._queue(dev_addr, payload)
        logger.info(
            f"{self.time_provider():.2f}s  TS004-NS  session {frag_index} delete "
            f"requested from {len(dev_addrs)} device(s)"
        )
        return payload

    def request_package_version(self, dev_addrs: list[int]) -> bytes:
        """Queue a unicast ``PackageVersionReq`` to each device (§3.1)."""
        payload = encode_commands([PackageVersionReq()])
        for dev_addr in dev_addrs:
            self._queue(dev_addr, payload)
        return payload

    # -- fragment transmission ---------------------------------------------

    def broadcast_fragments(
        self,
        frag_index: int,
        *,
        group_addr: int,
        start_time: float,
        interval: float,
        count: int | None = None,
        start_n: int = 1,
    ) -> list[ScheduledMulticastDownlink]:
        """Schedule ``DataFragment`` messages on a multicast group (§3.6).

        One fragment per frame, ``N`` running from *start_n*, spaced *interval*
        seconds apart from *start_time*. The gateway's own duty-cycle limiter may
        stretch the spacing further; the schedule is a request, not a guarantee.

        Because the encoder generates parity lines on the fly, a repair round is
        just another call with ``start_n`` past the highest ``N`` already sent —
        for example ``start_n=session.highest_n_sent + 1``.

        :param count: Fragments to schedule. None sends the rest of the planned
            ``NbFrag + redundancy_fragments`` from *start_n* onwards.
        :returns: The queue entries, in transmission order.
        """
        session = self._session(frag_index)
        if start_n < 1:
            raise ValueError(f"fragment index N is 1-based, got {start_n}")
        if count is None:
            count = max(0, session.total_fragments - start_n + 1)

        entries: list[ScheduledMulticastDownlink] = []
        for k in range(count):
            n = start_n + k
            payload = encode_commands(
                [
                    DataFragment(
                        frag_index=session.frag_index,
                        index_n=n,
                        payload=session.encoder.fragment(n),
                    )
                ]
            )
            entries.append(
                self.network_server.schedule_multicast_downlink(
                    group_addr,
                    fport=FRAGMENTATION_FPORT,
                    payload=payload,
                    at_time=start_time + k * interval,
                )
            )
            session.highest_n_sent = max(session.highest_n_sent, n)

        session.fragments_scheduled += count
        logger.info(
            f"{self.time_provider():.2f}s  TS004-NS  session {frag_index}: "
            f"{count} fragment(s) N={start_n}..{start_n + count - 1} scheduled on "
            f"0x{group_addr:08X} from {start_time:.2f}s every {interval:.2f}s"
        )
        return entries

    def send_fragment_unicast(self, dev_addr: int, frag_index: int, n: int) -> bytes:
        """Queue a single ``DataFragment`` as a unicast downlink (§3.6).

        Unicast is always an allowed fragment source regardless of
        ``McGroupBitMask``, which makes this the natural repair path for a
        device that is far behind the rest of the fleet.

        The frame goes through
        :meth:`~simulator.lorawan.network_server.NetworkServer.queue_downlink`,
        whose explicit queue is served before any application, so a repair
        fragment is not held up behind this package's own pending commands.
        """
        session = self._session(frag_index)
        payload = encode_commands(
            [
                DataFragment(
                    frag_index=session.frag_index,
                    index_n=n,
                    payload=session.encoder.fragment(n),
                )
            ]
        )
        self.network_server.queue_downlink(
            dev_addr, fport=FRAGMENTATION_FPORT, payload=payload
        )
        session.highest_n_sent = max(session.highest_n_sent, n)
        return payload

    # -- downlink / uplink plumbing ----------------------------------------

    def _queue(self, dev_addr: int, payload: bytes) -> None:
        self._pending[dev_addr].append(payload)

    async def get_downlink(self, dev_addr: int) -> bytes | None:
        """Hand the network server this package's next command for a device."""
        queue = self._pending.get(dev_addr)
        if not queue:
            return None
        return queue.popleft()

    def pending_downlinks(self, dev_addr: int) -> list[bytes]:
        """Queued but unsent commands for a device, oldest first (read-only)."""
        return list(self._pending.get(dev_addr, ()))

    async def on_uplink(self, dev_addr: int, payload: bytes) -> None:
        """Process a FPort 201 uplink from a device (§3)."""
        for command in parse_uplink_commands(payload):
            match command:
                case FragSessionSetupAns():
                    self._handle_setup_ans(dev_addr, command)
                case FragSessionStatusAns():
                    self._handle_status_ans(dev_addr, command)
                case FragDataBlockReceivedReq():
                    self._handle_block_received(dev_addr, command)
                case FragSessionDeleteAns():
                    self._handle_delete_ans(dev_addr, command)
                case PackageVersionAns():
                    logger.info(
                        f"{self.time_provider():.2f}s  TS004-NS  0x{dev_addr:08X} "
                        f"runs package {command.package_identifier} "
                        f"version {command.package_version}"
                    )
                case _:
                    logger.debug(
                        f"{self.time_provider():.2f}s  TS004-NS  ignoring "
                        f"{type(command).__name__} from 0x{dev_addr:08X}"
                    )
            if self.on_answer is not None:
                self.on_answer(dev_addr, command)

    def _handle_setup_ans(self, dev_addr: int, ans: FragSessionSetupAns) -> None:
        session = self.sessions.get(ans.frag_index)
        if session is not None:
            session.setup_answers[dev_addr] = ans
        if ans.accepted:
            logger.info(
                f"{self.time_provider():.2f}s  TS004-NS  0x{dev_addr:08X} accepted "
                f"session {ans.frag_index}"
            )
        else:
            logger.warning(
                f"{self.time_provider():.2f}s  TS004-NS  0x{dev_addr:08X} REFUSED "
                f"session {ans.frag_index}: StatusBitMask="
                f"0x{ans.encode_payload()[0]:02X}"
            )

    def _handle_status_ans(self, dev_addr: int, ans: FragSessionStatusAns) -> None:
        report = FragStatusReport(
            dev_addr=dev_addr,
            frag_index=ans.frag_index,
            nb_frag_received=ans.nb_frag_received,
            missing_frag=ans.missing_frag,
            memory_error=ans.memory_error,
            mic_error=ans.mic_error,
            session_does_not_exist=ans.session_does_not_exist,
            time=self.time_provider(),
        )
        self.status_reports.append(report)
        self.latest_status[(ans.frag_index, dev_addr)] = report
        logger.info(
            f"{self.time_provider():.2f}s  TS004-NS  0x{dev_addr:08X} session "
            f"{ans.frag_index}: received={ans.nb_frag_received} "
            f"missing={ans.missing_frag} "
            f"memory_error={int(ans.memory_error)} mic_error={int(ans.mic_error)} "
            f"no_session={int(ans.session_does_not_exist)}"
        )

    def _handle_block_received(
        self, dev_addr: int, req: FragDataBlockReceivedReq
    ) -> None:
        session = self.sessions.get(req.frag_index)
        if session is not None:
            session.completed.add(dev_addr)
            if req.mic_error:
                session.mic_errors.add(dev_addr)

        if req.mic_error:
            logger.warning(
                f"{self.time_provider():.2f}s  TS004-NS  0x{dev_addr:08X} completed "
                f"session {req.frag_index} but reports a MIC ERROR"
            )
        else:
            logger.info(
                f"{self.time_provider():.2f}s  TS004-NS  0x{dev_addr:08X} completed "
                f"session {req.frag_index}, MIC ok"
            )

        # §3.5: the answer SHALL echo the FragIndex of the request.
        self._queue(
            dev_addr,
            encode_commands([FragDataBlockReceivedAns(frag_index=req.frag_index)]),
        )

    def _handle_delete_ans(self, dev_addr: int, ans: FragSessionDeleteAns) -> None:
        session = self.sessions.get(ans.frag_index)
        if session is not None:
            session.delete_answers[dev_addr] = ans
        logger.info(
            f"{self.time_provider():.2f}s  TS004-NS  0x{dev_addr:08X} deleted session "
            f"{ans.frag_index}"
            + ("" if ans.accepted else " (it did not exist)")
        )

    # -- fleet view --------------------------------------------------------

    def _session(self, frag_index: int) -> FragServerSession:
        session = self.sessions.get(frag_index)
        if session is None:
            raise KeyError(f"no fragmentation session with FragIndex {frag_index}")
        return session

    def devices_acked_setup(self, frag_index: int) -> set[int]:
        """Devices that accepted the ``FragSessionSetupReq`` (§3.3)."""
        session = self.sessions.get(frag_index)
        if session is None:
            return set()
        return {
            addr for addr, ans in session.setup_answers.items() if ans.accepted
        }

    def devices_complete(self, frag_index: int) -> set[int]:
        """Devices known to hold the whole data block.

        A device counts as complete when it sent ``FragDataBlockReceivedReq``
        (§3.5) or when its last ``FragSessionStatusAns`` reported
        ``MissingFrag == 0`` (§3.2). With ``AckReception = 0`` and no status
        round the server has no way of knowing, which is exactly why TS004 v2.0.0
        added CID 0x04.
        """
        session = self.sessions.get(frag_index)
        complete = set(session.completed) if session is not None else set()
        for (index, dev_addr), report in self.latest_status.items():
            if index == frag_index and report.complete:
                complete.add(dev_addr)
        return complete

    def max_missing(self, frag_index: int) -> int:
        """Largest ``MissingFrag`` across the latest status answers (§3.2).

        The number of *independent* coded fragments the worst-off device still
        needs, and therefore the minimum size of a repair round. §A.3 suggests
        budgeting a couple of fragments on top, since a coded fragment can turn
        out to be linearly dependent.
        """
        return max(
            (
                report.missing_frag
                for (index, _addr), report in self.latest_status.items()
                if index == frag_index and not report.session_does_not_exist
            ),
            default=0,
        )
