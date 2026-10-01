"""Tests for the TS004-2.0.0 Fragmented Data Block Transport wire codecs.

Hex vectors are hand-written from the normative tables of
*LoRaWAN Fragmented Data Block Transport* TS004-2.0.0 (April 2022) §3,
Tables 1-24, and checked bit position by bit position.
"""

import logging
import random

import pytest

from simulator.environment import simulation_env as sim
from simulator.lorawan.crypto import compute_data_mic, encrypt_frm_payload
from simulator.lorawan.enums.frame_types import MType
from simulator.lorawan.frame import FCtrl, FHDR, MACPayload, MHDR, PHYPayload
from simulator.lorawan.network_server import NetworkServer
from simulator.lorawan.region import max_frm_payload
from simulator.lorawan.fuota.crypto import (
    compute_data_block_mic,
    derive_data_block_int_key,
)
from simulator.lorawan.fuota.fragmentation import (
    MAX_NB_FRAG,
    FragmentationDecoder,
    FragmentationEncoder,
)
from simulator.lorawan.fuota.frag_transport import (
    DATA_FRAGMENT_HEADER_SIZE,
    FragmentationDeviceApplication,
    FragmentationServerApplication,
    FragServerSession,
    FragSessionState,
    FRAGMENTATION_FPORT,
    MAX_FRAG_SESSIONS,
    MAX_MISSING_FRAG,
    MAX_NB_FRAG_RECEIVED,
    PACKAGE_IDENTIFIER,
    PACKAGE_VERSION,
    DataFragment,
    FragCID,
    FragDataBlockReceivedAns,
    FragDataBlockReceivedReq,
    FragSessionDeleteAns,
    FragSessionDeleteReq,
    FragSessionSetupAns,
    FragSessionSetupReq,
    FragSessionStatusAns,
    FragSessionStatusReq,
    PackageVersionAns,
    PackageVersionReq,
    block_ack_delay_seconds,
    encode_commands,
    max_fragment_payload,
    parse_downlink_commands,
    parse_uplink_commands,
)


# ---------------------------------------------------------------------------
# Constants and CIDs (§2.1, §3 Table 1)
# ---------------------------------------------------------------------------


def test_package_constants():
    assert FRAGMENTATION_FPORT == 201
    assert PACKAGE_IDENTIFIER == 3
    assert PACKAGE_VERSION == 2
    assert MAX_FRAG_SESSIONS == 4
    assert DATA_FRAGMENT_HEADER_SIZE == 3
    assert MAX_NB_FRAG_RECEIVED == 16383
    assert MAX_MISSING_FRAG == 255


def test_frag_cid_values():
    assert FragCID.PACKAGE_VERSION == 0x00
    assert FragCID.FRAG_SESSION_STATUS == 0x01
    assert FragCID.FRAG_SESSION_SETUP == 0x02
    assert FragCID.FRAG_SESSION_DELETE == 0x03
    assert FragCID.FRAG_DATA_BLOCK_RECEIVED == 0x04
    assert FragCID.DATA_FRAGMENT == 0x08


# ---------------------------------------------------------------------------
# PackageVersion (CID 0x00) — §3.1, Tables 2 and 3
# ---------------------------------------------------------------------------


def test_package_version_req_is_cid_only():
    assert PackageVersionReq().encode() == bytes.fromhex("00")


def test_package_version_ans_encode():
    # PackageIdentifier = 3, PackageVersion = 2 for TS004-2.0.0.
    assert PackageVersionAns().encode() == bytes.fromhex("000302")


def test_package_version_ans_round_trip():
    ans = PackageVersionAns(package_identifier=3, package_version=2)
    assert PackageVersionAns.decode_payload(ans.encode_payload()) == ans


def test_package_version_bad_lengths():
    with pytest.raises(ValueError):
        PackageVersionReq.decode_payload(b"\x00")
    with pytest.raises(ValueError):
        PackageVersionAns.decode_payload(b"\x03")


# ---------------------------------------------------------------------------
# FragSessionStatusReq (CID 0x01) — §3.2, Tables 4 and 5
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "frag_index,participants,expected",
    [
        (0, False, 0x00),
        (0, True, 0x01),
        (1, False, 0x02),  # FragIndex in bits 2:1
        (2, True, 0x05),
        (3, False, 0x06),
        (3, True, 0x07),
    ],
)
def test_frag_session_status_req_bits(frag_index, participants, expected):
    req = FragSessionStatusReq(frag_index=frag_index, participants=participants)
    assert req.encode() == bytes([FragCID.FRAG_SESSION_STATUS, expected])
    assert FragSessionStatusReq.decode_payload(bytes([expected])) == req


def test_frag_session_status_req_ignores_rfu_bits():
    # Bits 7:3 are RFU and SHALL be silently ignored by the receiver (§1.3).
    decoded = FragSessionStatusReq.decode_payload(bytes([0xF8 | 0x02]))
    assert decoded == FragSessionStatusReq(frag_index=1, participants=False)


def test_frag_session_status_req_rejects_bad_index():
    with pytest.raises(ValueError):
        FragSessionStatusReq(frag_index=4).encode()


# ---------------------------------------------------------------------------
# FragSessionStatusAns (CID 0x01) — §3.2, Tables 6, 7 and 8
# ---------------------------------------------------------------------------


def test_frag_session_status_ans_four_octet_form():
    # FragIndex = 1, NbFragReceived = 100, MissingFrag = 3, no error bits.
    # Received&index = (1 << 14) | 100 = 0x4064 -> octets 64 40 (little endian).
    ans = FragSessionStatusAns(frag_index=1, nb_frag_received=100, missing_frag=3)
    assert ans.encode() == bytes.fromhex("01" "00" "6440" "03")
    assert FragSessionStatusAns.decode_payload(ans.encode_payload()) == ans


def test_frag_session_status_ans_status_bit_positions():
    # Table 7: bit 2 = Session does not exist, bit 1 = MICError, bit 0 = MemoryError.
    assert FragSessionStatusAns(memory_error=True).status_byte == 0x01
    assert FragSessionStatusAns(mic_error=True).status_byte == 0x02
    assert FragSessionStatusAns(session_does_not_exist=True).status_byte == 0x04


def test_frag_session_status_ans_one_octet_form():
    # §3.2: when "Session does not exist" is set, Received&index and MissingFrag
    # SHALL NOT be included -> a 1-octet payload.
    ans = FragSessionStatusAns(frag_index=2, session_does_not_exist=True)
    assert ans.encode() == bytes.fromhex("0104")
    decoded = FragSessionStatusAns.decode_payload(b"\x04")
    assert decoded.session_does_not_exist is True
    assert FragSessionStatusAns.payload_size(0x04) == 1
    assert FragSessionStatusAns.payload_size(0x03) == 4


def test_frag_session_status_ans_mic_error_round_trip():
    ans = FragSessionStatusAns(
        frag_index=3, nb_frag_received=16383, missing_frag=0, mic_error=True
    )
    # Received&index = (3 << 14) | 16383 = 0xFFFF.
    assert ans.encode_payload() == bytes.fromhex("02" "ffff" "00")
    assert FragSessionStatusAns.decode_payload(ans.encode_payload()) == ans


def test_frag_session_status_ans_saturates():
    # NbFragReceived is 14 bits, MissingFrag SHALL saturate at 255 (§3.2).
    ans = FragSessionStatusAns(nb_frag_received=99999, missing_frag=4000)
    payload = ans.encode_payload()
    decoded = FragSessionStatusAns.decode_payload(payload)
    assert decoded.nb_frag_received == MAX_NB_FRAG_RECEIVED
    assert decoded.missing_frag == MAX_MISSING_FRAG


def test_frag_session_status_ans_rfu_ignored():
    # Bits 7:3 of Status are RFU.
    decoded = FragSessionStatusAns.decode_payload(bytes([0xF8, 0x05, 0x00, 0x07]))
    assert decoded.memory_error is False
    assert decoded.mic_error is False
    assert decoded.session_does_not_exist is False
    assert decoded.nb_frag_received == 5
    assert decoded.missing_frag == 7


def test_frag_session_status_ans_bad_length():
    with pytest.raises(ValueError):
        FragSessionStatusAns.decode_payload(b"")
    with pytest.raises(ValueError):
        FragSessionStatusAns.decode_payload(b"\x00\x01")  # needs 4
    with pytest.raises(ValueError):
        FragSessionStatusAns.decode_payload(b"\x04\x00")  # needs 1


# ---------------------------------------------------------------------------
# FragSessionSetupReq (CID 0x02) — §3.3, Tables 9, 10, 11
# ---------------------------------------------------------------------------


def _setup_req() -> FragSessionSetupReq:
    """A fully populated 16-octet setup request with distinctive field values."""
    return FragSessionSetupReq(
        frag_index=2,
        mc_group_bit_mask=0b0101,
        nb_frag=100,
        frag_size=20,
        ack_reception=True,
        frag_algo=0,
        block_ack_delay=5,
        padding=7,
        descriptor=0x44332211,
        session_cnt=0x0102,
        mic=bytes.fromhex("DEADBEEF"),
    )


def test_frag_session_setup_req_byte_exact():
    req = _setup_req()
    # FragSession: bits 5:4 FragIndex=2, bits 3:0 McGroupBitMask=0b0101 -> 0x25.
    # Control:     bit 6 AckReception=1, bits 5:3 FragAlgo=0, bits 2:0 delay=5 -> 0x45.
    expected = bytes.fromhex(
        "25"        # FragSession
        "6400"      # NbFrag = 100, little endian
        "14"        # FragSize = 20
        "45"        # Control
        "07"        # Padding
        "11223344"  # Descriptor = 0x44332211, little endian
        "0201"      # SessionCnt = 0x0102, little endian
        "deadbeef"  # MIC, as transmitted
    )
    assert len(expected) == FragSessionSetupReq.PAYLOAD_SIZE == 16
    assert req.encode_payload() == expected
    assert req.encode() == bytes([FragCID.FRAG_SESSION_SETUP]) + expected


def test_frag_session_setup_req_round_trip():
    req = _setup_req()
    assert FragSessionSetupReq.decode_payload(req.encode_payload()) == req


def test_frag_session_setup_req_frag_session_byte_positions():
    assert FragSessionSetupReq(frag_index=3, mc_group_bit_mask=0).frag_session_byte == 0x30
    assert FragSessionSetupReq(frag_index=0, mc_group_bit_mask=0xF).frag_session_byte == 0x0F
    assert FragSessionSetupReq(frag_index=1, mc_group_bit_mask=0b1000).frag_session_byte == 0x18


@pytest.mark.parametrize(
    "ack,algo,delay,expected",
    [
        (False, 0, 0, 0x00),
        (True, 0, 0, 0x40),   # AckReception is bit 6
        (False, 7, 0, 0x38),  # FragAlgo is bits 5:3
        (False, 0, 7, 0x07),  # BlockAckDelay is bits 2:0
        (False, 1, 0, 0x08),
        (True, 7, 7, 0x7F),
    ],
)
def test_frag_session_setup_req_control_byte_positions(ack, algo, delay, expected):
    req = FragSessionSetupReq(ack_reception=ack, frag_algo=algo, block_ack_delay=delay)
    assert req.control_byte == expected
    decoded = FragSessionSetupReq.decode_payload(req.encode_payload())
    assert decoded.ack_reception is ack
    assert decoded.frag_algo == algo
    assert decoded.block_ack_delay == delay


def test_frag_session_setup_req_rfu_bits_ignored():
    # FragSession bits 7:6 and Control bit 7 are RFU.
    raw = bytearray(_setup_req().encode_payload())
    raw[0] |= 0xC0
    raw[4] |= 0x80
    assert FragSessionSetupReq.decode_payload(bytes(raw)) == _setup_req()


def test_frag_session_setup_req_block_size():
    req = FragSessionSetupReq(nb_frag=100, frag_size=20, padding=7)
    assert req.block_size == 100 * 20 - 7


@pytest.mark.parametrize(
    "kwargs",
    [
        {"frag_index": 4},
        {"mc_group_bit_mask": 16},
        {"nb_frag": 0},
        {"nb_frag": MAX_NB_FRAG + 1},
        {"frag_size": 256},
        {"frag_algo": 8},
        {"block_ack_delay": 8},
        {"padding": 256},
        {"descriptor": 1 << 32},
        {"session_cnt": 1 << 16},
        {"mic": b"\x00\x00\x00"},
    ],
)
def test_frag_session_setup_req_validation(kwargs):
    with pytest.raises(ValueError):
        FragSessionSetupReq(**kwargs).encode()


def test_frag_session_setup_req_bad_length():
    with pytest.raises(ValueError):
        FragSessionSetupReq.decode_payload(bytes(15))


# -- MIC helpers (§3.3, Table 12) -------------------------------------------


def test_setup_req_mic_matches_crypto_module():
    key = derive_data_block_int_key(key=bytes(range(16)))
    block = bytes(range(256)) * 3
    req = _setup_req()

    expected = compute_data_block_mic(
        data_block_int_key=key,
        data_block=block,
        session_cnt=req.session_cnt,
        frag_index=req.frag_index,
        descriptor=req.descriptor,
    )
    assert req.compute_mic(key, block) == expected
    assert len(expected) == 4


def test_setup_req_with_mic_and_verify():
    key = derive_data_block_int_key(key=bytes(16))
    block = b"firmware image v1.2.3" * 10
    req = _setup_req().with_mic(key, block)

    assert req.mic == req.compute_mic(key, block)
    assert req.verify_mic(key, block) is True
    assert req.verify_mic(key, block + b"!") is False
    # with_mic leaves every other field untouched.
    assert req.frag_index == 2 and req.session_cnt == 0x0102


def test_setup_req_mic_survives_the_wire():
    key = derive_data_block_int_key(key=bytes(range(16)))
    block = bytes(range(100))
    req = _setup_req().with_mic(key, block)
    decoded = FragSessionSetupReq.decode_payload(req.encode_payload())
    assert decoded.verify_mic(key, block) is True


def test_setup_req_mic_binds_session_cnt_and_frag_index():
    key = derive_data_block_int_key(key=bytes(16))
    block = bytes(64)
    base = _setup_req()
    mic = base.compute_mic(key, block)
    assert FragSessionSetupReq(
        **{**base.__dict__, "session_cnt": base.session_cnt + 1}
    ).compute_mic(key, block) != mic
    assert FragSessionSetupReq(
        **{**base.__dict__, "frag_index": 1}
    ).compute_mic(key, block) != mic
    assert FragSessionSetupReq(
        **{**base.__dict__, "descriptor": 0}
    ).compute_mic(key, block) != mic


# ---------------------------------------------------------------------------
# FragSessionSetupAns (CID 0x02) — §3.3, Tables 13 and 14
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs,expected",
    [
        ({}, 0x00),
        ({"frag_algo_unsupported": True}, 0x01),
        ({"not_enough_memory": True}, 0x02),
        ({"frag_index_unsupported": True}, 0x04),
        ({"wrong_descriptor": True}, 0x08),
        ({"session_cnt_replay": True}, 0x10),
        ({"frag_index": 1}, 0x40),  # FragIndex echo sits in bits 7:6
        ({"frag_index": 3}, 0xC0),
    ],
)
def test_frag_session_setup_ans_bits(kwargs, expected):
    ans = FragSessionSetupAns(**kwargs)
    assert ans.encode() == bytes([FragCID.FRAG_SESSION_SETUP, expected])
    assert FragSessionSetupAns.decode_payload(bytes([expected])) == ans


def test_frag_session_setup_ans_accepted():
    assert FragSessionSetupAns(frag_index=2).accepted is True
    assert FragSessionSetupAns(frag_index=2, wrong_descriptor=True).accepted is False
    assert FragSessionSetupAns(session_cnt_replay=True).accepted is False


def test_frag_session_setup_ans_rfu_bit5_ignored():
    assert FragSessionSetupAns.decode_payload(b"\x20") == FragSessionSetupAns()


def test_frag_session_setup_ans_all_errors():
    ans = FragSessionSetupAns(
        frag_index=3,
        frag_algo_unsupported=True,
        not_enough_memory=True,
        frag_index_unsupported=True,
        wrong_descriptor=True,
        session_cnt_replay=True,
    )
    assert ans.encode_payload() == bytes([0xC0 | FragSessionSetupAns.ERROR_MASK])
    assert ans.accepted is False


# ---------------------------------------------------------------------------
# FragSessionDelete (CID 0x03) — §3.4, Tables 15-18
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("frag_index", range(MAX_FRAG_SESSIONS))
def test_frag_session_delete_req(frag_index):
    req = FragSessionDeleteReq(frag_index=frag_index)
    assert req.encode() == bytes([FragCID.FRAG_SESSION_DELETE, frag_index])
    assert FragSessionDeleteReq.decode_payload(bytes([frag_index])) == req
    # Bits 7:2 are RFU.
    assert FragSessionDeleteReq.decode_payload(bytes([0xFC | frag_index])) == req


def test_frag_session_delete_ans():
    ok = FragSessionDeleteAns(frag_index=2)
    assert ok.encode() == bytes.fromhex("0302")
    assert ok.accepted is True

    nope = FragSessionDeleteAns(frag_index=1, session_does_not_exist=True)
    assert nope.encode() == bytes.fromhex("0305")  # bit 2 set
    assert nope.accepted is False
    assert FragSessionDeleteAns.decode_payload(b"\x05") == nope


def test_frag_session_delete_bad_lengths():
    with pytest.raises(ValueError):
        FragSessionDeleteReq.decode_payload(b"")
    with pytest.raises(ValueError):
        FragSessionDeleteAns.decode_payload(b"\x00\x00")


# ---------------------------------------------------------------------------
# FragDataBlockReceived (CID 0x04) — §3.5, Tables 19-22
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "frag_index,mic_error,expected",
    [
        (0, False, 0x00),
        (1, False, 0x01),
        (3, False, 0x03),
        (0, True, 0x04),  # MICError is bit 2
        (2, True, 0x06),
    ],
)
def test_frag_data_block_received_req(frag_index, mic_error, expected):
    req = FragDataBlockReceivedReq(frag_index=frag_index, mic_error=mic_error)
    assert req.encode() == bytes([FragCID.FRAG_DATA_BLOCK_RECEIVED, expected])
    assert FragDataBlockReceivedReq.decode_payload(bytes([expected])) == req
    # Bits 7:3 RFU.
    assert FragDataBlockReceivedReq.decode_payload(bytes([0xF8 | expected])) == req


def test_frag_data_block_received_ans():
    ans = FragDataBlockReceivedAns(frag_index=3)
    assert ans.encode() == bytes.fromhex("0403")
    assert FragDataBlockReceivedAns.decode_payload(b"\xfb") == ans  # RFU bits 7:2


def test_frag_data_block_received_bad_lengths():
    with pytest.raises(ValueError):
        FragDataBlockReceivedReq.decode_payload(b"")
    with pytest.raises(ValueError):
        FragDataBlockReceivedAns.decode_payload(b"\x00\x00")


# ---------------------------------------------------------------------------
# DataFragment (CID 0x08) — §3.6, Tables 23 and 24
# ---------------------------------------------------------------------------


def test_data_fragment_worked_example():
    """TS004 §3.6 worked example: FragIndex = 1, N = 5.

    Index&N = (1 << 14) | 5 = 0x4005 -> octets 05 40 (little endian).
    """
    frag = DataFragment(frag_index=1, index_n=5, payload=bytes.fromhex("AABBCC"))
    assert frag.index_and_n == 0x4005
    assert frag.encode_payload() == bytes.fromhex("0540" "aabbcc")
    assert frag.encode() == bytes.fromhex("08" "0540" "aabbcc")
    assert DataFragment.decode_payload(frag.encode_payload()) == frag


@pytest.mark.parametrize(
    "frag_index,index_n,word",
    [
        (0, 1, 0x0001),
        (0, MAX_NB_FRAG, 0x3FFF),
        (1, 1, 0x4001),
        (2, 16383, 0xBFFF),
        (3, 16383, 0xFFFF),
    ],
)
def test_data_fragment_index_and_n_range(frag_index, index_n, word):
    frag = DataFragment(frag_index=frag_index, index_n=index_n, payload=b"\x01")
    assert frag.index_and_n == word
    assert frag.encode_payload()[:2] == word.to_bytes(2, "little")
    assert DataFragment.decode_payload(frag.encode_payload()) == frag


@pytest.mark.parametrize("index_n", [0, -1, MAX_NB_FRAG + 1, 1 << 14])
def test_data_fragment_rejects_out_of_range_n(index_n):
    # N is 14 bits and 1-based in v2.0.0 (it was 0-based in v1.0.0).
    with pytest.raises(ValueError):
        DataFragment(frag_index=0, index_n=index_n, payload=b"").encode()


def test_data_fragment_rejects_zero_n_on_decode():
    with pytest.raises(ValueError):
        DataFragment.decode_payload(bytes.fromhex("0000" "aa"))


def test_data_fragment_empty_payload_allowed():
    frag = DataFragment(frag_index=0, index_n=1, payload=b"")
    assert frag.encode() == bytes.fromhex("08" "0100")
    assert DataFragment.decode_payload(b"\x01\x00").payload == b""


def test_data_fragment_truncated_header():
    with pytest.raises(ValueError):
        DataFragment.decode_payload(b"\x01")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def test_max_fragment_payload():
    # 1 CID octet + 2 octets of Index&N (§3.6).
    assert max_fragment_payload(51) == 48
    assert max_fragment_payload(242) == 239
    assert max_fragment_payload(3) == 0
    assert max_fragment_payload(0) == 0


@pytest.mark.parametrize("delay", range(8))
def test_block_ack_delay_seconds_bounds(delay):
    """delay = rand() * 2**(BlockAckDelay + 4), rand() uniform in [0, 1] (§3.3)."""
    rng = random.Random(1234 + delay)
    window = float(1 << (delay + 4))
    samples = [block_ack_delay_seconds(delay, rng) for _ in range(2000)]

    assert all(0.0 <= s < window for s in samples)
    # Uniform on [0, window): mean ~ window/2, and both halves get hit.
    assert abs(sum(samples) / len(samples) - window / 2) < window * 0.05
    assert min(samples) < window * 0.02
    assert max(samples) > window * 0.98


def test_block_ack_delay_seconds_window_sizes():
    """BlockAckDelay 0..7 spans 16 s .. 2048 s."""
    rng = random.Random(7)
    assert block_ack_delay_seconds(0, rng) < 16.0
    rng = random.Random(7)
    assert block_ack_delay_seconds(7, rng) < 2048.0
    # Same seed, same draw, scaled by the window.
    a = block_ack_delay_seconds(0, random.Random(99))
    b = block_ack_delay_seconds(3, random.Random(99))
    assert b == pytest.approx(a * 8.0)


def test_block_ack_delay_seconds_default_rng_and_validation():
    assert 0.0 <= block_ack_delay_seconds(0) < 16.0
    with pytest.raises(ValueError):
        block_ack_delay_seconds(8)
    with pytest.raises(ValueError):
        block_ack_delay_seconds(-1)


# ---------------------------------------------------------------------------
# Multi-command parsing (§3)
# ---------------------------------------------------------------------------


def test_encode_commands_concatenates():
    cmds = [PackageVersionReq(), FragSessionDeleteReq(frag_index=2)]
    assert encode_commands(cmds) == bytes.fromhex("00" "0302")


def test_parse_downlink_multiple_commands():
    cmds = [
        PackageVersionReq(),
        FragSessionStatusReq(frag_index=1, participants=True),
        _setup_req(),
        FragSessionDeleteReq(frag_index=3),
        FragDataBlockReceivedAns(frag_index=1),
    ]
    assert parse_downlink_commands(encode_commands(cmds)) == cmds


def test_parse_uplink_multiple_commands():
    cmds = [
        PackageVersionAns(),
        FragSessionSetupAns(frag_index=2, not_enough_memory=True),
        FragSessionStatusAns(frag_index=1, nb_frag_received=50, missing_frag=2),
        FragSessionStatusAns(frag_index=0, session_does_not_exist=True),
        FragSessionDeleteAns(frag_index=1),
        FragDataBlockReceivedReq(frag_index=2, mic_error=True),
    ]
    assert parse_uplink_commands(encode_commands(cmds)) == cmds


def test_parse_uplink_handles_variable_length_status_ans():
    """A 1-octet status answer must not swallow the command that follows."""
    # FragIndex lives only inside Received&index, which the 1-octet form omits,
    # so it cannot round-trip and decodes back as 0 (documented spec gap).
    short = FragSessionStatusAns(frag_index=0, session_does_not_exist=True)
    after = FragSessionDeleteAns(frag_index=1)
    parsed = parse_uplink_commands(encode_commands([short, after]))
    assert parsed == [short, after]

    long = FragSessionStatusAns(frag_index=1, nb_frag_received=7, missing_frag=1)
    parsed = parse_uplink_commands(encode_commands([long, after]))
    assert parsed == [long, after]


def test_parse_downlink_data_fragment_consumes_rest():
    frag = DataFragment(frag_index=2, index_n=9, payload=bytes(range(20)))
    parsed = parse_downlink_commands(frag.encode())
    assert parsed == [frag]


def test_parse_downlink_data_fragment_terminates_parsing():
    # §3.6: DataFragment SHALL be the only command in its message payload, so
    # anything appended is treated as part of the fragment, not a new command.
    payload = DataFragment(0, 1, b"\xaa\xbb").encode() + PackageVersionReq().encode()
    parsed = parse_downlink_commands(payload)
    assert len(parsed) == 1
    assert isinstance(parsed[0], DataFragment)
    assert parsed[0].payload == b"\xaa\xbb\x00"


def test_parse_unknown_cid_stops_with_warning(caplog):
    payload = PackageVersionReq().encode() + b"\x7f\xff\xff"
    with caplog.at_level(logging.WARNING):
        parsed = parse_downlink_commands(payload)
    assert parsed == [PackageVersionReq()]
    assert any("0x7F" in r.getMessage() for r in caplog.records)


def test_parse_uplink_unknown_cid(caplog):
    with caplog.at_level(logging.WARNING):
        parsed = parse_uplink_commands(b"\x05\x00")
    assert parsed == []
    assert caplog.records


def test_parse_uplink_rejects_data_fragment(caplog):
    # DataFragment is downlink-only (§3.6).
    with caplog.at_level(logging.WARNING):
        parsed = parse_uplink_commands(DataFragment(0, 1, b"\x01").encode())
    assert parsed == []
    assert any("downlink-only" in r.message for r in caplog.records)


def test_parse_truncated_command_stops(caplog):
    payload = PackageVersionReq().encode() + bytes([FragCID.FRAG_SESSION_SETUP]) + bytes(5)
    with caplog.at_level(logging.WARNING):
        parsed = parse_downlink_commands(payload)
    assert parsed == [PackageVersionReq()]
    assert any("truncated" in r.message for r in caplog.records)


def test_parse_truncated_status_ans_stops(caplog):
    payload = bytes([FragCID.FRAG_SESSION_STATUS])  # no Status octet at all
    with caplog.at_level(logging.WARNING):
        assert parse_uplink_commands(payload) == []
    assert caplog.records

    payload = bytes([FragCID.FRAG_SESSION_STATUS, 0x00, 0x01])  # needs 4, has 3
    with caplog.at_level(logging.WARNING):
        assert parse_uplink_commands(payload) == []


def test_parse_empty_payload():
    assert parse_downlink_commands(b"") == []
    assert parse_uplink_commands(b"") == []


def test_parse_malformed_data_fragment_logs(caplog):
    with caplog.at_level(logging.WARNING):
        assert parse_downlink_commands(bytes([FragCID.DATA_FRAGMENT, 0x00])) == []
    assert any("DataFragment" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Integration: encoder -> wire -> decoder (§3.6 + Annex A)
# ---------------------------------------------------------------------------


def test_end_to_end_block_transfer_with_losses():
    """Fragment a block, ship it as DataFragments, drop some, reassemble."""
    rng = random.Random(20240401)
    block = bytes(rng.randrange(256) for _ in range(2000))
    frag_size = 20
    frag_index = 1

    encoder = FragmentationEncoder(block, frag_size)
    assert encoder.nb_frag == 100
    assert encoder.padding == 0

    setup = FragSessionSetupReq(
        frag_index=frag_index,
        mc_group_bit_mask=0b0001,
        nb_frag=encoder.nb_frag,
        frag_size=frag_size,
        ack_reception=True,
        frag_algo=0,
        block_ack_delay=2,
        padding=encoder.padding,
        descriptor=0xCAFEBABE,
        session_cnt=1,
    )
    key = derive_data_block_int_key(key=bytes(range(16)))
    setup = setup.with_mic(key, block)

    # The setup command survives the wire.
    setup_cmds = parse_downlink_commands(setup.encode())
    assert setup_cmds == [setup]
    device_setup = setup_cmds[0]

    decoder = FragmentationDecoder(device_setup.nb_frag, device_setup.frag_size)

    # Spec example of §A.1: CR = 1/2, i.e. 2M coded fragments transmitted,
    # tolerating ~50% loss. Here we drop 30% of frames at random.
    sent = 0
    for n in range(1, 2 * encoder.nb_frag + 1):
        wire = DataFragment(
            frag_index=frag_index, index_n=n, payload=encoder.fragment(n)
        ).encode()
        if rng.random() < 0.30:
            continue  # lost on air
        sent += 1
        parsed = parse_downlink_commands(wire)
        assert len(parsed) == 1
        frag = parsed[0]
        assert isinstance(frag, DataFragment)
        assert frag.frag_index == frag_index
        assert len(frag.payload) == device_setup.frag_size
        if decoder.receive(frag.index_n, frag.payload):
            break

    assert decoder.is_complete, f"not complete after {sent} received fragments"
    recovered = decoder.reconstruct(device_setup.padding)
    assert recovered == block

    # The device now verifies the MIC it was given at setup time.
    assert device_setup.verify_mic(key, recovered) is True

    # ... and acknowledges, because AckReception was set.
    ack = FragDataBlockReceivedReq(frag_index=frag_index, mic_error=False)
    assert parse_uplink_commands(ack.encode()) == [ack]


def test_end_to_end_with_padding_and_status_report():
    """A block that does not divide evenly, plus a mid-transfer status report."""
    block = bytes(range(256)) * 2 + b"tail"  # 516 octets
    frag_size = 50
    encoder = FragmentationEncoder(block, frag_size)
    assert encoder.nb_frag == 11
    assert encoder.padding == 11 * 50 - 516 == 34

    decoder = FragmentationDecoder(encoder.nb_frag, frag_size)

    # Deliver only the first 6 uncoded fragments, then report status.
    for n in range(1, 7):
        wire = DataFragment(0, n, encoder.fragment(n)).encode()
        frag = parse_downlink_commands(wire)[0]
        assert isinstance(frag, DataFragment)
        decoder.receive(frag.index_n, frag.payload)
    assert not decoder.is_complete

    status = FragSessionStatusAns(
        frag_index=0,
        nb_frag_received=decoder.nb_frames_received,
        missing_frag=decoder.missing_count(),
    )
    assert status.nb_frag_received == 6
    assert status.missing_frag == 5
    assert parse_uplink_commands(status.encode()) == [status]

    # Server sends redundancy; N continues past M.
    n = encoder.nb_frag + 1
    while not decoder.is_complete:
        frag = parse_downlink_commands(DataFragment(0, n, encoder.fragment(n)).encode())[0]
        assert isinstance(frag, DataFragment)
        decoder.receive(frag.index_n, frag.payload)
        n += 1
        assert n < 100, "decoder failed to converge"

    assert decoder.reconstruct(encoder.padding) == block
    done = FragSessionStatusAns(frag_index=0, nb_frag_received=decoder.nb_frames_received)
    assert done.missing_frag == 0
    assert done.mic_error is False


# ═══════════════════════════════════════════════════════════════════════════
# Application classes (TS004-2.0.0 §3) — device side and network-server side
# ═══════════════════════════════════════════════════════════════════════════

DEV_ADDR = 0x26011234
DEV_ADDR_B = 0x26011235
DEV_ADDR_C = 0x26011236
NWK_S_KEY = bytes.fromhex("2B7E151628AED2A6ABF7158809CF4F3C")
APP_S_KEY = bytes.fromhex("3C4F9C098815F7ABA6D2AE281615E72B")
GEN_APP_KEY = bytes.fromhex("000102030405060708090A0B0C0D0E0F")
DATA_BLOCK_INT_KEY = derive_data_block_int_key(key=GEN_APP_KEY)

MC_ADDR = 0xFF000001
MC_NWK_KEY = bytes.fromhex("AABBCCDD11223344AABBCCDD11223344")
MC_APP_KEY = bytes.fromhex("11223344AABBCCDD11223344AABBCCDD")


def _build_uplink(dev_addr: int, fcnt: int, fport: int, payload: bytes) -> bytes:
    """A real encrypted + MIC'd uplink frame, as `NetworkServer.handle_uplink` wants it."""
    encrypted = encrypt_frm_payload(
        APP_S_KEY, dev_addr=dev_addr, fcnt=fcnt, uplink=True, payload=payload,
    )
    fhdr = FHDR(dev_addr=dev_addr, fctrl=FCtrl(), fcnt=fcnt)
    mac_payload = MACPayload(fhdr=fhdr, fport=fport, frm_payload=encrypted)
    mhdr = MHDR(mtype=MType.UNCONFIRMED_DATA_UP)
    mic = compute_data_mic(
        NWK_S_KEY, dev_addr=dev_addr, fcnt=fcnt, uplink=True,
        mhdr_and_payload=bytes([mhdr.encode()]) + mac_payload.encode(uplink=True),
    )
    return PHYPayload(mhdr=mhdr, mac_payload=mac_payload, mic=mic).encode()


def _decrypt_downlink(raw: bytes, dev_addr: int, fcnt: int) -> tuple[int, bytes]:
    """`(FPort, plaintext)` of a unicast downlink built by the network server."""
    phy = PHYPayload.decode_data(raw)
    assert phy.mac_payload is not None
    plain = encrypt_frm_payload(
        APP_S_KEY, dev_addr=dev_addr, fcnt=fcnt, uplink=False,
        payload=phy.mac_payload.frm_payload,
    )
    assert phy.mac_payload.fport is not None
    return phy.mac_payload.fport, plain


def _app_setup_req(data: bytes, frag_size: int, **kwargs) -> FragSessionSetupReq:
    """A `FragSessionSetupReq` matching `data`, MIC'd with `DATA_BLOCK_INT_KEY`."""
    encoder = FragmentationEncoder(data, frag_size)
    req = FragSessionSetupReq(
        nb_frag=encoder.nb_frag, frag_size=frag_size, padding=encoder.padding, **kwargs
    )
    return req.with_mic(DATA_BLOCK_INT_KEY, data)


def _block(size: int, seed: int = 1) -> bytes:
    return bytes(random.Random(seed).getrandbits(8) for _ in range(size))


async def _feed(
    app: FragmentationDeviceApplication,
    encoder: FragmentationEncoder,
    indices,
    frag_index: int = 0,
) -> None:
    """Hand a sequence of coded fragment indices to a device application."""
    for n in indices:
        await app.on_downlink(
            DataFragment(frag_index, n, encoder.fragment(n)).encode()
        )


def _single_uplink(app: FragmentationDeviceApplication):
    """Pop exactly one queued uplink and return its single parsed command."""
    payload = app.pop_pending_uplink()
    assert payload is not None, "expected an uplink to be queued"
    commands = parse_uplink_commands(payload)
    assert len(commands) == 1, commands
    return commands[0]


# ── Device: FragSessionSetupReq acceptance (§3.3) ───────────────────────────

class TestDeviceSetup:
    @pytest.mark.asyncio
    async def test_accepts_a_valid_setup(self):
        app = FragmentationDeviceApplication(gen_app_key=GEN_APP_KEY)
        data = _block(200)
        req = _app_setup_req(data, 20, frag_index=1, session_cnt=1, descriptor=0xDEADBEEF)

        await app.on_downlink(req.encode())

        assert isinstance(app.sessions[1], FragSessionState)

        ans = _single_uplink(app)
        assert isinstance(ans, FragSessionSetupAns)
        assert ans.accepted
        assert ans.frag_index == 1
        session = app.sessions[1]
        assert session.nb_frag == 10
        assert session.frag_size == 20
        assert session.descriptor == 0xDEADBEEF
        assert app.progress(1) == (0, 10)
        assert not app.is_complete(1)

    @pytest.mark.asyncio
    async def test_frag_algo_unsupported(self):
        app = FragmentationDeviceApplication()
        await app.on_downlink(_app_setup_req(_block(40), 10, frag_algo=1).encode())

        ans = _single_uplink(app)
        assert isinstance(ans, FragSessionSetupAns)
        assert ans.frag_algo_unsupported
        assert not ans.accepted
        assert app.sessions == {}

    @pytest.mark.asyncio
    async def test_frag_index_unsupported(self):
        app = FragmentationDeviceApplication(max_sessions=2)
        await app.on_downlink(_app_setup_req(_block(40), 10, frag_index=3).encode())

        ans = _single_uplink(app)
        assert isinstance(ans, FragSessionSetupAns)
        assert ans.frag_index_unsupported
        assert ans.frag_index == 3
        assert not ans.accepted

    @pytest.mark.asyncio
    async def test_not_enough_memory_from_nb_frag(self):
        app = FragmentationDeviceApplication(max_nb_frag=5)
        await app.on_downlink(_app_setup_req(_block(100), 10).encode())

        ans = _single_uplink(app)
        assert isinstance(ans, FragSessionSetupAns)
        assert ans.not_enough_memory
        assert not ans.accepted

    @pytest.mark.asyncio
    async def test_not_enough_memory_from_block_size(self):
        app = FragmentationDeviceApplication(max_block_size=64)
        await app.on_downlink(_app_setup_req(_block(100), 10).encode())

        ans = _single_uplink(app)
        assert isinstance(ans, FragSessionSetupAns)
        assert ans.not_enough_memory

    @pytest.mark.asyncio
    async def test_wrong_descriptor(self):
        app = FragmentationDeviceApplication(
            descriptor_filter=lambda descriptor: descriptor == 0x01020304
        )
        await app.on_downlink(_app_setup_req(_block(40), 10, descriptor=0x99).encode())
        ans = _single_uplink(app)
        assert isinstance(ans, FragSessionSetupAns)
        assert ans.wrong_descriptor
        assert not ans.accepted

        await app.on_downlink(
            _app_setup_req(_block(40), 10, descriptor=0x01020304, session_cnt=1).encode()
        )
        ans = _single_uplink(app)
        assert isinstance(ans, FragSessionSetupAns)
        assert ans.accepted

    @pytest.mark.asyncio
    async def test_first_session_may_use_session_cnt_zero(self):
        """SessionCntPrev starts at -1, so SessionCnt = 0 is legal once (§3.3)."""
        app = FragmentationDeviceApplication()
        assert app.last_session_cnt[0] == -1
        await app.on_downlink(_app_setup_req(_block(40), 10, session_cnt=0).encode())
        ans = _single_uplink(app)
        assert isinstance(ans, FragSessionSetupAns)
        assert ans.accepted

    @pytest.mark.asyncio
    async def test_session_cnt_replay_is_refused(self):
        """SessionCntPrev commits on the first DataFragment, not at setup (§3.3)."""
        app = FragmentationDeviceApplication()
        data = _block(40)
        encoder = FragmentationEncoder(data, 10)

        await app.on_downlink(_app_setup_req(data, 10, session_cnt=7).encode())
        assert _single_uplink(app).accepted
        # Nothing committed yet: a replay is still accepted at this point.
        assert app.last_session_cnt[0] == -1

        await _feed(app, encoder, [1])
        assert app.last_session_cnt[0] == 7

        for replayed in (0, 7):
            await app.on_downlink(
                _app_setup_req(data, 10, session_cnt=replayed).encode()
            )
            ans = _single_uplink(app)
            assert isinstance(ans, FragSessionSetupAns)
            assert ans.session_cnt_replay
            assert not ans.accepted

        await app.on_downlink(_app_setup_req(data, 10, session_cnt=8).encode())
        assert _single_uplink(app).accepted

    @pytest.mark.asyncio
    async def test_refused_setup_leaves_the_running_session_alone(self):
        """v2.0.0: the old session is replaced only if the new setup succeeds (§3.3)."""
        app = FragmentationDeviceApplication()
        data = _block(40)
        encoder = FragmentationEncoder(data, 10)
        await app.on_downlink(_app_setup_req(data, 10, session_cnt=1).encode())
        app.pop_pending_uplink()
        await _feed(app, encoder, [1, 2])
        assert app.progress(0) == (2, 4)

        # Refused: SessionCnt replay.
        await app.on_downlink(_app_setup_req(data, 10, session_cnt=1).encode())
        assert not _single_uplink(app).accepted
        assert app.progress(0) == (2, 4)

    @pytest.mark.asyncio
    async def test_accepted_setup_replaces_the_running_session(self):
        app = FragmentationDeviceApplication()
        data = _block(40)
        encoder = FragmentationEncoder(data, 10)
        await app.on_downlink(_app_setup_req(data, 10, session_cnt=1).encode())
        app.pop_pending_uplink()
        await _feed(app, encoder, [1, 2])
        assert app.progress(0) == (2, 4)

        await app.on_downlink(_app_setup_req(_block(80), 20, session_cnt=2).encode())
        assert _single_uplink(app).accepted
        assert app.progress(0) == (0, 4)
        assert app.sessions[0].frag_size == 20

    @pytest.mark.asyncio
    async def test_four_sessions_run_side_by_side(self):
        app = FragmentationDeviceApplication()
        for index in range(MAX_FRAG_SESSIONS):
            await app.on_downlink(
                _app_setup_req(_block(40, seed=index), 10, frag_index=index).encode()
            )
            ans = _single_uplink(app)
            assert ans.accepted and ans.frag_index == index
        assert sorted(app.sessions) == [0, 1, 2, 3]


# ── Device: DataFragment reception (§3.6) ──────────────────────────────────

class TestDeviceFragments:
    @pytest.mark.asyncio
    async def test_reconstructs_through_losses_and_verifies_the_mic(self):
        data = _block(600)
        encoder = FragmentationEncoder(data, 24)  # 25 fragments
        seen: list[tuple[int, bytes, int]] = []
        app = FragmentationDeviceApplication(
            gen_app_key=GEN_APP_KEY,
            on_block_received=lambda i, d, desc: seen.append((i, d, desc)),
        )
        await app.on_downlink(
            _app_setup_req(data, 24, session_cnt=3, descriptor=0xABCD).encode()
        )
        app.pop_pending_uplink()

        # Lose every third uncoded fragment, then stream redundancy until done.
        delivered = [n for n in range(1, encoder.nb_frag + 1) if n % 3 != 0]
        await _feed(app, encoder, delivered)
        assert not app.is_complete(0)

        n = encoder.nb_frag + 1
        while not app.is_complete(0):
            await _feed(app, encoder, [n])
            n += 1
            assert n < encoder.nb_frag + 60, "decoder failed to converge"

        assert app.completed_blocks[0] == data
        assert app.sessions[0].data == data
        assert app.sessions[0].mic_ok is True
        assert app.fragments_received == app.sessions[0].frames_received
        assert seen == [(0, data, 0xABCD)]

    @pytest.mark.asyncio
    async def test_mic_failure_with_the_wrong_key(self):
        data = _block(120)
        encoder = FragmentationEncoder(data, 12)
        app = FragmentationDeviceApplication(gen_app_key=bytes(16))
        await app.on_downlink(_app_setup_req(data, 12).encode())
        app.pop_pending_uplink()

        await _feed(app, encoder, range(1, encoder.nb_frag + 1))

        assert app.is_complete(0)
        assert app.completed_blocks[0] == data
        assert app.sessions[0].mic_ok is False

    @pytest.mark.asyncio
    async def test_without_a_key_the_mic_is_simply_not_checked(self):
        data = _block(120)
        encoder = FragmentationEncoder(data, 12)
        app = FragmentationDeviceApplication()
        await app.on_downlink(_app_setup_req(data, 12).encode())
        app.pop_pending_uplink()

        await _feed(app, encoder, range(1, encoder.nb_frag + 1))

        assert app.sessions[0].mic_ok is None

    @pytest.mark.asyncio
    async def test_fragments_after_completion_are_dropped(self):
        """§3.6: once reconstructed, further messages on that FragIndex are dropped."""
        data = _block(120)
        encoder = FragmentationEncoder(data, 12)
        app = FragmentationDeviceApplication(gen_app_key=GEN_APP_KEY)
        await app.on_downlink(_app_setup_req(data, 12).encode())
        app.pop_pending_uplink()

        await _feed(app, encoder, range(1, encoder.nb_frag + 1))
        received = app.fragments_received
        frames = app.sessions[0].frames_received

        await _feed(app, encoder, [1, 2, encoder.nb_frag + 1])

        assert app.fragments_received == received
        assert app.sessions[0].frames_received == frames
        assert app.fragments_dropped == 3

    @pytest.mark.asyncio
    async def test_fragments_for_an_unknown_session_are_dropped(self):
        app = FragmentationDeviceApplication()
        await app.on_downlink(DataFragment(2, 1, b"\x01" * 10).encode())
        assert app.fragments_dropped == 1
        assert app.fragments_received == 0

    @pytest.mark.asyncio
    async def test_a_fragment_of_the_wrong_length_is_dropped(self):
        app = FragmentationDeviceApplication()
        await app.on_downlink(_app_setup_req(_block(40), 10).encode())
        app.pop_pending_uplink()

        await app.on_downlink(DataFragment(0, 1, b"\x01" * 9).encode())

        assert app.fragments_dropped == 1
        assert app.progress(0) == (0, 4)

    @pytest.mark.asyncio
    async def test_duplicates_count_as_frames_but_not_as_progress(self):
        """NbFragReceived counts repeats; the decoder rank does not (§3.2)."""
        data = _block(40)
        encoder = FragmentationEncoder(data, 10)
        app = FragmentationDeviceApplication()
        await app.on_downlink(_app_setup_req(data, 10).encode())
        app.pop_pending_uplink()

        await _feed(app, encoder, [1, 1, 1, 2])

        assert app.sessions[0].frames_received == 4
        assert app.progress(0) == (2, 4)

    @pytest.mark.asyncio
    async def test_lmax_aborts_defragmentation_with_a_memory_error(self):
        data = _block(400)
        encoder = FragmentationEncoder(data, 20)  # 20 fragments
        app = FragmentationDeviceApplication(lmax=4)
        await app.on_downlink(_app_setup_req(data, 20).encode())
        app.pop_pending_uplink()

        # One coded fragment is enough: all 20 uncoded fragments count as lost.
        await _feed(app, encoder, [encoder.nb_frag + 1])

        assert app.sessions[0].memory_error
        assert not app.is_complete(0)
        assert app.fragments_dropped == 0
        # The session stops taking fragments once it has given up.
        await _feed(app, encoder, [1, 2])
        assert app.fragments_dropped == 2


# ── Device: FragSessionStatusReq / Ans (§3.2) ──────────────────────────────

class TestDeviceStatus:
    @pytest.mark.asyncio
    async def test_status_reports_frames_received_and_missing_uncoded(self):
        data = _block(400)
        encoder = FragmentationEncoder(data, 20)  # 20 fragments
        app = FragmentationDeviceApplication(rng=random.Random(1))
        await app.on_downlink(_app_setup_req(data, 20, frag_index=2).encode())
        app.pop_pending_uplink()

        # 11 frames, one of them a duplicate -> 10 distinct uncoded fragments.
        await _feed(app, encoder, [*range(1, 11), 1], frag_index=2)

        await app.on_downlink(FragSessionStatusReq(frag_index=2).encode())
        ans = _single_uplink(app)
        assert isinstance(ans, FragSessionStatusAns)
        assert ans.frag_index == 2
        assert ans.nb_frag_received == 11
        assert ans.missing_frag == 10
        assert ans.memory_error is False
        assert ans.mic_error is False
        assert ans.session_does_not_exist is False

    @pytest.mark.asyncio
    async def test_missing_frag_saturates_at_255(self):
        data = _block(1200)
        app = FragmentationDeviceApplication()
        await app.on_downlink(_app_setup_req(data, 4).encode())  # 300 fragments
        app.pop_pending_uplink()

        await app.on_downlink(FragSessionStatusReq().encode())
        ans = _single_uplink(app)
        assert isinstance(ans, FragSessionStatusAns)
        assert ans.missing_frag == MAX_MISSING_FRAG == 255

    @pytest.mark.asyncio
    async def test_unknown_session_answers_the_one_octet_form(self):
        app = FragmentationDeviceApplication()
        await app.on_downlink(FragSessionStatusReq(frag_index=1).encode())

        payload = app.pop_pending_uplink()
        assert payload is not None
        assert len(payload) == 2  # CID + 1-octet Status
        ans = parse_uplink_commands(payload)[0]
        assert isinstance(ans, FragSessionStatusAns)
        assert ans.session_does_not_exist

    @pytest.mark.asyncio
    async def test_participants_zero_keeps_a_finished_device_silent(self):
        data = _block(120)
        encoder = FragmentationEncoder(data, 12)
        app = FragmentationDeviceApplication(gen_app_key=GEN_APP_KEY)
        await app.on_downlink(_app_setup_req(data, 12).encode())
        app.pop_pending_uplink()
        await _feed(app, encoder, range(1, encoder.nb_frag + 1))

        await app.on_downlink(FragSessionStatusReq(participants=False).encode())
        assert app.pending_uplinks == []

    @pytest.mark.asyncio
    async def test_participants_one_gets_an_answer_from_everyone(self):
        data = _block(120)
        encoder = FragmentationEncoder(data, 12)
        app = FragmentationDeviceApplication(gen_app_key=GEN_APP_KEY)
        await app.on_downlink(_app_setup_req(data, 12).encode())
        app.pop_pending_uplink()
        await _feed(app, encoder, range(1, encoder.nb_frag + 1))

        await app.on_downlink(FragSessionStatusReq(participants=True).encode())
        ans = _single_uplink(app)
        assert isinstance(ans, FragSessionStatusAns)
        assert ans.missing_frag == 0
        assert ans.mic_error is False
        assert ans.nb_frag_received == encoder.nb_frag

    @pytest.mark.asyncio
    async def test_mic_error_is_reported_once_the_block_is_complete(self):
        data = _block(120)
        encoder = FragmentationEncoder(data, 12)
        app = FragmentationDeviceApplication(gen_app_key=bytes(16))
        await app.on_downlink(_app_setup_req(data, 12).encode())
        app.pop_pending_uplink()
        await _feed(app, encoder, range(1, encoder.nb_frag + 1))

        await app.on_downlink(FragSessionStatusReq(participants=True).encode())
        ans = _single_uplink(app)
        assert isinstance(ans, FragSessionStatusAns)
        assert ans.missing_frag == 0
        assert ans.mic_error is True

    @pytest.mark.asyncio
    async def test_memory_error_is_reported(self):
        data = _block(400)
        encoder = FragmentationEncoder(data, 20)
        app = FragmentationDeviceApplication(lmax=2)
        await app.on_downlink(_app_setup_req(data, 20).encode())
        app.pop_pending_uplink()
        await _feed(app, encoder, [encoder.nb_frag + 1])

        await app.on_downlink(FragSessionStatusReq().encode())
        ans = _single_uplink(app)
        assert isinstance(ans, FragSessionStatusAns)
        assert ans.memory_error is True

    @pytest.mark.asyncio
    async def test_the_answer_is_spread_over_the_block_ack_delay_window(self):
        """§3.2: answers are spread over rand() * 2**(BlockAckDelay + 4) seconds."""
        app = FragmentationDeviceApplication(rng=random.Random(7))
        await app.on_downlink(_app_setup_req(_block(40), 10, block_ack_delay=3).encode())
        app.pop_pending_uplink()

        delays: list[float] = []

        async def capture(payload: bytes, delay: float = 0.0) -> None:
            delays.append(delay)

        app.queue_uplink = capture  # type: ignore[method-assign]
        await app.on_downlink(FragSessionStatusReq().encode())

        assert len(delays) == 1
        assert 0.0 <= delays[0] < 2 ** (3 + 4)


# ── Device: delete, package version, completion acknowledgement ────────────

class TestDeviceMisc:
    @pytest.mark.asyncio
    async def test_delete_removes_the_session(self):
        app = FragmentationDeviceApplication()
        await app.on_downlink(_app_setup_req(_block(40), 10, frag_index=1).encode())
        app.pop_pending_uplink()

        await app.on_downlink(FragSessionDeleteReq(frag_index=1).encode())
        ans = _single_uplink(app)
        assert isinstance(ans, FragSessionDeleteAns)
        assert ans.accepted
        assert ans.frag_index == 1
        assert 1 not in app.sessions

    @pytest.mark.asyncio
    async def test_delete_of_an_unknown_session_is_refused(self):
        app = FragmentationDeviceApplication()
        await app.on_downlink(FragSessionDeleteReq(frag_index=3).encode())
        ans = _single_uplink(app)
        assert isinstance(ans, FragSessionDeleteAns)
        assert ans.session_does_not_exist
        assert not ans.accepted

    @pytest.mark.asyncio
    async def test_package_version(self):
        app = FragmentationDeviceApplication()
        await app.on_downlink(PackageVersionReq().encode())
        ans = _single_uplink(app)
        assert isinstance(ans, PackageVersionAns)
        assert (ans.package_identifier, ans.package_version) == (3, 2)

    @pytest.mark.asyncio
    async def test_ack_reception_sends_frag_data_block_received(self):
        data = _block(120)
        encoder = FragmentationEncoder(data, 12)
        app = FragmentationDeviceApplication(gen_app_key=GEN_APP_KEY, rng=random.Random(2))
        await app.on_downlink(_app_setup_req(data, 12, ack_reception=True).encode())
        app.pop_pending_uplink()

        await _feed(app, encoder, range(1, encoder.nb_frag + 1))

        req = _single_uplink(app)
        assert isinstance(req, FragDataBlockReceivedReq)
        assert req.frag_index == 0
        assert req.mic_error is False
        assert app.sessions[0].ack_pending

        await app.on_downlink(FragDataBlockReceivedAns(frag_index=0).encode())
        assert not app.sessions[0].ack_pending

    @pytest.mark.asyncio
    async def test_ack_reception_reports_a_mic_error(self):
        data = _block(120)
        encoder = FragmentationEncoder(data, 12)
        app = FragmentationDeviceApplication(gen_app_key=bytes(16))
        await app.on_downlink(_app_setup_req(data, 12, ack_reception=True).encode())
        app.pop_pending_uplink()

        await _feed(app, encoder, range(1, encoder.nb_frag + 1))

        req = _single_uplink(app)
        assert isinstance(req, FragDataBlockReceivedReq)
        assert req.mic_error is True

    @pytest.mark.asyncio
    async def test_no_ack_when_ack_reception_is_clear(self):
        data = _block(120)
        encoder = FragmentationEncoder(data, 12)
        app = FragmentationDeviceApplication(gen_app_key=GEN_APP_KEY)
        await app.on_downlink(_app_setup_req(data, 12, ack_reception=False).encode())
        app.pop_pending_uplink()

        await _feed(app, encoder, range(1, encoder.nb_frag + 1))

        assert app.pending_uplinks == []

    def test_port_is_201(self):
        assert FragmentationDeviceApplication().port() == FRAGMENTATION_FPORT

    def test_max_sessions_is_validated(self):
        with pytest.raises(ValueError):
            FragmentationDeviceApplication(max_sessions=5)


# ── Server: session creation and fragment scheduling (§3.3, §3.6) ──────────

def _server(**kwargs) -> tuple[NetworkServer, FragmentationServerApplication]:
    ns = NetworkServer()
    for addr in (DEV_ADDR, DEV_ADDR_B, DEV_ADDR_C):
        ns.register_device(addr, NWK_S_KEY, APP_S_KEY)
    ns.create_multicast_group(MC_ADDR, MC_NWK_KEY, MC_APP_KEY)
    app = FragmentationServerApplication(ns, time_provider=lambda: 0.0, **kwargs)
    ns.register_application(app)
    return ns, app


class TestServerSessionCreation:
    @pytest.mark.asyncio
    async def test_setup_request_carries_a_per_device_mic(self):
        other_key = derive_data_block_int_key(key=bytes.fromhex("FF" * 16))
        _ns, server = _server(
            key_provider={DEV_ADDR: DATA_BLOCK_INT_KEY, DEV_ADDR_B: other_key}
        )
        data = _block(500)

        session = server.create_session(
            [DEV_ADDR, DEV_ADDR_B],
            frag_index=1,
            data=data,
            frag_size=50,
            session_cnt=4,
            mc_group_bit_mask=0b0001,
            descriptor=0x01020304,
            block_ack_delay=2,
            ack_reception=True,
        )

        assert isinstance(session, FragServerSession)
        assert session.nb_frag == 10
        assert session.frag_size == 50
        assert session.padding == 0
        assert session.total_fragments == 10

        payload = await server.get_downlink(DEV_ADDR)
        assert payload is not None
        req = parse_downlink_commands(payload)[0]
        assert isinstance(req, FragSessionSetupReq)
        assert req.frag_index == 1
        assert req.nb_frag == 10
        assert req.frag_size == 50
        assert req.mc_group_bit_mask == 0b0001
        assert req.descriptor == 0x01020304
        assert req.session_cnt == 4
        assert req.block_ack_delay == 2
        assert req.ack_reception is True
        assert req.frag_algo == 0
        # The MIC verifies with this device's key and only with this one.
        assert req.verify_mic(DATA_BLOCK_INT_KEY, data)
        assert not req.verify_mic(other_key, data)

        payload_b = await server.get_downlink(DEV_ADDR_B)
        assert payload_b is not None
        req_b = parse_downlink_commands(payload_b)[0]
        assert isinstance(req_b, FragSessionSetupReq)
        assert req_b.verify_mic(other_key, data)
        assert req_b.mic != req.mic

    @pytest.mark.asyncio
    async def test_padding_follows_the_encoder(self):
        _ns, server = _server(key_provider={DEV_ADDR: DATA_BLOCK_INT_KEY})
        data = _block(95)
        session = server.create_session(
            [DEV_ADDR], frag_index=0, data=data, frag_size=10, session_cnt=1
        )
        assert (session.nb_frag, session.padding) == (10, 5)

        payload = await server.get_downlink(DEV_ADDR)
        assert payload is not None
        req = parse_downlink_commands(payload)[0]
        assert isinstance(req, FragSessionSetupReq)
        assert req.padding == 5
        assert req.block_size == len(data)
        # The MIC is over the un-padded block.
        assert req.verify_mic(DATA_BLOCK_INT_KEY, data)

    @pytest.mark.asyncio
    async def test_a_device_without_a_key_gets_a_zero_mic(self):
        _ns, server = _server()
        server.create_session(
            [DEV_ADDR], frag_index=0, data=_block(40), frag_size=10, session_cnt=1
        )
        payload = await server.get_downlink(DEV_ADDR)
        assert payload is not None
        req = parse_downlink_commands(payload)[0]
        assert isinstance(req, FragSessionSetupReq)
        assert req.mic == b"\x00\x00\x00\x00"

    def test_redundancy_ratio_rounds_up(self):
        _ns, server = _server()
        session = server.create_session(
            [DEV_ADDR], frag_index=0, data=_block(400), frag_size=20,
            session_cnt=1, redundancy_ratio=0.25,
        )
        assert session.nb_frag == 20
        assert session.redundancy_fragments == 5
        assert session.total_fragments == 25

    def test_redundancy_arguments_are_exclusive(self):
        _ns, server = _server()
        with pytest.raises(ValueError):
            server.create_session(
                [DEV_ADDR], frag_index=0, data=_block(40), frag_size=10,
                session_cnt=1, redundancy_fragments=2, redundancy_ratio=0.1,
            )

    def test_fragment_payload_size_for_matches_the_region(self):
        assert FragmentationServerApplication.fragment_payload_size_for(0) == 48
        assert FragmentationServerApplication.fragment_payload_size_for(3) == 112
        assert FragmentationServerApplication.fragment_payload_size_for(5) == 219
        assert FragmentationServerApplication.fragment_payload_size_for(5, 15) == (
            max_frm_payload(5, 15) - DATA_FRAGMENT_HEADER_SIZE
        )

    def test_port_is_201(self):
        _ns, server = _server()
        assert server.port() == FRAGMENTATION_FPORT


class TestServerFragmentScheduling:
    def test_broadcast_schedules_the_whole_session(self):
        ns, server = _server()
        data = _block(400)
        session = server.create_session(
            [DEV_ADDR], frag_index=0, data=data, frag_size=20,
            session_cnt=1, redundancy_fragments=4,
        )

        entries = server.broadcast_fragments(
            0, group_addr=MC_ADDR, start_time=10.0, interval=2.0
        )

        assert len(entries) == 24 == session.total_fragments
        assert session.highest_n_sent == 24
        queued = ns.pending_multicast_downlinks()
        assert len(queued) == 24
        assert [e.at_time for e in queued[:3]] == [10.0, 12.0, 14.0]
        assert all(e.fport == FRAGMENTATION_FPORT for e in queued)
        assert all(e.group_addr == MC_ADDR for e in queued)

        # The payloads decode back to the encoder's own fragments.
        encoder = FragmentationEncoder(data, 20)
        for k, entry in enumerate(queued, start=1):
            fragment = parse_downlink_commands(entry.payload)[0]
            assert isinstance(fragment, DataFragment)
            assert fragment.frag_index == 0
            assert fragment.index_n == k
            assert fragment.payload == encoder.fragment(k)
            assert len(entry.payload) == 20 + DATA_FRAGMENT_HEADER_SIZE

    def test_broadcast_with_an_explicit_count_and_start(self):
        ns, server = _server()
        server.create_session(
            [DEV_ADDR], frag_index=2, data=_block(400), frag_size=20, session_cnt=1
        )
        server.broadcast_fragments(
            2, group_addr=MC_ADDR, start_time=0.0, interval=1.0
        )
        ns.pop_due_multicast_downlinks(1e9)

        entries = server.broadcast_fragments(
            2, group_addr=MC_ADDR, start_time=100.0, interval=0.5, count=3, start_n=21
        )

        assert len(entries) == 3
        indices = []
        for entry in entries:
            fragment = parse_downlink_commands(entry.payload)[0]
            assert isinstance(fragment, DataFragment)
            assert fragment.frag_index == 2
            indices.append(fragment.index_n)
        assert indices == [21, 22, 23]
        assert [e.at_time for e in entries] == [100.0, 100.5, 101.0]
        assert server.sessions[2].highest_n_sent == 23

    def test_broadcast_rejects_a_zero_index(self):
        _ns, server = _server()
        server.create_session(
            [DEV_ADDR], frag_index=0, data=_block(40), frag_size=10, session_cnt=1
        )
        with pytest.raises(ValueError):
            server.broadcast_fragments(
                0, group_addr=MC_ADDR, start_time=0.0, interval=1.0, start_n=0
            )

    def test_broadcast_of_an_unknown_session(self):
        _ns, server = _server()
        with pytest.raises(KeyError):
            server.broadcast_fragments(
                1, group_addr=MC_ADDR, start_time=0.0, interval=1.0
            )

    @pytest.mark.asyncio
    async def test_unicast_repair_fragment(self):
        ns, server = _server()
        data = _block(400)
        server.create_session(
            [DEV_ADDR], frag_index=0, data=data, frag_size=20, session_cnt=1
        )
        await server.get_downlink(DEV_ADDR)  # drain the setup request

        server.send_fragment_unicast(DEV_ADDR, 0, 25)

        # It goes on the network server's explicit queue, not this package's.
        assert await server.get_downlink(DEV_ADDR) is None
        uplink = _build_uplink(DEV_ADDR, 0, FRAGMENTATION_FPORT, b"")
        raw = await ns.handle_uplink(uplink)
        assert raw is not None
        fport, plain = _decrypt_downlink(raw, DEV_ADDR, 0)
        assert fport == FRAGMENTATION_FPORT
        fragment = parse_downlink_commands(plain)[0]
        assert isinstance(fragment, DataFragment)
        assert fragment.index_n == 25
        assert fragment.payload == FragmentationEncoder(data, 20).fragment(25)


# ── Server: uplink answers (§3.2–§3.5) ─────────────────────────────────────

class TestServerAnswers:
    @pytest.mark.asyncio
    async def test_setup_answers_are_tracked(self):
        _ns, server = _server()
        server.create_session(
            [DEV_ADDR, DEV_ADDR_B], frag_index=0, data=_block(40),
            frag_size=10, session_cnt=1,
        )

        await server.on_uplink(DEV_ADDR, FragSessionSetupAns(frag_index=0).encode())
        await server.on_uplink(
            DEV_ADDR_B,
            FragSessionSetupAns(frag_index=0, not_enough_memory=True).encode(),
        )

        assert server.devices_acked_setup(0) == {DEV_ADDR}
        assert server.sessions[0].setup_answers[DEV_ADDR_B].not_enough_memory

    @pytest.mark.asyncio
    async def test_status_answers_feed_max_missing(self):
        _ns, server = _server()
        server.create_session(
            [DEV_ADDR, DEV_ADDR_B], frag_index=0, data=_block(400),
            frag_size=20, session_cnt=1,
        )

        await server.on_uplink(
            DEV_ADDR,
            FragSessionStatusAns(frag_index=0, nb_frag_received=18, missing_frag=3).encode(),
        )
        await server.on_uplink(
            DEV_ADDR_B,
            FragSessionStatusAns(frag_index=0, nb_frag_received=20, missing_frag=0).encode(),
        )

        assert len(server.status_reports) == 2
        assert server.max_missing(0) == 3
        assert server.devices_complete(0) == {DEV_ADDR_B}
        report = server.latest_status[(0, DEV_ADDR)]
        assert (report.nb_frag_received, report.missing_frag) == (18, 3)
        assert report.complete is False

        # A later answer replaces the older one.
        await server.on_uplink(
            DEV_ADDR,
            FragSessionStatusAns(frag_index=0, nb_frag_received=24, missing_frag=0).encode(),
        )
        assert server.max_missing(0) == 0
        assert server.devices_complete(0) == {DEV_ADDR, DEV_ADDR_B}

    @pytest.mark.asyncio
    async def test_a_session_that_does_not_exist_is_not_counted_as_missing(self):
        _ns, server = _server()
        server.create_session(
            [DEV_ADDR], frag_index=0, data=_block(40), frag_size=10, session_cnt=1
        )
        await server.on_uplink(
            DEV_ADDR, FragSessionStatusAns(session_does_not_exist=True).encode()
        )
        assert server.max_missing(0) == 0
        assert server.devices_complete(0) == set()

    @pytest.mark.asyncio
    async def test_block_received_is_acknowledged(self):
        _ns, server = _server()
        server.create_session(
            [DEV_ADDR], frag_index=1, data=_block(40), frag_size=10,
            session_cnt=1, ack_reception=True,
        )
        await server.get_downlink(DEV_ADDR)  # drain the setup request

        await server.on_uplink(
            DEV_ADDR, FragDataBlockReceivedReq(frag_index=1).encode()
        )

        assert server.devices_complete(1) == {DEV_ADDR}
        payload = await server.get_downlink(DEV_ADDR)
        assert payload is not None
        ans = parse_downlink_commands(payload)[0]
        assert isinstance(ans, FragDataBlockReceivedAns)
        assert ans.frag_index == 1

    @pytest.mark.asyncio
    async def test_a_reported_mic_error_is_recorded(self):
        _ns, server = _server()
        server.create_session(
            [DEV_ADDR], frag_index=0, data=_block(40), frag_size=10,
            session_cnt=1, ack_reception=True,
        )
        await server.on_uplink(
            DEV_ADDR, FragDataBlockReceivedReq(frag_index=0, mic_error=True).encode()
        )
        assert server.sessions[0].mic_errors == {DEV_ADDR}

    @pytest.mark.asyncio
    async def test_delete_round_trip(self):
        _ns, server = _server()
        server.create_session(
            [DEV_ADDR], frag_index=0, data=_block(40), frag_size=10, session_cnt=1
        )
        await server.get_downlink(DEV_ADDR)

        server.delete_session([DEV_ADDR], 0)
        payload = await server.get_downlink(DEV_ADDR)
        assert payload is not None
        req = parse_downlink_commands(payload)[0]
        assert isinstance(req, FragSessionDeleteReq)
        assert req.frag_index == 0

        await server.on_uplink(DEV_ADDR, FragSessionDeleteAns(frag_index=0).encode())
        assert server.sessions[0].delete_answers[DEV_ADDR].accepted

    @pytest.mark.asyncio
    async def test_request_status_queues_per_device(self):
        _ns, server = _server()
        server.create_session(
            [DEV_ADDR, DEV_ADDR_B], frag_index=0, data=_block(40),
            frag_size=10, session_cnt=1,
        )
        await server.get_downlink(DEV_ADDR)
        await server.get_downlink(DEV_ADDR_B)

        server.request_status([DEV_ADDR, DEV_ADDR_B], 0, participants=True)

        for addr in (DEV_ADDR, DEV_ADDR_B):
            payload = await server.get_downlink(addr)
            assert payload is not None
            req = parse_downlink_commands(payload)[0]
            assert isinstance(req, FragSessionStatusReq)
            assert req.frag_index == 0
            assert req.participants is True
        assert server.pending_downlinks(DEV_ADDR) == []

    @pytest.mark.asyncio
    async def test_on_answer_callback_sees_every_command(self):
        _ns, server = _server()
        seen: list[tuple[int, str]] = []
        server.on_answer = lambda addr, cmd: seen.append((addr, type(cmd).__name__))

        await server.on_uplink(
            DEV_ADDR,
            encode_commands([FragSessionSetupAns(), PackageVersionAns()]),
        )

        assert seen == [
            (DEV_ADDR, "FragSessionSetupAns"),
            (DEV_ADDR, "PackageVersionAns"),
        ]

    @pytest.mark.asyncio
    async def test_package_version_request(self):
        _ns, server = _server()
        server.request_package_version([DEV_ADDR])
        payload = await server.get_downlink(DEV_ADDR)
        assert payload == b"\x00"


# ── Device + server over the network server (§3) ───────────────────────────

class TestEndToEndOverNetworkServer:
    @pytest.mark.asyncio
    async def test_setup_answer_travels_back_through_handle_uplink(self):
        ns, server = _server(key_provider={DEV_ADDR: DATA_BLOCK_INT_KEY})
        device_app = FragmentationDeviceApplication(gen_app_key=GEN_APP_KEY)
        data = _block(500)
        server.create_session(
            [DEV_ADDR], frag_index=0, data=data, frag_size=50, session_cnt=1,
            descriptor=0xCAFE,
        )

        # Device uplink -> network server -> downlink carrying FragSessionSetupReq.
        raw = await ns.handle_uplink(_build_uplink(DEV_ADDR, 0, FRAGMENTATION_FPORT, b""))
        assert raw is not None
        fport, plain = _decrypt_downlink(raw, DEV_ADDR, 0)
        assert fport == FRAGMENTATION_FPORT
        await device_app.on_downlink(plain)

        assert device_app.sessions[0].nb_frag == 10
        assert device_app.sessions[0].descriptor == 0xCAFE

        # The device's answer travels back on the next uplink.
        answer = device_app.pop_pending_uplink()
        assert answer is not None
        await ns.handle_uplink(
            _build_uplink(DEV_ADDR, 1, FRAGMENTATION_FPORT, answer)
        )
        assert server.devices_acked_setup(0) == {DEV_ADDR}

    @pytest.mark.asyncio
    async def test_full_transfer_with_status_and_repair(self):
        """Setup, lossy broadcast, status round, repair round, completion ack."""
        ns, server = _server(key_provider={DEV_ADDR: DATA_BLOCK_INT_KEY})
        device_app = FragmentationDeviceApplication(
            gen_app_key=GEN_APP_KEY, rng=random.Random(5)
        )
        data = _block(1000)
        session = server.create_session(
            [DEV_ADDR], frag_index=0, data=data, frag_size=40, session_cnt=1,
            ack_reception=True, block_ack_delay=1,
        )
        assert session.nb_frag == 25

        setup_payload = await server.get_downlink(DEV_ADDR)
        assert setup_payload is not None
        await device_app.on_downlink(setup_payload)
        answer = device_app.pop_pending_uplink()
        assert answer is not None
        await server.on_uplink(DEV_ADDR, answer)
        assert server.devices_acked_setup(0) == {DEV_ADDR}

        # Broadcast round: every fifth frame is lost on the way to this device.
        entries = server.broadcast_fragments(
            0, group_addr=MC_ADDR, start_time=0.0, interval=1.0
        )
        for k, entry in enumerate(entries):
            if k % 5 == 4:
                continue
            await device_app.on_downlink(entry.payload)
        assert not device_app.is_complete(0)

        # Status round.
        server.request_status([DEV_ADDR], 0, participants=False)
        status_req = await server.get_downlink(DEV_ADDR)
        assert status_req is not None
        await device_app.on_downlink(status_req)
        status_ans = device_app.pop_pending_uplink()
        assert status_ans is not None
        await server.on_uplink(DEV_ADDR, status_ans)
        missing = server.max_missing(0)
        assert missing == 5

        # Repair round. A coded fragment can turn out to be linearly dependent, so
        # §A.3 budgets a handful on top of MissingFrag; here 5 of the 10 are wasted.
        repair = server.broadcast_fragments(
            0, group_addr=MC_ADDR, start_time=100.0, interval=1.0,
            count=missing + 5, start_n=session.highest_n_sent + 1,
        )
        for entry in repair:
            await device_app.on_downlink(entry.payload)

        assert device_app.is_complete(0)
        assert device_app.completed_blocks[0] == data
        assert device_app.sessions[0].mic_ok is True

        # Completion acknowledgement.
        ack = device_app.pop_pending_uplink()
        assert ack is not None
        await server.on_uplink(DEV_ADDR, ack)
        assert server.devices_complete(0) == {DEV_ADDR}
        ack_ans = await server.get_downlink(DEV_ADDR)
        assert ack_ans is not None
        await device_app.on_downlink(ack_ans)
        assert not device_app.sessions[0].ack_pending


# ═══════════════════════════════════════════════════════════════════════════
# Spec-conformance regressions (TS004-2.0.0 §3.2, §3.3, §3.5, §3.6, §A.4)
# ═══════════════════════════════════════════════════════════════════════════

class TestLmaxIsEvaluatedAfterTheUncodedFragments:
    """§A.4: ``L`` is the number of fragments lost *among the first M*.

    It is only knowable once the transmitter has moved past ``N = M``. Evaluating the
    budget on every fragment made every session with ``Lmax < NbFrag`` abort on its very
    first fragment, because nothing had been transmitted yet.
    """

    @pytest.mark.asyncio
    async def test_a_small_lmax_does_not_abort_a_healthy_session(self):
        data = _block(400)
        encoder = FragmentationEncoder(data, 20)  # NbFrag = 20
        app = FragmentationDeviceApplication(lmax=4)  # Lmax well below NbFrag
        await app.on_downlink(_app_setup_req(data, 20).encode())
        app.pop_pending_uplink()

        # The very first fragment used to trip the budget: 19 uncoded fragments were not
        # received *yet*, which is not the same as lost.
        await _feed(app, encoder, [1])
        assert app.sessions[0].memory_error is False

        await _feed(app, encoder, range(2, encoder.nb_frag + 1))
        assert app.sessions[0].memory_error is False
        assert app.is_complete(0)
        assert app.completed_blocks[0] == data

    @pytest.mark.asyncio
    async def test_exactly_lmax_losses_are_still_tolerated(self):
        data = _block(400)
        encoder = FragmentationEncoder(data, 20)
        app = FragmentationDeviceApplication(lmax=4)
        await app.on_downlink(_app_setup_req(data, 20).encode())
        app.pop_pending_uplink()

        lost = {3, 6, 9, 12}  # exactly Lmax
        await _feed(app, encoder, [n for n in range(1, 21) if n not in lost])
        await _feed(app, encoder, [21])  # first coded fragment: the budget is now checked

        assert app.sessions[0].memory_error is False
        assert app.sessions[0].lost_uncoded_count() == 4

    @pytest.mark.asyncio
    async def test_more_than_lmax_losses_abort_once_the_block_is_past_m(self):
        data = _block(400)
        encoder = FragmentationEncoder(data, 20)
        app = FragmentationDeviceApplication(lmax=4)
        await app.on_downlink(_app_setup_req(data, 20).encode())
        app.pop_pending_uplink()

        lost = {3, 6, 9, 12, 15}  # one more than Lmax
        await _feed(app, encoder, [n for n in range(1, 21) if n not in lost])
        assert app.sessions[0].memory_error is False, "not knowable before N > M"

        await _feed(app, encoder, [21])
        assert app.sessions[0].memory_error is True
        assert app.sessions[0].lost_uncoded_count() == 5

        await app.on_downlink(FragSessionStatusReq().encode())
        ans = _single_uplink(app)
        assert isinstance(ans, FragSessionStatusAns)
        assert ans.memory_error is True


class TestMissingFragIsTheRankDeficit:
    """§3.2: ``MissingFrag`` is "the minimum number of independent coded fragments still
    required before being able to reconstruct the data block"."""

    @staticmethod
    async def _app_with_28_of_32(app_kwargs: dict | None = None):
        data = _block(320)
        encoder = FragmentationEncoder(data, 10)  # NbFrag = 32
        assert encoder.nb_frag == 32
        app = FragmentationDeviceApplication(**(app_kwargs or {}))
        await app.on_downlink(_app_setup_req(data, 10).encode())
        app.pop_pending_uplink()
        await _feed(app, encoder, range(1, 29))  # 28 uncoded fragments
        return app, encoder

    @pytest.mark.asyncio
    async def test_coded_fragments_reduce_missing_frag(self):
        app, encoder = await self._app_with_28_of_32()
        decoder = app.sessions[0].decoder
        assert decoder.nb_received == 28
        assert app.sessions[0].missing_frag_count() == 4

        # Feed coded fragments until two of them were linearly independent.
        n = encoder.nb_frag + 1
        while decoder.nb_received < 30:
            await _feed(app, encoder, [n])
            n += 1

        # Four uncoded fragments were never heard directly, but only two more independent
        # fragments are actually needed — and that is what goes on the wire.
        assert app.sessions[0].lost_uncoded_count() == 4
        assert app.sessions[0].missing_frag_count() == 2

        await app.on_downlink(FragSessionStatusReq().encode())
        ans = _single_uplink(app)
        assert isinstance(ans, FragSessionStatusAns)
        assert ans.missing_frag == 2

    @pytest.mark.asyncio
    async def test_a_complete_block_reports_zero(self):
        app, encoder = await self._app_with_28_of_32()
        n = encoder.nb_frag + 1
        while not app.is_complete(0):
            await _feed(app, encoder, [n])
            n += 1
        assert app.sessions[0].missing_frag_count() == 0

    @pytest.mark.asyncio
    async def test_the_server_sizes_a_repair_round_from_the_rank_deficit(self):
        app, encoder = await self._app_with_28_of_32()
        decoder = app.sessions[0].decoder
        n = encoder.nb_frag + 1
        while decoder.nb_received < 30:
            await _feed(app, encoder, [n])
            n += 1

        await app.on_downlink(FragSessionStatusReq().encode())
        answer = app.pop_pending_uplink()
        assert answer is not None

        ns, server = _server()
        await server.on_uplink(DEV_ADDR, answer)
        assert server.max_missing(0) == 2


class TestSetupRejectsAZeroFragSize:
    @pytest.mark.asyncio
    async def test_frag_size_zero_is_refused_with_not_enough_memory(self):
        app = FragmentationDeviceApplication()
        req = FragSessionSetupReq(nb_frag=4, frag_size=0, session_cnt=1)
        await app.on_downlink(req.encode())

        ans = _single_uplink(app)
        assert isinstance(ans, FragSessionSetupAns)
        assert ans.not_enough_memory is True
        assert ans.accepted is False
        assert app.sessions == {}


class TestDataFragmentIndexMasking:
    def test_index_and_n_masks_both_fields(self):
        assert DataFragment(1, 5, b"").index_and_n == (1 << 14) | 5
        # An out-of-range N can no longer bleed into the FragIndex bits.
        assert DataFragment(0, MAX_NB_FRAG + 2, b"").index_and_n == 1
        assert DataFragment(5, 1, b"").index_and_n == (1 << 14) | 1

    def test_encode_payload_still_rejects_out_of_range_values(self):
        with pytest.raises(ValueError):
            DataFragment(0, MAX_NB_FRAG + 2, b"").encode_payload()


class TestServerHasDownlinkIsNonDestructive:
    @pytest.mark.asyncio
    async def test_probing_does_not_consume_a_queued_command(self):
        ns, server = _server()
        data = _block(200)
        server.create_session(
            [DEV_ADDR], frag_index=0, data=data, frag_size=20, session_cnt=1
        )
        assert len(server.pending_downlinks(DEV_ADDR)) == 1

        assert await server.has_downlink(DEV_ADDR) is True
        assert await ns.has_pending_downlink(DEV_ADDR) is True
        # Neither probe may pop the FragSessionSetupReq.
        assert len(server.pending_downlinks(DEV_ADDR)) == 1

        assert await server.get_downlink(DEV_ADDR) is not None
        assert await server.has_downlink(DEV_ADDR) is False
        assert await ns.has_pending_downlink(DEV_ADDR) is False


# ═══════════════════════════════════════════════════════════════════════════
# The shared uplink queue (FuotaDeviceApplication)
# ═══════════════════════════════════════════════════════════════════════════

class _RecordingDevice:
    """The only part of ``LoRaWanDevice`` a FUOTA package's uplink queue touches."""

    def __init__(self) -> None:
        self.sent: list[tuple[float, int, bytes]] = []

    async def send_uplink(self, fport: int, payload: bytes) -> None:
        self.sent.append((sim.current_time(), fport, payload))


class TestUplinkQueueDelays:
    def test_each_payload_keeps_its_own_delay(self):
        """A delayed answer must not swap places with an undelayed one queued after it.

        Popping "the queue head" after sleeping sent ``p1`` immediately and held ``p2``
        back for 10 s — exactly backwards, and enough to defeat the §3.2 ``BlockAckDelay``
        spreading of a status answer concatenated behind a setup request.
        """
        device = _RecordingDevice()
        app = FragmentationDeviceApplication(device)  # type: ignore[arg-type]

        async def driver() -> None:
            await app.queue_uplink(b"p1", delay=10.0)
            await app.queue_uplink(b"p2", delay=0.0)

        sim.create_task(driver())
        sim.run(simulation_length=30)

        assert [payload for _t, _p, payload in device.sent] == [b"p2", b"p1"]
        assert device.sent[0][0] == pytest.approx(0.0, abs=1e-6)
        assert device.sent[1][0] == pytest.approx(10.0, abs=1e-6)

    def test_equal_delays_keep_fifo_order(self):
        device = _RecordingDevice()
        app = FragmentationDeviceApplication(device)  # type: ignore[arg-type]

        async def driver() -> None:
            for index in range(4):
                await app.queue_uplink(bytes([index]), delay=5.0)

        sim.create_task(driver())
        sim.run(simulation_length=20)

        assert [payload for _t, _p, payload in device.sent] == [
            b"\x00", b"\x01", b"\x02", b"\x03"
        ]

    def test_pop_with_a_timestamp_skips_payloads_that_are_not_due(self):
        app = FragmentationDeviceApplication()  # no device: the queue is drained by hand
        results: list[bytes | None] = []

        async def driver() -> None:
            await app.queue_uplink(b"late", delay=10.0)
            await app.queue_uplink(b"now")
            results.append(app.pop_pending_uplink(sim.current_time()))
            results.append(app.pop_pending_uplink(sim.current_time()))
            await sim.sleep(11.0)
            results.append(app.pop_pending_uplink(sim.current_time()))

        sim.create_task(driver())
        sim.run(simulation_length=20)

        assert results == [b"now", None, b"late"]

    def test_a_late_bound_payload_is_built_at_send_time(self):
        device = _RecordingDevice()
        app = FragmentationDeviceApplication(device)  # type: ignore[arg-type]

        async def driver() -> None:
            await app.queue_uplink(
                lambda: f"{sim.current_time():.0f}".encode(), delay=7.0
            )

        sim.create_task(driver())
        sim.run(simulation_length=20)

        assert [payload for _t, _p, payload in device.sent] == [b"7"]


class TestBlockReceivedRetriesWithoutADevice:
    """§3.5: the application retransmits ``FragDataBlockReceivedReq`` until it is answered.

    The TS004 package in :class:`~simulator.lorawan.fuota.device_stack.FuotaDeviceStack`
    runs with ``device=None``, so the retry loop has to work on the queue alone.
    """

    @staticmethod
    def _complete(app: FragmentationDeviceApplication, data: bytes, encoder):
        return _feed(app, encoder, range(1, encoder.nb_frag + 1))

    def test_the_request_is_requeued_until_it_is_answered(self):
        data = _block(200)
        encoder = FragmentationEncoder(data, 20)
        app = FragmentationDeviceApplication(ack_retry_interval=5.0, max_ack_retries=3)
        drained: list[tuple[float, bytes]] = []

        async def driver() -> None:
            await app.on_downlink(
                _app_setup_req(data, 20, ack_reception=True, session_cnt=1).encode()
            )
            app.pop_pending_uplink()
            await self._complete(app, data, encoder)
            assert app.is_complete(0)

            # A firmware-style drain loop, like the device stack's.
            for _ in range(60):
                await sim.sleep(1.0)
                payload = app.pop_pending_uplink(sim.current_time())
                if payload is not None:
                    drained.append((sim.current_time(), payload))

        sim.create_task(driver())
        sim.run(simulation_length=70)

        assert len(drained) == 4, drained  # first transmission + max_ack_retries
        assert app.sessions[0].ack_attempts == 4
        # Nothing goes out before the retry interval has elapsed.
        assert drained[0][0] >= 5.0
        for (earlier, _), (later, _) in zip(drained, drained[1:]):
            assert later - earlier >= 5.0 - 1e-6
        for _time, payload in drained:
            command = parse_uplink_commands(payload)[0]
            assert isinstance(command, FragDataBlockReceivedReq)

    def test_the_answer_stops_the_retransmissions(self):
        data = _block(200)
        encoder = FragmentationEncoder(data, 20)
        app = FragmentationDeviceApplication(ack_retry_interval=5.0, max_ack_retries=5)
        drained: list[bytes] = []

        async def driver() -> None:
            await app.on_downlink(
                _app_setup_req(data, 20, ack_reception=True, session_cnt=1).encode()
            )
            app.pop_pending_uplink()
            await self._complete(app, data, encoder)

            for _ in range(40):
                await sim.sleep(1.0)
                payload = app.pop_pending_uplink(sim.current_time())
                if payload is None:
                    continue
                drained.append(payload)
                if len(drained) == 2:
                    await app.on_downlink(
                        encode_commands([FragDataBlockReceivedAns(frag_index=0)])
                    )

        sim.create_task(driver())
        sim.run(simulation_length=50)

        assert len(drained) == 2
        assert app.sessions[0].ack_pending is False
