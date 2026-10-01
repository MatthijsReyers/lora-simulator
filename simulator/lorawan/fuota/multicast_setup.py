"""TS005 v2.0.0 Remote Multicast Setup package — wire codecs (FPort 200).

Pure, dependency-free encoders/decoders for the application-layer commands of
the LoRa Alliance specification *LoRaWAN Remote Multicast Setup*,
**TS005-2.0.0** (FINAL, April 2022):

- §4.1 ``PackageVersionReq`` / ``PackageVersionAns``      (CID 0x00)
- §4.2 ``McGroupStatusReq`` / ``McGroupStatusAns``        (CID 0x01)
- §4.3 ``McGroupSetupReq`` / ``McGroupSetupAns``          (CID 0x02)
- §4.4 ``McGroupDeleteReq`` / ``McGroupDeleteAns``        (CID 0x03)
- §4.5 ``McClassCSessionReq`` / ``McClassCSessionAns``    (CID 0x04)
- §4.6 ``McClassBSessionReq`` / ``McClassBSessionAns``    (CID 0x05)

This module contains **no simulator state**: no devices, no frames, no keys.
Key derivation lives in :mod:`simulator.lorawan.fuota.crypto` (TS005 §4.3), and
the device/server state machines are built on top of these codecs elsewhere.

Conventions (normative, TS005-2.0.0 §1.5 "Conventions"):

- All multi-octet fields are **little endian** (``McAddr``, ``minMcFCnt``,
  ``maxMcFCnt``, ``SessionTime``, ``DLFreq``, ``TimeToStart``).
- RFU bits **SHALL** be set to 0 by the transmitter and **silently ignored** by
  the receiver.  The one documented exception is bits 7:6 of
  ``McClassB/CSessionAns.Status&McGroupID`` ("Reserved for future errors",
  Tables 20 and 25), which SHALL be treated as *errors* when set — modelled
  here by :attr:`McClassCSessionAns.reserved_errors`.
- A single application payload MAY carry several commands concatenated as
  ``CID | payload | CID | payload | …`` (§3); they are executed first to last
  and each is individually acknowledged on the same FPort.
- All TS005 messages SHALL be sent unicast; a device SHALL silently drop them
  when received on a multicast address (§3).  That rule is enforced by the
  callers of this module, not by the codecs.

Implementation decisions where TS005-2.0.0 is silent (see also the digest of
ambiguities):

1. ``McGroupStatusAns`` record ordering is not stated normatively.  The
   "discard the highest ``McGroupID`` first" rule of §4.2 implies **ascending
   ``McGroupID``**; :meth:`McGroupStatusAns.encode_payload` does not reorder
   the caller's list but :func:`parse_uplink_commands` preserves wire order.
2. ``DLFreq`` is a frequency *in units of 100 Hz* on the wire.  This module
   stores and exposes ``dl_frequency`` in **Hz** and rejects values that are
   not an exact multiple of 100 Hz, rather than silently rounding.
3. Decoders raise :class:`ValueError` on a wrong payload length.  Encoders
   raise :class:`ValueError` on out-of-range field values.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import IntEnum
from typing import ClassVar

logger = logging.getLogger(__name__)


__all__ = [
    "MULTICAST_SETUP_FPORT",
    "PACKAGE_IDENTIFIER",
    "PACKAGE_VERSION",
    "MAX_MULTICAST_GROUPS",
    "TIME_TO_START_UNSYNCHRONIZED",
    "McCID",
    "MulticastSetupCommand",
    "PackageVersionReq",
    "PackageVersionAns",
    "McGroupStatusReq",
    "McGroupStatusEntry",
    "McGroupStatusAns",
    "McGroupSetupReq",
    "McGroupSetupAns",
    "McGroupDeleteReq",
    "McGroupDeleteAns",
    "McClassCSessionReq",
    "McClassCSessionAns",
    "McClassBSessionReq",
    "McClassBSessionAns",
    "MulticastSetupCommandType",
    "parse_downlink_commands",
    "parse_uplink_commands",
    "encode_commands",
]


#: RECOMMENDED default FPort for the Remote Multicast Setup package (§3).
MULTICAST_SETUP_FPORT = 200

#: ``PackageIdentifier`` of the Remote Multicast Setup package (§3, §4.1).
PACKAGE_IDENTIFIER = 2

#: ``PackageVersion`` implemented here, i.e. TS005 **v2.0.0** (§4.1).
PACKAGE_VERSION = 2

#: Maximum number of simultaneous multicast contexts, ``McGroupID`` 0..3 (§4.3).
#: A device SHALL support at least 1; if it supports *N*, ``McGroupID`` SHALL
#: be in ``[0 : N-1]``.
MAX_MULTICAST_GROUPS = 4

#: ``TimeToStart`` value meaning "more than 2**24-1 seconds away", which tells
#: the Application Server the device clock is out of synchronisation (§4.5).
TIME_TO_START_UNSYNCHRONIZED = 0xFFFFFF


class McCID(IntEnum):
    """Remote Multicast Setup command identifiers (TS005-2.0.0 §3, Table 2)."""

    PACKAGE_VERSION = 0x00
    MC_GROUP_STATUS = 0x01
    MC_GROUP_SETUP = 0x02
    MC_GROUP_DELETE = 0x03
    MC_CLASS_C_SESSION = 0x04
    MC_CLASS_B_SESSION = 0x05


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

def _check_uint(name: str, value: int, bits: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{name} must be an int, got {value!r}")
    if not 0 <= value < (1 << bits):
        raise ValueError(f"{name} must fit in {bits} bits (0..{(1 << bits) - 1}), got {value}")
    return value


def _check_group_id(group_id: int) -> int:
    """``McGroupID`` is 2 bits wide and valid in ``[0 : MAX_MULTICAST_GROUPS-1]``."""
    if not isinstance(group_id, int) or isinstance(group_id, bool):
        raise ValueError(f"group_id must be an int, got {group_id!r}")
    if not 0 <= group_id < MAX_MULTICAST_GROUPS:
        raise ValueError(
            f"group_id must be 0..{MAX_MULTICAST_GROUPS - 1} (TS005 §4.3), got {group_id}"
        )
    return group_id


def _encode_dl_frequency(dl_frequency: int) -> bytes:
    """Encode ``DLFreq``: 24-bit little endian, in units of 100 Hz (§4.5, §4.6).

    Same meaning and coding as the ``NewChannelReq`` ``Freq`` field (Class C)
    and the ``BeaconFreqReq`` ``Frequency`` field (Class B), so
    ``actual frequency [Hz] = DLFreq * 100``.  ``DLFreq = 0`` is legal and, for
    Class B, selects the region's default beacon frequency-hopping scheme.
    """
    if not isinstance(dl_frequency, int) or isinstance(dl_frequency, bool):
        raise ValueError(f"dl_frequency must be an int, got {dl_frequency!r}")
    if dl_frequency % 100 != 0:
        raise ValueError(
            f"dl_frequency must be a multiple of 100 Hz (DLFreq is in units of "
            f"100 Hz, TS005 §4.5), got {dl_frequency}"
        )
    raw = dl_frequency // 100
    if not 0 <= raw <= 0xFFFFFF:
        raise ValueError(
            f"dl_frequency must be 0..{0xFFFFFF * 100} Hz (DLFreq is 24 bits), "
            f"got {dl_frequency}"
        )
    return raw.to_bytes(3, "little")


# ---------------------------------------------------------------------------
# Base
# ---------------------------------------------------------------------------

@dataclass
class MulticastSetupCommand:
    """Base class for all TS005 Remote Multicast Setup commands.

    ``encode()`` always emits ``CID | payload``; ``encoded_payload_size`` is the
    length of the payload *without* the CID octet.  Fixed-size commands also
    expose it as the class attribute ``PAYLOAD_SIZE``; variable-length answers
    (``McGroupStatusAns``, ``McClassB/CSessionAns``) set ``PAYLOAD_SIZE`` to
    ``None`` and compute the size from their own fields.
    """

    #: Payload size in octets, or ``None`` when the command is variable length.
    PAYLOAD_SIZE: ClassVar[int | None] = None

    cid: McCID

    def encode_payload(self) -> bytes:
        """Encode the command payload (without the CID octet)."""
        return b""

    def encode(self) -> bytes:
        """Encode the full command: CID octet followed by the payload."""
        return bytes([self.cid]) + self.encode_payload()

    @property
    def encoded_payload_size(self) -> int:
        """Size in octets of this command's payload (CID octet excluded)."""
        return len(self.encode_payload())

    @property
    def encoded_size(self) -> int:
        """Size in octets of the full encoded command, CID octet included."""
        return 1 + self.encoded_payload_size


# ---------------------------------------------------------------------------
# PackageVersion (CID 0x00) — TS005-2.0.0 §4.1, Tables 2 and 3
# ---------------------------------------------------------------------------

@dataclass
class PackageVersionReq(MulticastSetupCommand):
    """Server → device request for the package identity (§4.1).

    No payload: the encoded command is the single octet ``0x00``.
    """

    PAYLOAD_SIZE: ClassVar[int | None] = 0

    cid: McCID = McCID.PACKAGE_VERSION

    @staticmethod
    def decode_payload(data: bytes) -> PackageVersionReq:
        if len(data) != 0:
            raise ValueError(f"PackageVersionReq takes no payload, got {len(data)} bytes")
        return PackageVersionReq()


@dataclass
class PackageVersionAns(MulticastSetupCommand):
    """Device → server answer carrying the package identity (§4.1, Table 3).

    Payload: ``PackageIdentifier`` (1) ``| PackageVersion`` (1).
    For this package ``PackageIdentifier = 2`` and ``PackageVersion = 2``.
    """

    PAYLOAD_SIZE: ClassVar[int | None] = 2

    package_identifier: int = PACKAGE_IDENTIFIER
    package_version: int = PACKAGE_VERSION
    cid: McCID = McCID.PACKAGE_VERSION

    def encode_payload(self) -> bytes:
        _check_uint("package_identifier", self.package_identifier, 8)
        _check_uint("package_version", self.package_version, 8)
        return bytes([self.package_identifier, self.package_version])

    @staticmethod
    def decode_payload(data: bytes) -> PackageVersionAns:
        if len(data) != 2:
            raise ValueError(f"PackageVersionAns needs 2 bytes, got {len(data)}")
        return PackageVersionAns(package_identifier=data[0], package_version=data[1])


# ---------------------------------------------------------------------------
# McGroupStatus (CID 0x01) — TS005-2.0.0 §4.2, Tables 4-7
# ---------------------------------------------------------------------------

@dataclass
class McGroupStatusReq(MulticastSetupCommand):
    """Server → device request for the status of some multicast groups (§4.2).

    Payload: a single ``CmdMask`` octet (Table 5)::

        bits 7:4 = RFU
        bits 3:0 = ReqGroupMask

    ``ReqGroupMask[n] = 1`` means the *n*-th multicast group SHOULD be included
    in the answer; ``0`` means it SHALL NOT be included.
    """

    PAYLOAD_SIZE: ClassVar[int | None] = 1

    req_group_mask: int = 0
    cid: McCID = McCID.MC_GROUP_STATUS

    def encode_payload(self) -> bytes:
        _check_uint("req_group_mask", self.req_group_mask, 4)
        return bytes([self.req_group_mask & 0x0F])

    @staticmethod
    def decode_payload(data: bytes) -> McGroupStatusReq:
        if len(data) != 1:
            raise ValueError(f"McGroupStatusReq needs 1 byte, got {len(data)}")
        return McGroupStatusReq(req_group_mask=data[0] & 0x0F)


@dataclass
class McGroupStatusEntry:
    """One ``[McGroupID + McAddr]`` record of ``McGroupStatusAns`` (§4.2).

    5 octets: 1 octet ``McGroupID`` followed by the 4-octet little-endian
    ``McAddr``.  The upper 6 bits of the ``McGroupID`` octet are unspecified;
    they are transmitted as 0 and ignored on reception.
    """

    group_id: int = 0
    mc_addr: int = 0

    def encode(self) -> bytes:
        _check_group_id(self.group_id)
        _check_uint("mc_addr", self.mc_addr, 32)
        return bytes([self.group_id & 0x03]) + self.mc_addr.to_bytes(4, "little")

    @staticmethod
    def decode(data: bytes) -> McGroupStatusEntry:
        if len(data) != 5:
            raise ValueError(f"McGroupStatusEntry needs 5 bytes, got {len(data)}")
        return McGroupStatusEntry(
            group_id=data[0] & 0x03,
            mc_addr=int.from_bytes(data[1:5], "little"),
        )


@dataclass
class McGroupStatusAns(MulticastSetupCommand):
    """Device → server multicast group report (§4.2, Tables 6 and 7).

    **Variable length**: ``1 + 5 * NbItems`` octets, where
    ``NbItems = popcount(AnsGroupMask)``.

    ``Status`` octet (Table 7)::

        bit  7   = RFU
        bits 6:4 = NbTotalGroups   (groups currently defined, valid range 0..4)
        bits 3:0 = AnsGroupMask    (groups actually listed below)

    If the full requested list does not fit in the available payload, the
    device SHALL discard groups starting with the highest ``McGroupID`` until
    the answer fits, so ``AnsGroupMask`` may differ from ``ReqGroupMask``.
    """

    PAYLOAD_SIZE: ClassVar[int | None] = None

    nb_total_groups: int = 0
    ans_group_mask: int = 0
    groups: list[McGroupStatusEntry] = field(default_factory=list)
    cid: McCID = McCID.MC_GROUP_STATUS

    def encode_payload(self) -> bytes:
        if not 0 <= self.nb_total_groups <= MAX_MULTICAST_GROUPS:
            raise ValueError(
                f"nb_total_groups must be 0..{MAX_MULTICAST_GROUPS} (TS005 §4.2), "
                f"got {self.nb_total_groups}"
            )
        _check_uint("ans_group_mask", self.ans_group_mask, 4)
        expected = bin(self.ans_group_mask & 0x0F).count("1")
        if len(self.groups) != expected:
            raise ValueError(
                f"McGroupStatusAns carries {len(self.groups)} records but "
                f"ans_group_mask=0x{self.ans_group_mask:X} announces {expected}"
            )
        status = ((self.nb_total_groups & 0x07) << 4) | (self.ans_group_mask & 0x0F)
        buf = bytearray([status])
        for entry in self.groups:
            buf.extend(entry.encode())
        return bytes(buf)

    @staticmethod
    def payload_size_from_status(status: int) -> int:
        """Total payload size implied by a ``Status`` octet: ``1 + 5*NbItems``."""
        return 1 + 5 * bin(status & 0x0F).count("1")

    @staticmethod
    def decode_payload(data: bytes) -> McGroupStatusAns:
        if len(data) < 1:
            raise ValueError("McGroupStatusAns needs at least 1 byte, got 0")
        status = data[0]
        expected = McGroupStatusAns.payload_size_from_status(status)
        if len(data) != expected:
            raise ValueError(
                f"McGroupStatusAns with status 0x{status:02X} needs {expected} bytes, "
                f"got {len(data)}"
            )
        groups = [
            McGroupStatusEntry.decode(data[1 + 5 * i:6 + 5 * i])
            for i in range((len(data) - 1) // 5)
        ]
        return McGroupStatusAns(
            nb_total_groups=(status >> 4) & 0x07,
            ans_group_mask=status & 0x0F,
            groups=groups,
        )


# ---------------------------------------------------------------------------
# McGroupSetup (CID 0x02) — TS005-2.0.0 §4.3, Tables 8-11
# ---------------------------------------------------------------------------

@dataclass
class McGroupSetupReq(MulticastSetupCommand):
    """Server → device creation/modification of a multicast group (§4.3, Table 8).

    Payload, **29 octets**::

        offset 0  size 1  : McGroupIDHeader (bits 7:2 RFU, bits 1:0 McGroupID)
        offset 1  size 4  : McAddr          (little endian)
        offset 5  size 16 : McKey_encrypted
        offset 21 size 4  : minMcFCnt       (little endian)
        offset 25 size 4  : maxMcFCnt       (little endian)

    ``McAddr`` has the same format as a LoRaWAN ``DevAddr``.  The device accepts
    a multicast downlink on this address only while
    ``minMcFCnt <= McFCnt <= maxMcFCnt`` (32-bit comparison).
    ``McKey_encrypted`` is ``aes128_decrypt(McKEKey, McKey)``; the device
    recovers ``McKey = aes128_encrypt(McKEKey, McKey_encrypted)``.
    """

    PAYLOAD_SIZE: ClassVar[int | None] = 29

    group_id: int = 0
    mc_addr: int = 0
    mc_key_encrypted: bytes = b"\x00" * 16
    min_fcnt: int = 0
    max_fcnt: int = 0
    cid: McCID = McCID.MC_GROUP_SETUP

    def encode_payload(self) -> bytes:
        _check_group_id(self.group_id)
        _check_uint("mc_addr", self.mc_addr, 32)
        if len(self.mc_key_encrypted) != 16:
            raise ValueError(
                f"mc_key_encrypted must be 16 bytes, got {len(self.mc_key_encrypted)}"
            )
        _check_uint("min_fcnt", self.min_fcnt, 32)
        _check_uint("max_fcnt", self.max_fcnt, 32)
        return (
            bytes([self.group_id & 0x03])
            + self.mc_addr.to_bytes(4, "little")
            + bytes(self.mc_key_encrypted)
            + self.min_fcnt.to_bytes(4, "little")
            + self.max_fcnt.to_bytes(4, "little")
        )

    @staticmethod
    def decode_payload(data: bytes) -> McGroupSetupReq:
        if len(data) != 29:
            raise ValueError(f"McGroupSetupReq needs 29 bytes, got {len(data)}")
        return McGroupSetupReq(
            group_id=data[0] & 0x03,
            mc_addr=int.from_bytes(data[1:5], "little"),
            mc_key_encrypted=bytes(data[5:21]),
            min_fcnt=int.from_bytes(data[21:25], "little"),
            max_fcnt=int.from_bytes(data[25:29], "little"),
        )


@dataclass
class McGroupSetupAns(MulticastSetupCommand):
    """Device → server acknowledgement of ``McGroupSetupReq`` (§4.3, Table 11).

    Payload: a single ``McGroupIDHeader`` octet::

        bits 7:3 = RFU
        bit  2   = IDerror
        bits 1:0 = McGroupID

    ``IDerror = 1`` means the device does not support the multicast context
    indexed by that ``McGroupID`` (e.g. a single-group device asked to create
    ``McGroupID = 1``).
    """

    PAYLOAD_SIZE: ClassVar[int | None] = 1

    group_id: int = 0
    id_error: bool = False
    cid: McCID = McCID.MC_GROUP_SETUP

    def encode_payload(self) -> bytes:
        # An ID error is precisely the case where the group id is unsupported,
        # so the 2-bit field is only range-checked, never the 0..3 group range.
        _check_uint("group_id", self.group_id, 2)
        return bytes([(int(self.id_error) << 2) | (self.group_id & 0x03)])

    @staticmethod
    def decode_payload(data: bytes) -> McGroupSetupAns:
        if len(data) != 1:
            raise ValueError(f"McGroupSetupAns needs 1 byte, got {len(data)}")
        return McGroupSetupAns(group_id=data[0] & 0x03, id_error=bool(data[0] & 0x04))


# ---------------------------------------------------------------------------
# McGroupDelete (CID 0x03) — TS005-2.0.0 §4.4, Tables 12-15
# ---------------------------------------------------------------------------

@dataclass
class McGroupDeleteReq(MulticastSetupCommand):
    """Server → device deletion of a multicast group (§4.4).

    Payload: a single octet, ``bits 7:2 = RFU``, ``bits 1:0 = McGroupID``.
    """

    PAYLOAD_SIZE: ClassVar[int | None] = 1

    group_id: int = 0
    cid: McCID = McCID.MC_GROUP_DELETE

    def encode_payload(self) -> bytes:
        _check_group_id(self.group_id)
        return bytes([self.group_id & 0x03])

    @staticmethod
    def decode_payload(data: bytes) -> McGroupDeleteReq:
        if len(data) != 1:
            raise ValueError(f"McGroupDeleteReq needs 1 byte, got {len(data)}")
        return McGroupDeleteReq(group_id=data[0] & 0x03)


@dataclass
class McGroupDeleteAns(MulticastSetupCommand):
    """Device → server acknowledgement of ``McGroupDeleteReq`` (§4.4).

    Payload: a single octet::

        bits 7:3 = RFU
        bit  2   = McGroupUndefined
        bits 1:0 = McGroupID

    ``McGroupUndefined = 1`` means no group with that ``McGroupID`` was defined
    in the device, so nothing was deleted.
    """

    PAYLOAD_SIZE: ClassVar[int | None] = 1

    group_id: int = 0
    mc_group_undefined: bool = False
    cid: McCID = McCID.MC_GROUP_DELETE

    def encode_payload(self) -> bytes:
        _check_uint("group_id", self.group_id, 2)
        return bytes([(int(self.mc_group_undefined) << 2) | (self.group_id & 0x03)])

    @staticmethod
    def decode_payload(data: bytes) -> McGroupDeleteAns:
        if len(data) != 1:
            raise ValueError(f"McGroupDeleteAns needs 1 byte, got {len(data)}")
        return McGroupDeleteAns(
            group_id=data[0] & 0x03,
            mc_group_undefined=bool(data[0] & 0x04),
        )


# ---------------------------------------------------------------------------
# Session answers (shared layout) — TS005-2.0.0 §4.5/§4.6, Tables 20 and 25
# ---------------------------------------------------------------------------

@dataclass
class _SessionAns(MulticastSetupCommand):
    """Common implementation of ``McClassC/BSessionAns`` (Tables 19/20, 24/25).

    ``Status&McGroupID`` octet::

        bits 7:6 = Reserved for future errors (SHALL be sent as 0, treated as
                   errors when set — note this is the opposite of the usual
                   "silently ignore RFU" rule)
        bit  5   = StartMissed      (SessionTime is in the past)
        bit  4   = McGroupUndefined (group never created, or deleted)
        bit  3   = FreqError        (DLFreq not usable by the device)
        bit  2   = DRError          (data rate not defined)
        bits 1:0 = McGroupID

    ``TimeToStart`` (3 octets, little endian) is present **only if bits 2
    through 7 are all 0**, i.e. only when the session was accepted, making the
    answer either 1 or 4 octets long.  It counts seconds from this uplink to
    the start of the multicast session, letting the server (which timestamps
    uplinks) verify the device's clock synchronisation.  Intervals longer than
    ``2**24 - 1`` seconds SHALL be reported as
    :data:`TIME_TO_START_UNSYNCHRONIZED` (``0xFFFFFF``), which caps scheduling
    at roughly 194 days.
    """

    PAYLOAD_SIZE: ClassVar[int | None] = None

    group_id: int = 0
    mc_group_undefined: bool = False
    freq_error: bool = False
    dr_error: bool = False
    start_missed: bool = False
    time_to_start: int | None = None
    reserved_errors: int = 0
    cid: McCID = McCID.MC_CLASS_C_SESSION

    @property
    def status_byte(self) -> int:
        """The encoded ``Status&McGroupID`` octet."""
        _check_uint("group_id", self.group_id, 2)
        _check_uint("reserved_errors", self.reserved_errors, 2)
        return (
            ((self.reserved_errors & 0x03) << 6)
            | (int(self.start_missed) << 5)
            | (int(self.mc_group_undefined) << 4)
            | (int(self.freq_error) << 3)
            | (int(self.dr_error) << 2)
            | (self.group_id & 0x03)
        )

    @property
    def has_error(self) -> bool:
        """True when any of status bits 2..7 is set, i.e. the request failed."""
        return (self.status_byte & 0xFC) != 0

    def encode_payload(self) -> bytes:
        status = self.status_byte
        if self.has_error:
            if self.time_to_start is not None:
                raise ValueError(
                    "time_to_start SHALL be omitted when any error bit (status "
                    "bits 2..7) is set (TS005 §4.5)"
                )
            return bytes([status])
        if self.time_to_start is None:
            raise ValueError(
                "time_to_start is mandatory when no error bit is set (TS005 §4.5)"
            )
        _check_uint("time_to_start", self.time_to_start, 24)
        return bytes([status]) + self.time_to_start.to_bytes(3, "little")

    @staticmethod
    def payload_size_from_status(status: int) -> int:
        """Payload size implied by a status octet: 1 on error, else 4."""
        return 1 if (status & 0xFC) else 4

    @classmethod
    def _decode(cls, data: bytes, name: str) -> "_SessionAns":
        if len(data) < 1:
            raise ValueError(f"{name} needs at least 1 byte, got 0")
        status = data[0]
        expected = _SessionAns.payload_size_from_status(status)
        if len(data) != expected:
            raise ValueError(
                f"{name} with status 0x{status:02X} needs {expected} bytes, got {len(data)}"
            )
        time_to_start = int.from_bytes(data[1:4], "little") if expected == 4 else None
        return cls(
            group_id=status & 0x03,
            dr_error=bool(status & 0x04),
            freq_error=bool(status & 0x08),
            mc_group_undefined=bool(status & 0x10),
            start_missed=bool(status & 0x20),
            reserved_errors=(status >> 6) & 0x03,
            time_to_start=time_to_start,
        )


# ---------------------------------------------------------------------------
# McClassCSession (CID 0x04) — TS005-2.0.0 §4.5, Tables 17-20
# ---------------------------------------------------------------------------

@dataclass
class McClassCSessionReq(MulticastSetupCommand):
    """Server → device scheduling of a multicast Class C session (§4.5, Table 17).

    Payload, **10 octets**::

        offset 0 size 1 : McGroupIDHeader (bits 7:2 RFU, bits 1:0 McGroupID)
        offset 1 size 4 : SessionTime     (little endian)
        offset 5 size 1 : SessionTimeOut  (bits 7:4 RFU, bits 3:0 TimeOut)
        offset 6 size 3 : DLFreq          (little endian, units of 100 Hz)
        offset 9 size 1 : DR

    ``SessionTime`` is the start of the Class C window in seconds since the GPS
    epoch (00:00:00, Sunday 6 January 1980) modulo ``2**32`` — the same format
    as the ``Time`` field of the Class B beacon.

    ``TimeOut`` is the *maximum* session length, ``2**TimeOut`` **seconds**
    (1 s .. 32768 s); the application MAY revert to Class A earlier.
    """

    PAYLOAD_SIZE: ClassVar[int | None] = 10

    group_id: int = 0
    session_time: int = 0
    session_timeout: int = 0
    dl_frequency: int = 0
    data_rate: int = 0
    cid: McCID = McCID.MC_CLASS_C_SESSION

    @property
    def timeout_seconds(self) -> int:
        """Maximum session duration in seconds: ``2**TimeOut`` (§4.5)."""
        return 1 << self.session_timeout

    def encode_payload(self) -> bytes:
        _check_group_id(self.group_id)
        _check_uint("session_time", self.session_time, 32)
        _check_uint("session_timeout", self.session_timeout, 4)
        _check_uint("data_rate", self.data_rate, 8)
        return (
            bytes([self.group_id & 0x03])
            + self.session_time.to_bytes(4, "little")
            + bytes([self.session_timeout & 0x0F])
            + _encode_dl_frequency(self.dl_frequency)
            + bytes([self.data_rate & 0xFF])
        )

    @staticmethod
    def decode_payload(data: bytes) -> McClassCSessionReq:
        if len(data) != 10:
            raise ValueError(f"McClassCSessionReq needs 10 bytes, got {len(data)}")
        return McClassCSessionReq(
            group_id=data[0] & 0x03,
            session_time=int.from_bytes(data[1:5], "little"),
            session_timeout=data[5] & 0x0F,
            dl_frequency=int.from_bytes(data[6:9], "little") * 100,
            data_rate=data[9],
        )


@dataclass
class McClassCSessionAns(_SessionAns):
    """Device → server acknowledgement of ``McClassCSessionReq`` (§4.5, Table 19).

    1 or 4 octets; see :class:`_SessionAns` for the status layout and the
    conditional ``TimeToStart`` field.
    """

    cid: McCID = McCID.MC_CLASS_C_SESSION

    @staticmethod
    def decode_payload(data: bytes) -> McClassCSessionAns:
        decoded = McClassCSessionAns._decode(data, "McClassCSessionAns")
        assert isinstance(decoded, McClassCSessionAns)
        return decoded


# ---------------------------------------------------------------------------
# McClassBSession (CID 0x05) — TS005-2.0.0 §4.6, Tables 21-25
# ---------------------------------------------------------------------------

@dataclass
class McClassBSessionReq(MulticastSetupCommand):
    """Server → device scheduling of a multicast Class B session (§4.6, Table 21).

    Payload, **10 octets**::

        offset 0 size 1 : McGroupIDHeader    (bits 7:2 RFU, bits 1:0 McGroupID)
        offset 1 size 4 : SessionTime        (little endian, multiple of 128)
        offset 5 size 1 : TimeOutPeriodicity (bit 7 RFU, bits 6:4 Periodicity,
                                              bits 3:0 TimeOut)
        offset 6 size 3 : DLFreq             (little endian, units of 100 Hz)
        offset 9 size 1 : DR

    Differences from the Class C request (§4.6):

    - ``SessionTime`` SHALL be an integer multiple of 128 (one beacon period).
      The spec does not say what a device does otherwise; this encoder does not
      enforce it, so a simulated server can deliberately send a bad value.
    - ``TimeOut`` is expressed in **BeaconPeriods of 128 s**, so the maximum
      duration is ``128 * 2**TimeOut`` seconds (``TimeOut = 8`` is ≈9.1 hours).
    - ``Periodicity`` uses the ``PingSlotInfoReq`` encoding of TS001.
    - ``DLFreq = 0`` selects the region's default Class B frequency-hopping
      beacon scheme.
    """

    PAYLOAD_SIZE: ClassVar[int | None] = 10

    group_id: int = 0
    session_time: int = 0
    periodicity: int = 0
    session_timeout: int = 0
    dl_frequency: int = 0
    data_rate: int = 0
    cid: McCID = McCID.MC_CLASS_B_SESSION

    @property
    def timeout_seconds(self) -> int:
        """Maximum session duration in seconds: ``128 * 2**TimeOut`` (§4.6)."""
        return 128 << self.session_timeout

    @property
    def ping_nb(self) -> int:
        """Ping slots opened per 128 s beacon period: ``2**(7 - Periodicity)``.

        ``PingSlotInfoReq`` encoding (TS001 Class B): ``Periodicity = 0`` gives
        128 slots per beacon period (one per second), ``Periodicity = 7`` gives
        a single slot per beacon period.
        """
        return 128 >> self.periodicity

    @property
    def ping_period_seconds(self) -> int:
        """Seconds between two ping slots: ``2**Periodicity`` (128 / ``ping_nb``)."""
        return 1 << self.periodicity

    @property
    def ping_period_slots(self) -> int:
        """Ping period in 30 ms slot units: ``4096 / ping_nb = 2**(5 + Periodicity)``."""
        return 4096 // self.ping_nb

    def encode_payload(self) -> bytes:
        _check_group_id(self.group_id)
        _check_uint("session_time", self.session_time, 32)
        _check_uint("periodicity", self.periodicity, 3)
        _check_uint("session_timeout", self.session_timeout, 4)
        _check_uint("data_rate", self.data_rate, 8)
        timeout_periodicity = ((self.periodicity & 0x07) << 4) | (self.session_timeout & 0x0F)
        return (
            bytes([self.group_id & 0x03])
            + self.session_time.to_bytes(4, "little")
            + bytes([timeout_periodicity])
            + _encode_dl_frequency(self.dl_frequency)
            + bytes([self.data_rate & 0xFF])
        )

    @staticmethod
    def decode_payload(data: bytes) -> McClassBSessionReq:
        if len(data) != 10:
            raise ValueError(f"McClassBSessionReq needs 10 bytes, got {len(data)}")
        return McClassBSessionReq(
            group_id=data[0] & 0x03,
            session_time=int.from_bytes(data[1:5], "little"),
            periodicity=(data[5] >> 4) & 0x07,
            session_timeout=data[5] & 0x0F,
            dl_frequency=int.from_bytes(data[6:9], "little") * 100,
            data_rate=data[9],
        )


@dataclass
class McClassBSessionAns(_SessionAns):
    """Device → server acknowledgement of ``McClassBSessionReq`` (§4.6, Table 24).

    Structurally and semantically identical to :class:`McClassCSessionAns`.
    """

    cid: McCID = McCID.MC_CLASS_B_SESSION

    @staticmethod
    def decode_payload(data: bytes) -> McClassBSessionAns:
        decoded = McClassBSessionAns._decode(data, "McClassBSessionAns")
        assert isinstance(decoded, McClassBSessionAns)
        return decoded


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------

MulticastSetupCommandType = (
    PackageVersionReq | PackageVersionAns |
    McGroupStatusReq | McGroupStatusAns |
    McGroupSetupReq | McGroupSetupAns |
    McGroupDeleteReq | McGroupDeleteAns |
    McClassCSessionReq | McClassCSessionAns |
    McClassBSessionReq | McClassBSessionAns
)


#: Fixed downlink (server → device) payload sizes, in octets (§3, Table 2).
_DOWNLINK_PAYLOAD_SIZES: dict[McCID, int] = {
    McCID.PACKAGE_VERSION: 0,
    McCID.MC_GROUP_STATUS: 1,
    McCID.MC_GROUP_SETUP: 29,
    McCID.MC_GROUP_DELETE: 1,
    McCID.MC_CLASS_C_SESSION: 10,
    McCID.MC_CLASS_B_SESSION: 10,
}

#: Fixed uplink (device → server) payload sizes.  ``McGroupStatusAns`` and the
#: two session answers are variable length and therefore absent from this map.
_UPLINK_FIXED_PAYLOAD_SIZES: dict[McCID, int] = {
    McCID.PACKAGE_VERSION: 2,
    McCID.MC_GROUP_SETUP: 1,
    McCID.MC_GROUP_DELETE: 1,
}


def parse_downlink_commands(data: bytes) -> list[MulticastSetupCommandType]:
    """Parse a server → device FRMPayload received on the package FPort (§3).

    Several commands may be concatenated as ``CID | payload | …``; they are
    returned in wire order.  Parsing stops (with a warning) on an unknown CID
    or a truncated payload, since the remaining octets can no longer be framed.
    """
    commands: list[MulticastSetupCommandType] = []
    pos = 0

    while pos < len(data):
        cid_byte = data[pos]
        try:
            cid = McCID(cid_byte)
        except ValueError:
            logger.warning(
                f"TS005 downlink: unknown command id 0x{cid_byte:02X} at offset {pos}, "
                f"dropping the remaining {len(data) - pos} byte(s)"
            )
            break

        size = _DOWNLINK_PAYLOAD_SIZES[cid]
        payload = data[pos + 1:pos + 1 + size]
        if len(payload) < size:
            logger.warning(
                f"TS005 downlink: truncated {cid.name} at offset {pos} "
                f"({len(payload)}/{size} payload bytes), dropping the rest"
            )
            break

        match cid:
            case McCID.PACKAGE_VERSION:
                commands.append(PackageVersionReq())
            case McCID.MC_GROUP_STATUS:
                commands.append(McGroupStatusReq.decode_payload(payload))
            case McCID.MC_GROUP_SETUP:
                commands.append(McGroupSetupReq.decode_payload(payload))
            case McCID.MC_GROUP_DELETE:
                commands.append(McGroupDeleteReq.decode_payload(payload))
            case McCID.MC_CLASS_C_SESSION:
                commands.append(McClassCSessionReq.decode_payload(payload))
            case McCID.MC_CLASS_B_SESSION:
                commands.append(McClassBSessionReq.decode_payload(payload))

        pos += 1 + size

    return commands


def parse_uplink_commands(data: bytes) -> list[MulticastSetupCommandType]:
    """Parse a device → server FRMPayload received on the package FPort (§3).

    Three answers are variable length and their size is derived from the first
    payload octet:

    - ``McGroupStatusAns``: ``1 + 5 * popcount(AnsGroupMask)`` (§4.2);
    - ``McClassCSessionAns`` / ``McClassBSessionAns``: 4 octets when status bits
      2..7 are all 0, otherwise 1 octet (§4.5, §4.6).

    Parsing stops (with a warning) on an unknown CID or a truncated payload.
    """
    commands: list[MulticastSetupCommandType] = []
    pos = 0

    while pos < len(data):
        cid_byte = data[pos]
        try:
            cid = McCID(cid_byte)
        except ValueError:
            logger.warning(
                f"TS005 uplink: unknown command id 0x{cid_byte:02X} at offset {pos}, "
                f"dropping the remaining {len(data) - pos} byte(s)"
            )
            break

        rest = data[pos + 1:]
        size = _UPLINK_FIXED_PAYLOAD_SIZES.get(cid)
        if size is None:
            # Variable length: the status octet determines the payload size.
            if len(rest) < 1:
                logger.warning(
                    f"TS005 uplink: truncated {cid.name} at offset {pos} "
                    f"(no status byte), dropping the rest"
                )
                break
            if cid is McCID.MC_GROUP_STATUS:
                size = McGroupStatusAns.payload_size_from_status(rest[0])
            else:
                size = _SessionAns.payload_size_from_status(rest[0])

        payload = rest[:size]
        if len(payload) < size:
            logger.warning(
                f"TS005 uplink: truncated {cid.name} at offset {pos} "
                f"({len(payload)}/{size} payload bytes), dropping the rest"
            )
            break

        match cid:
            case McCID.PACKAGE_VERSION:
                commands.append(PackageVersionAns.decode_payload(payload))
            case McCID.MC_GROUP_STATUS:
                commands.append(McGroupStatusAns.decode_payload(payload))
            case McCID.MC_GROUP_SETUP:
                commands.append(McGroupSetupAns.decode_payload(payload))
            case McCID.MC_GROUP_DELETE:
                commands.append(McGroupDeleteAns.decode_payload(payload))
            case McCID.MC_CLASS_C_SESSION:
                commands.append(McClassCSessionAns.decode_payload(payload))
            case McCID.MC_CLASS_B_SESSION:
                commands.append(McClassBSessionAns.decode_payload(payload))

        pos += 1 + size

    return commands


def encode_commands(commands: list[MulticastSetupCommand]) -> bytes:
    """Concatenate TS005 commands into a single FRMPayload (§3).

    Commands are executed by the receiver first to last, so the caller controls
    ordering.  No payload-size check against the region's ``MaxAppPl`` is done
    here; that belongs to the sender.
    """
    buf = bytearray()
    for cmd in commands:
        buf.extend(cmd.encode())
    return bytes(buf)
