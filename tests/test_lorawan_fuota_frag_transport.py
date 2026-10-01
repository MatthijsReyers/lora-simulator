"""Tests for the TS004-2.0.0 Fragmented Data Block Transport wire codecs.

Hex vectors are hand-written from the normative tables of
*LoRaWAN Fragmented Data Block Transport* TS004-2.0.0 (April 2022) §3,
Tables 1-24, and checked bit position by bit position.
"""

import logging
import random

import pytest

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
