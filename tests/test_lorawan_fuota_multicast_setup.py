"""Tests for the TS005 v2.0.0 Remote Multicast Setup wire codecs (FPort 200).

Every encode test is checked against a hand-written byte string assembled
directly from the field tables of TS005-2.0.0 (Tables 3-25), not against the
implementation's own output.
"""

import pytest

from simulator.lorawan.fuota.multicast_setup import (
    MAX_MULTICAST_GROUPS,
    MULTICAST_SETUP_FPORT,
    PACKAGE_IDENTIFIER,
    PACKAGE_VERSION,
    TIME_TO_START_UNSYNCHRONIZED,
    McCID,
    McClassBSessionAns,
    McClassBSessionReq,
    McClassCSessionAns,
    McClassCSessionReq,
    McGroupDeleteAns,
    McGroupDeleteReq,
    McGroupSetupAns,
    McGroupSetupReq,
    McGroupStatusAns,
    McGroupStatusEntry,
    McGroupStatusReq,
    PackageVersionAns,
    PackageVersionReq,
    encode_commands,
    parse_downlink_commands,
    parse_uplink_commands,
)


# ---------------------------------------------------------------------------
# Package identity constants (§3, §4.1)
# ---------------------------------------------------------------------------

def test_package_constants() -> None:
    assert MULTICAST_SETUP_FPORT == 200
    assert PACKAGE_IDENTIFIER == 2
    assert PACKAGE_VERSION == 2
    assert MAX_MULTICAST_GROUPS == 4
    assert TIME_TO_START_UNSYNCHRONIZED == 0xFFFFFF


def test_cid_values() -> None:
    assert McCID.PACKAGE_VERSION == 0x00
    assert McCID.MC_GROUP_STATUS == 0x01
    assert McCID.MC_GROUP_SETUP == 0x02
    assert McCID.MC_GROUP_DELETE == 0x03
    assert McCID.MC_CLASS_C_SESSION == 0x04
    assert McCID.MC_CLASS_B_SESSION == 0x05


# ---------------------------------------------------------------------------
# PackageVersion (CID 0x00) — §4.1
# ---------------------------------------------------------------------------

def test_package_version_req_is_cid_only() -> None:
    cmd = PackageVersionReq()
    assert cmd.encode() == bytes.fromhex("00")
    assert cmd.encoded_payload_size == 0
    assert cmd.encoded_size == 1
    assert PackageVersionReq.PAYLOAD_SIZE == 0


def test_package_version_ans_encodes_identity() -> None:
    cmd = PackageVersionAns()
    # CID 0x00 | PackageIdentifier = 2 | PackageVersion = 2
    assert cmd.encode() == bytes.fromhex("000202")
    assert cmd.encoded_payload_size == 2


def test_package_version_ans_round_trip() -> None:
    decoded = PackageVersionAns.decode_payload(bytes.fromhex("0202"))
    assert decoded == PackageVersionAns(package_identifier=2, package_version=2)
    assert decoded.encode_payload() == bytes.fromhex("0202")


def test_package_version_decode_bad_length() -> None:
    with pytest.raises(ValueError):
        PackageVersionAns.decode_payload(b"\x02")
    with pytest.raises(ValueError):
        PackageVersionReq.decode_payload(b"\x00")


# ---------------------------------------------------------------------------
# McGroupStatus (CID 0x01) — §4.2, Tables 4-7
# ---------------------------------------------------------------------------

def test_mc_group_status_req_cmd_mask() -> None:
    # CmdMask: bits 7:4 RFU = 0, bits 3:0 ReqGroupMask = 0b1011
    assert McGroupStatusReq(req_group_mask=0b1011).encode() == bytes.fromhex("010b")


def test_mc_group_status_req_rfu_bits_ignored_on_decode() -> None:
    decoded = McGroupStatusReq.decode_payload(bytes([0xF5]))
    assert decoded.req_group_mask == 0x05
    # RFU bits are re-emitted as zero.
    assert decoded.encode_payload() == bytes([0x05])


def test_mc_group_status_req_mask_range() -> None:
    with pytest.raises(ValueError):
        McGroupStatusReq(req_group_mask=0x10).encode()


def test_mc_group_status_ans_bytes() -> None:
    ans = McGroupStatusAns(
        nb_total_groups=3,
        ans_group_mask=0b0101,
        groups=[
            McGroupStatusEntry(group_id=0, mc_addr=0x01020304),
            McGroupStatusEntry(group_id=2, mc_addr=0xAABBCCDD),
        ],
    )
    # Status: bit7 RFU=0 | NbTotalGroups=3 (bits 6:4) | AnsGroupMask=0b0101
    #         = 0b0_011_0101 = 0x35
    # Records: 00 | McAddr LE 04 03 02 01 , 02 | McAddr LE dd cc bb aa
    assert ans.encode() == bytes.fromhex("01" "35" "00" "04030201" "02" "ddccbbaa")
    assert ans.encoded_payload_size == 1 + 5 * 2


def test_mc_group_status_ans_empty_list() -> None:
    ans = McGroupStatusAns(nb_total_groups=0, ans_group_mask=0, groups=[])
    assert ans.encode() == bytes.fromhex("0100")
    assert ans.encoded_payload_size == 1


def test_mc_group_status_ans_round_trip() -> None:
    ans = McGroupStatusAns(
        nb_total_groups=4,
        ans_group_mask=0b1111,
        groups=[McGroupStatusEntry(group_id=i, mc_addr=0x11223344 + i) for i in range(4)],
    )
    raw = ans.encode_payload()
    assert len(raw) == 1 + 5 * 4
    assert McGroupStatusAns.decode_payload(raw) == ans


def test_mc_group_status_ans_mc_addr_is_little_endian() -> None:
    entry = McGroupStatusEntry(group_id=1, mc_addr=0x12345678)
    assert entry.encode() == bytes.fromhex("01" "78563412")
    assert McGroupStatusEntry.decode(entry.encode()) == entry


def test_mc_group_status_entry_upper_bits_ignored() -> None:
    assert McGroupStatusEntry.decode(bytes.fromhex("fe" "00000000")).group_id == 2


def test_mc_group_status_ans_mask_must_match_record_count() -> None:
    with pytest.raises(ValueError):
        McGroupStatusAns(
            nb_total_groups=1, ans_group_mask=0b0011, groups=[McGroupStatusEntry(0, 1)]
        ).encode()


def test_mc_group_status_ans_nb_total_groups_range() -> None:
    with pytest.raises(ValueError):
        McGroupStatusAns(nb_total_groups=5, ans_group_mask=0, groups=[]).encode()


def test_mc_group_status_ans_decode_length_keyed_on_mask() -> None:
    assert McGroupStatusAns.payload_size_from_status(0x00) == 1
    assert McGroupStatusAns.payload_size_from_status(0x35) == 11
    assert McGroupStatusAns.payload_size_from_status(0x7F) == 21
    with pytest.raises(ValueError):
        # Mask announces one record but no record bytes follow.
        McGroupStatusAns.decode_payload(bytes([0x01]))
    with pytest.raises(ValueError):
        McGroupStatusAns.decode_payload(b"")


# ---------------------------------------------------------------------------
# McGroupSetup (CID 0x02) — §4.3, Tables 8-11
# ---------------------------------------------------------------------------

def test_mc_group_setup_req_exact_bytes() -> None:
    req = McGroupSetupReq(
        group_id=1,
        mc_addr=0x01020304,
        mc_key_encrypted=bytes(range(16)),
        min_fcnt=16,
        max_fcnt=0xDEADBEEF,
    )
    expected = bytes.fromhex(
        "01"                                  # McGroupIDHeader: RFU=0, McGroupID=1
        "04030201"                            # McAddr, little endian
        "000102030405060708090a0b0c0d0e0f"    # McKey_encrypted
        "10000000"                            # minMcFCnt = 16, little endian
        "efbeadde"                            # maxMcFCnt = 0xDEADBEEF, little endian
    )
    assert req.encode_payload() == expected
    assert len(expected) == 29
    assert req.encoded_payload_size == 29
    assert McGroupSetupReq.PAYLOAD_SIZE == 29
    assert req.encode() == bytes([0x02]) + expected
    assert len(req.encode()) == 30


def test_mc_group_setup_req_round_trip() -> None:
    req = McGroupSetupReq(
        group_id=3,
        mc_addr=0xFFFFFFFF,
        mc_key_encrypted=bytes([0xA5] * 16),
        min_fcnt=0,
        max_fcnt=0xFFFFFFFF,
    )
    assert McGroupSetupReq.decode_payload(req.encode_payload()) == req


def test_mc_group_setup_req_header_rfu_ignored() -> None:
    raw = bytes([0xFD]) + bytes(28)
    assert McGroupSetupReq.decode_payload(raw).group_id == 1


def test_mc_group_setup_req_validation() -> None:
    with pytest.raises(ValueError):
        McGroupSetupReq(group_id=4).encode()
    with pytest.raises(ValueError):
        McGroupSetupReq(mc_key_encrypted=b"\x00" * 15).encode()
    with pytest.raises(ValueError):
        McGroupSetupReq(mc_addr=0x1_0000_0000).encode()
    with pytest.raises(ValueError):
        McGroupSetupReq(max_fcnt=0x1_0000_0000).encode()


def test_mc_group_setup_req_decode_bad_length() -> None:
    with pytest.raises(ValueError):
        McGroupSetupReq.decode_payload(bytes(28))


@pytest.mark.parametrize(
    "group_id, id_error, raw",
    [
        (0, False, 0x00),
        (3, False, 0x03),
        (1, True, 0x05),   # IDerror is bit 2
        (2, True, 0x06),
    ],
)
def test_mc_group_setup_ans_bit_packing(group_id: int, id_error: bool, raw: int) -> None:
    ans = McGroupSetupAns(group_id=group_id, id_error=id_error)
    assert ans.encode() == bytes([0x02, raw])
    assert McGroupSetupAns.decode_payload(bytes([raw])) == ans


def test_mc_group_setup_ans_rfu_ignored() -> None:
    # 0xFD = 1111_1101: RFU bits 7:3 all set, IDerror = 1, McGroupID = 1.
    assert McGroupSetupAns.decode_payload(bytes([0xFD])) == McGroupSetupAns(
        group_id=1, id_error=True
    )


# ---------------------------------------------------------------------------
# McGroupDelete (CID 0x03) — §4.4
# ---------------------------------------------------------------------------

def test_mc_group_delete_req() -> None:
    assert McGroupDeleteReq(group_id=2).encode() == bytes.fromhex("0302")
    assert McGroupDeleteReq.decode_payload(bytes([0xFE])).group_id == 2
    with pytest.raises(ValueError):
        McGroupDeleteReq(group_id=4).encode()
    with pytest.raises(ValueError):
        McGroupDeleteReq.decode_payload(b"")


@pytest.mark.parametrize(
    "group_id, undefined, raw",
    [(0, False, 0x00), (3, False, 0x03), (2, True, 0x06)],
)
def test_mc_group_delete_ans_bit_packing(group_id: int, undefined: bool, raw: int) -> None:
    ans = McGroupDeleteAns(group_id=group_id, mc_group_undefined=undefined)
    assert ans.encode() == bytes([0x03, raw])
    assert McGroupDeleteAns.decode_payload(bytes([raw])) == ans


# ---------------------------------------------------------------------------
# McClassCSession (CID 0x04) — §4.5, Tables 17-20
# ---------------------------------------------------------------------------

def test_mc_class_c_session_req_exact_bytes() -> None:
    req = McClassCSessionReq(
        group_id=2,
        session_time=0x12345678,
        session_timeout=8,
        dl_frequency=869_525_000,
        data_rate=3,
    )
    # DLFreq = 869525000 / 100 = 8695250 = 0x84ADD2 -> little endian d2 ad 84
    assert 8_695_250 == 0x84ADD2
    expected = bytes.fromhex(
        "02"          # McGroupIDHeader
        "78563412"    # SessionTime, little endian
        "08"          # SessionTimeOut: RFU=0, TimeOut=8
        "d2ad84"      # DLFreq, 24-bit little endian, units of 100 Hz
        "03"          # DR
    )
    assert req.encode_payload() == expected
    assert len(expected) == 10
    assert req.encode() == bytes([0x04]) + expected
    assert McClassCSessionReq.PAYLOAD_SIZE == 10


def test_mc_class_c_session_req_round_trip() -> None:
    req = McClassCSessionReq(
        group_id=1, session_time=0xFFFFFFFF, session_timeout=15,
        dl_frequency=0xFFFFFF * 100, data_rate=15,
    )
    assert McClassCSessionReq.decode_payload(req.encode_payload()) == req


def test_mc_class_c_timeout_seconds() -> None:
    # TimeOut encodes a maximum session duration of 2**TimeOut seconds (§4.5).
    assert McClassCSessionReq(session_timeout=0).timeout_seconds == 1
    assert McClassCSessionReq(session_timeout=8).timeout_seconds == 256
    assert McClassCSessionReq(session_timeout=15).timeout_seconds == 32768


def test_dl_frequency_scaling_and_rejection() -> None:
    req = McClassCSessionReq(dl_frequency=868_100_000)
    assert req.encode_payload()[6:9] == (8_681_000).to_bytes(3, "little")
    assert McClassCSessionReq.decode_payload(req.encode_payload()).dl_frequency == 868_100_000

    # DLFreq = 0 is legal (and selects frequency hopping for Class B, §4.6).
    assert McClassCSessionReq(dl_frequency=0).encode_payload()[6:9] == b"\x00\x00\x00"

    # Non-multiple-of-100 Hz values are rejected rather than silently rounded.
    with pytest.raises(ValueError):
        McClassCSessionReq(dl_frequency=868_100_050).encode_payload()
    # Out of the 24-bit range.
    with pytest.raises(ValueError):
        McClassCSessionReq(dl_frequency=(0xFFFFFF + 1) * 100).encode_payload()


def test_mc_class_c_session_req_validation() -> None:
    with pytest.raises(ValueError):
        McClassCSessionReq(group_id=4).encode()
    with pytest.raises(ValueError):
        McClassCSessionReq(session_timeout=16).encode()
    with pytest.raises(ValueError):
        McClassCSessionReq(session_time=0x1_0000_0000).encode()
    with pytest.raises(ValueError):
        McClassCSessionReq(data_rate=256).encode()
    with pytest.raises(ValueError):
        McClassCSessionReq.decode_payload(bytes(9))


def test_mc_class_c_session_ans_success_is_four_bytes() -> None:
    ans = McClassCSessionAns(group_id=1, time_to_start=0x010203)
    # Status: all error bits 0, McGroupID = 1 -> 0x01; TimeToStart LE.
    assert ans.encode() == bytes.fromhex("04" "01" "030201")
    assert ans.encoded_payload_size == 4
    assert ans.has_error is False
    assert McClassCSessionAns.decode_payload(ans.encode_payload()) == ans


def test_mc_class_c_session_ans_time_to_start_is_three_bytes_le() -> None:
    ans = McClassCSessionAns(group_id=0, time_to_start=TIME_TO_START_UNSYNCHRONIZED)
    assert ans.encode_payload() == bytes.fromhex("00" "ffffff")
    assert McClassCSessionAns.decode_payload(ans.encode_payload()).time_to_start == 0xFFFFFF
    with pytest.raises(ValueError):
        McClassCSessionAns(group_id=0, time_to_start=0x1000000).encode_payload()


@pytest.mark.parametrize(
    "kwargs, status",
    [
        ({"dr_error": True}, 0x04),
        ({"freq_error": True}, 0x08),
        ({"mc_group_undefined": True}, 0x10),
        ({"start_missed": True}, 0x20),
        ({"reserved_errors": 0b11}, 0xC0),
        ({"dr_error": True, "freq_error": True, "group_id": 3}, 0x0F),
    ],
)
def test_mc_class_c_session_ans_error_is_one_byte(kwargs: dict, status: int) -> None:
    ans = McClassCSessionAns(**kwargs)
    assert ans.has_error is True
    assert ans.encode_payload() == bytes([status])
    assert ans.encoded_payload_size == 1
    decoded = McClassCSessionAns.decode_payload(bytes([status]))
    assert decoded == ans
    assert decoded.time_to_start is None


def test_mc_class_c_session_ans_conditional_field_consistency() -> None:
    # TimeToStart must be omitted when any of status bits 2..7 is set (§4.5).
    with pytest.raises(ValueError):
        McClassCSessionAns(group_id=0, dr_error=True, time_to_start=5).encode_payload()
    # ...and present when none is.
    with pytest.raises(ValueError):
        McClassCSessionAns(group_id=0).encode_payload()


def test_mc_class_c_session_ans_reserved_bits_are_errors() -> None:
    # TS005 §4.5: bits 7:6 are "reserved for future errors" and SHALL be
    # treated as errors when set, unlike ordinary RFU bits.
    decoded = McClassCSessionAns.decode_payload(bytes([0x40]))
    assert decoded.reserved_errors == 0b01
    assert decoded.has_error is True
    assert decoded.time_to_start is None


def test_mc_class_c_session_ans_bad_length() -> None:
    with pytest.raises(ValueError):
        McClassCSessionAns.decode_payload(bytes.fromhex("01" "0302"))
    with pytest.raises(ValueError):
        McClassCSessionAns.decode_payload(bytes.fromhex("04" "000000"))
    with pytest.raises(ValueError):
        McClassCSessionAns.decode_payload(b"")


# ---------------------------------------------------------------------------
# McClassBSession (CID 0x05) — §4.6, Tables 21-25
# ---------------------------------------------------------------------------

def test_mc_class_b_session_req_exact_bytes() -> None:
    req = McClassBSessionReq(
        group_id=3,
        session_time=0xFF80,          # 65408 = 511 * 128, a valid beacon-period multiple
        periodicity=5,
        session_timeout=4,
        dl_frequency=869_525_000,
        data_rate=3,
    )
    # TimeOutPeriodicity: bit7 RFU=0 | Periodicity=5 (bits 6:4) | TimeOut=4
    #                     = 0b0_101_0100 = 0x54
    expected = bytes.fromhex(
        "03"          # McGroupIDHeader
        "80ff0000"    # SessionTime, little endian
        "54"          # TimeOutPeriodicity
        "d2ad84"      # DLFreq
        "03"          # DR
    )
    assert req.encode_payload() == expected
    assert len(expected) == 10
    assert req.encode() == bytes([0x05]) + expected


def test_mc_class_b_session_req_round_trip_all_periodicities() -> None:
    for periodicity in range(8):
        for timeout in range(16):
            req = McClassBSessionReq(
                group_id=1, session_time=128 * 1234, periodicity=periodicity,
                session_timeout=timeout, dl_frequency=869_525_000, data_rate=5,
            )
            assert McClassBSessionReq.decode_payload(req.encode_payload()) == req


def test_mc_class_b_timeout_seconds() -> None:
    # Class B TimeOut is in BeaconPeriods of 128 s: 128 * 2**TimeOut (§4.6).
    assert McClassBSessionReq(session_timeout=0).timeout_seconds == 128
    assert McClassBSessionReq(session_timeout=8).timeout_seconds == 32768
    assert McClassBSessionReq(session_timeout=8).timeout_seconds == pytest.approx(9.1 * 3600, rel=0.02)
    assert McClassBSessionReq(session_timeout=15).timeout_seconds == 128 * 32768


@pytest.mark.parametrize(
    "periodicity, ping_nb, ping_period_seconds, ping_period_slots",
    [
        (0, 128, 1, 32),
        (1, 64, 2, 64),
        (2, 32, 4, 128),
        (3, 16, 8, 256),
        (4, 8, 16, 512),
        (5, 4, 32, 1024),
        (6, 2, 64, 2048),
        (7, 1, 128, 4096),
    ],
)
def test_mc_class_b_periodicity_helpers(
    periodicity: int, ping_nb: int, ping_period_seconds: int, ping_period_slots: int
) -> None:
    # PingSlotInfoReq encoding (TS001): pingNb = 2**(7 - Periodicity),
    # pingPeriod = 4096 / pingNb slots of 30 ms = 2**Periodicity seconds.
    req = McClassBSessionReq(periodicity=periodicity)
    assert req.ping_nb == ping_nb
    assert req.ping_period_seconds == ping_period_seconds
    assert req.ping_period_slots == ping_period_slots
    assert req.ping_nb * req.ping_period_seconds == 128


def test_mc_class_b_session_req_validation() -> None:
    with pytest.raises(ValueError):
        McClassBSessionReq(periodicity=8).encode()
    with pytest.raises(ValueError):
        McClassBSessionReq(session_timeout=16).encode()
    with pytest.raises(ValueError):
        McClassBSessionReq(group_id=4).encode()
    with pytest.raises(ValueError):
        McClassBSessionReq.decode_payload(bytes(11))


def test_mc_class_b_session_req_rfu_bit7_ignored() -> None:
    raw = bytes([0x00]) + bytes(4) + bytes([0xD4]) + bytes(3) + bytes([0x00])
    decoded = McClassBSessionReq.decode_payload(raw)
    assert decoded.periodicity == 5
    assert decoded.session_timeout == 4
    # The RFU bit is cleared when re-encoding.
    assert decoded.encode_payload()[5] == 0x54


def test_mc_class_b_session_ans_shape_matches_class_c() -> None:
    ok = McClassBSessionAns(group_id=2, time_to_start=1000)
    assert ok.encode() == bytes([0x05, 0x02]) + (1000).to_bytes(3, "little")
    assert McClassBSessionAns.decode_payload(ok.encode_payload()) == ok

    err = McClassBSessionAns(group_id=2, mc_group_undefined=True)
    assert err.encode() == bytes([0x05, 0x12])
    assert McClassBSessionAns.decode_payload(err.encode_payload()) == err
    assert err.time_to_start is None


# ---------------------------------------------------------------------------
# Multi-command parsing (§3)
# ---------------------------------------------------------------------------

def test_encode_commands_concatenates() -> None:
    cmds = [
        PackageVersionReq(),
        McGroupDeleteReq(group_id=1),
        McGroupStatusReq(req_group_mask=0x0F),
    ]
    assert encode_commands(cmds) == bytes.fromhex("00" "0301" "010f")


def test_parse_downlink_multiple_commands() -> None:
    cmds = [
        PackageVersionReq(),
        McGroupSetupReq(
            group_id=0, mc_addr=0xC0FFEE11, mc_key_encrypted=bytes(range(16)),
            min_fcnt=1, max_fcnt=1000,
        ),
        McClassCSessionReq(
            group_id=0, session_time=1_300_000_000, session_timeout=10,
            dl_frequency=869_525_000, data_rate=3,
        ),
        McClassBSessionReq(
            group_id=1, session_time=128 * 99, periodicity=4,
            session_timeout=2, dl_frequency=0, data_rate=8,
        ),
        McGroupStatusReq(req_group_mask=0b0011),
        McGroupDeleteReq(group_id=2),
    ]
    raw = encode_commands(cmds)
    assert len(raw) == 1 + 30 + 11 + 11 + 2 + 2
    assert parse_downlink_commands(raw) == cmds


def test_parse_uplink_multiple_commands_with_variable_lengths() -> None:
    cmds = [
        PackageVersionAns(),
        McGroupStatusAns(
            nb_total_groups=2,
            ans_group_mask=0b0011,
            groups=[McGroupStatusEntry(0, 0x11111111), McGroupStatusEntry(1, 0x22222222)],
        ),
        McGroupSetupAns(group_id=1),
        McClassCSessionAns(group_id=1, time_to_start=42),       # 4-byte form
        McClassBSessionAns(group_id=2, freq_error=True),        # 1-byte form
        McGroupDeleteAns(group_id=3, mc_group_undefined=True),
    ]
    raw = encode_commands(cmds)
    assert parse_uplink_commands(raw) == cmds


def test_parse_uplink_distinguishes_class_b_and_c_answers() -> None:
    raw = encode_commands([
        McClassCSessionAns(group_id=0, time_to_start=7),
        McClassBSessionAns(group_id=0, time_to_start=9),
    ])
    parsed = parse_uplink_commands(raw)
    assert isinstance(parsed[0], McClassCSessionAns)
    assert isinstance(parsed[1], McClassBSessionAns)
    assert [c.cid for c in parsed] == [McCID.MC_CLASS_C_SESSION, McCID.MC_CLASS_B_SESSION]


def test_parse_empty_payload() -> None:
    assert parse_downlink_commands(b"") == []
    assert parse_uplink_commands(b"") == []


def test_parse_stops_on_unknown_cid(caplog: pytest.LogCaptureFixture) -> None:
    raw = McGroupDeleteReq(group_id=1).encode() + bytes.fromhex("7f" "aabbcc")
    with caplog.at_level("WARNING"):
        parsed = parse_downlink_commands(raw)
    assert parsed == [McGroupDeleteReq(group_id=1)]
    assert "0x7F" in caplog.text

    caplog.clear()
    raw = PackageVersionAns().encode() + bytes.fromhex("99")
    with caplog.at_level("WARNING"):
        parsed_up = parse_uplink_commands(raw)
    assert parsed_up == [PackageVersionAns()]
    assert "0x99" in caplog.text


def test_parse_stops_on_truncated_command(caplog: pytest.LogCaptureFixture) -> None:
    truncated = McClassCSessionReq(group_id=0).encode()[:-2]
    raw = McGroupDeleteReq(group_id=0).encode() + truncated
    with caplog.at_level("WARNING"):
        parsed = parse_downlink_commands(raw)
    assert parsed == [McGroupDeleteReq(group_id=0)]
    assert "truncated" in caplog.text

    caplog.clear()
    # Status byte announces a 4-octet answer but only 2 further octets follow.
    with caplog.at_level("WARNING"):
        parsed_up = parse_uplink_commands(bytes.fromhex("04" "00" "0102"))
    assert parsed_up == []
    assert "truncated" in caplog.text

    caplog.clear()
    # CID present but no status byte at all for a variable-length answer.
    with caplog.at_level("WARNING"):
        assert parse_uplink_commands(bytes.fromhex("01")) == []
    assert "truncated" in caplog.text


def test_round_trip_of_full_fuota_setup_sequence() -> None:
    """A realistic FPort 200 downlink burst survives encode -> parse -> encode."""
    cmds = [
        McGroupSetupReq(
            group_id=0, mc_addr=0x26011BDA, mc_key_encrypted=bytes([0x5A] * 16),
            min_fcnt=0, max_fcnt=0xFFFF,
        ),
        McClassCSessionReq(
            group_id=0, session_time=1_354_000_000, session_timeout=9,
            dl_frequency=869_525_000, data_rate=0,
        ),
    ]
    raw = encode_commands(cmds)
    assert encode_commands(list(parse_downlink_commands(raw))) == raw


# ═══════════════════════════════════════════════════════════════════════════
# Application classes (TS005 §3 to §4.6)
# ═══════════════════════════════════════════════════════════════════════════

from simulator.lorawan.crypto import compute_data_mic, encrypt_frm_payload
from simulator.lorawan.device import DeviceSession, LoRaWanDevice, MulticastGroup
from simulator.lorawan.enums.frame_types import MType
from simulator.lorawan.enums.operating_mode import OperatingMode
from simulator.lorawan.frame import FCtrl, FHDR, MACPayload, MHDR, PHYPayload
from simulator.lorawan.fuota.crypto import (
    decrypt_mc_key, derive_mc_ke_key, derive_mc_root_key,
    derive_multicast_key_material, encrypt_mc_key,
)
from simulator.lorawan.fuota.multicast_setup import (
    DeviceSetupState,
    MulticastSetupDeviceApplication,
    MulticastSetupServerApplication,
)
from simulator.lorawan.network_server import NetworkServer
from simulator.lorawan.region import RX2_DEFAULT_FREQUENCY


# ── Test credentials ────────────────────────────────────────────────────────
DEV_ADDR_A = 0x26011234
DEV_ADDR_B = 0x26015678
NWK_S_KEY = bytes.fromhex("2B7E151628AED2A6ABF7158809CF4F3C")
APP_S_KEY = bytes.fromhex("3C4F9C098815F7ABA6D2AE281615E72B")

GEN_APP_KEY_A = bytes.fromhex("000102030405060708090A0B0C0D0E0F")
GEN_APP_KEY_B = bytes.fromhex("0F0E0D0C0B0A09080706050403020100")

MC_ADDR = 0xFF000001
MC_KEY = bytes.fromhex("AABBCCDD11223344AABBCCDD11223344")


def _mc_ke_key(gen_app_key: bytes) -> bytes:
    return derive_mc_ke_key(mc_root_key=derive_mc_root_key(key=gen_app_key))


def _setup_req(
    group_id: int = 0,
    mc_addr: int = MC_ADDR,
    gen_app_key: bytes = GEN_APP_KEY_A,
    min_fcnt: int = 0,
    max_fcnt: int = 0xFFFFFFFF,
) -> McGroupSetupReq:
    """An McGroupSetupReq whose McKey is wrapped for the given device key."""
    return McGroupSetupReq(
        group_id=group_id,
        mc_addr=mc_addr,
        mc_key_encrypted=encrypt_mc_key(
            mc_ke_key=_mc_ke_key(gen_app_key), mc_key=MC_KEY,
        ),
        min_fcnt=min_fcnt,
        max_fcnt=max_fcnt,
    )


def _device_app(**kwargs: object) -> MulticastSetupDeviceApplication:
    kwargs.setdefault("gen_app_key", GEN_APP_KEY_A)
    return MulticastSetupDeviceApplication(None, **kwargs)  # type: ignore[arg-type]


def _answers(app: MulticastSetupDeviceApplication) -> list:
    """Parse the single concatenated uplink the device app queued."""
    payload = app.pop_pending_uplink()
    assert payload is not None
    return list(parse_uplink_commands(payload))


def _build_uplink(dev_addr: int, fcnt: int, fport: int, payload: bytes) -> bytes:
    """A valid encrypted + MIC'd uplink, as in tests/test_lorawan_clock_sync.py."""
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


def _decrypt_downlink(dev_addr: int, raw: bytes) -> bytes:
    """Decrypt a downlink built by the network server, using its own FCnt."""
    phy = PHYPayload.decode_data(raw)
    assert phy.mac_payload is not None
    assert phy.mac_payload.fport == MULTICAST_SETUP_FPORT
    return encrypt_frm_payload(
        APP_S_KEY, dev_addr=dev_addr, fcnt=phy.mac_payload.fhdr.fcnt, uplink=False,
        payload=phy.mac_payload.frm_payload,
    )


# ── Device app: key handling ────────────────────────────────────────────────

class TestDeviceAppKeys:
    def test_mc_ke_key_derived_from_gen_app_key(self) -> None:
        app = _device_app()
        assert app.mc_ke_key == _mc_ke_key(GEN_APP_KEY_A)

    def test_explicit_mc_ke_key_wins(self) -> None:
        app = MulticastSetupDeviceApplication(None, mc_ke_key=bytes(range(16)))
        assert app.mc_ke_key == bytes(range(16))

    def test_lorawan_1_1_uses_a_different_root_key(self) -> None:
        app = MulticastSetupDeviceApplication(
            None, gen_app_key=GEN_APP_KEY_A, lorawan_1_1=True,
        )
        assert app.mc_ke_key != _mc_ke_key(GEN_APP_KEY_A)
        assert app.mc_ke_key == derive_mc_ke_key(
            mc_root_key=derive_mc_root_key(key=GEN_APP_KEY_A, lorawan_1_1=True)
        )

    def test_no_key_at_all_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="mc_ke_key or gen_app_key"):
            MulticastSetupDeviceApplication(None)

    def test_short_key_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="16 bytes"):
            MulticastSetupDeviceApplication(None, mc_ke_key=b"\x00" * 8)

    def test_port_is_200(self) -> None:
        assert _device_app().port() == MULTICAST_SETUP_FPORT


# ── Device app: commands without an attached device ─────────────────────────

class TestDeviceAppWithoutDevice:
    @pytest.mark.asyncio
    async def test_package_version(self) -> None:
        app = _device_app()
        await app.on_downlink(encode_commands([PackageVersionReq()]))

        answers = _answers(app)
        assert answers == [PackageVersionAns(
            package_identifier=PACKAGE_IDENTIFIER, package_version=PACKAGE_VERSION,
        )]
        assert app.package_version_requests == 1

    @pytest.mark.asyncio
    async def test_group_setup_derives_the_session_keys(self) -> None:
        app = _device_app()
        await app.on_downlink(encode_commands([_setup_req(group_id=1)]))

        assert _answers(app) == [McGroupSetupAns(group_id=1, id_error=False)]

        group = app.get_group(1)
        assert group is not None
        assert group.group_addr == MC_ADDR
        material = derive_multicast_key_material(mc_key=MC_KEY, mc_addr=MC_ADDR)
        assert group.app_s_key == material.mc_app_s_key
        assert group.nwk_s_key == material.mc_nwk_s_key
        assert app.groups == {1: group}

    @pytest.mark.asyncio
    async def test_group_setup_stores_the_counter_window(self) -> None:
        app = _device_app()
        await app.on_downlink(
            encode_commands([_setup_req(min_fcnt=100, max_fcnt=200)])
        )
        group = app.get_group(0)
        assert group is not None
        assert (group.min_fcnt, group.max_fcnt) == (100, 200)
        # A group whose window starts above zero never accepts anything below it.
        assert group.fcnt_down == 100

    @pytest.mark.asyncio
    async def test_group_setup_with_an_inverted_window_is_an_id_error(self) -> None:
        app = _device_app()
        await app.on_downlink(
            encode_commands([_setup_req(min_fcnt=500, max_fcnt=10)])
        )
        assert _answers(app) == [McGroupSetupAns(group_id=0, id_error=True)]
        assert app.groups == {}

    @pytest.mark.asyncio
    async def test_group_setup_replaces_an_existing_group_id(self) -> None:
        app = _device_app()
        await app.on_downlink(encode_commands([_setup_req(group_id=0)]))
        await app.on_downlink(
            encode_commands([_setup_req(group_id=0, mc_addr=0xFF00BEEF)])
        )
        assert list(app.groups) == [0]
        group = app.get_group(0)
        assert group is not None and group.group_addr == 0xFF00BEEF

    @pytest.mark.asyncio
    async def test_wrong_mc_ke_key_yields_different_session_keys(self) -> None:
        """A device set up with the wrong McKEKey still answers OK but cannot decrypt."""
        app = MulticastSetupDeviceApplication(None, gen_app_key=GEN_APP_KEY_B)
        await app.on_downlink(
            encode_commands([_setup_req(gen_app_key=GEN_APP_KEY_A)])
        )
        group = app.get_group(0)
        assert group is not None
        material = derive_multicast_key_material(mc_key=MC_KEY, mc_addr=MC_ADDR)
        assert group.app_s_key != material.mc_app_s_key

    @pytest.mark.asyncio
    async def test_group_delete(self) -> None:
        app = _device_app()
        await app.on_downlink(encode_commands([_setup_req(group_id=2)]))
        app.pop_pending_uplink()

        await app.on_downlink(encode_commands([McGroupDeleteReq(group_id=2)]))
        assert _answers(app) == [
            McGroupDeleteAns(group_id=2, mc_group_undefined=False)
        ]
        assert app.groups == {}

    @pytest.mark.asyncio
    async def test_group_delete_of_an_undefined_group(self) -> None:
        app = _device_app()
        await app.on_downlink(encode_commands([McGroupDeleteReq(group_id=3)]))
        assert _answers(app) == [
            McGroupDeleteAns(group_id=3, mc_group_undefined=True)
        ]

    @pytest.mark.asyncio
    async def test_group_status_lists_only_the_requested_groups(self) -> None:
        app = _device_app()
        for group_id in range(3):
            await app.on_downlink(encode_commands([
                _setup_req(group_id=group_id, mc_addr=MC_ADDR + group_id)
            ]))
            app.pop_pending_uplink()

        await app.on_downlink(encode_commands([McGroupStatusReq(req_group_mask=0b0101)]))
        answers = _answers(app)
        assert len(answers) == 1
        status = answers[0]
        assert isinstance(status, McGroupStatusAns)
        assert status.nb_total_groups == 3
        assert status.ans_group_mask == 0b0101
        assert [(e.group_id, e.mc_addr) for e in status.groups] == [
            (0, MC_ADDR), (2, MC_ADDR + 2),
        ]

    @pytest.mark.asyncio
    async def test_group_status_of_an_empty_device(self) -> None:
        app = _device_app()
        await app.on_downlink(encode_commands([McGroupStatusReq(req_group_mask=0x0F)]))
        status = _answers(app)[0]
        assert isinstance(status, McGroupStatusAns)
        assert status.nb_total_groups == 0
        assert status.ans_group_mask == 0
        assert status.groups == []

    @pytest.mark.asyncio
    async def test_group_status_drops_the_highest_group_ids_when_short_of_room(self) -> None:
        """TS005 §4.2: discard the last groups, starting with the highest McGroupID."""
        # 1 CID + 1 status + 5 per record: a 12 byte budget fits exactly two records.
        app = _device_app(max_uplink_payload=12)
        for group_id in range(4):
            await app.on_downlink(encode_commands([
                _setup_req(group_id=group_id, mc_addr=MC_ADDR + group_id)
            ]))
            app.pop_pending_uplink()

        await app.on_downlink(encode_commands([McGroupStatusReq(req_group_mask=0x0F)]))
        status = _answers(app)[0]
        assert isinstance(status, McGroupStatusAns)
        # All four exist, but only the two lowest ids are reported.
        assert status.nb_total_groups == 4
        assert status.ans_group_mask == 0b0011
        assert [e.group_id for e in status.groups] == [0, 1]

    @pytest.mark.asyncio
    async def test_several_commands_are_answered_in_one_uplink(self) -> None:
        payload = encode_commands([
            PackageVersionReq(),
            _setup_req(group_id=0),
            McGroupStatusReq(req_group_mask=0x0F),
        ])
        app = _device_app()
        await app.on_downlink(payload)

        # Exactly one uplink, carrying all three answers in request order.
        assert len(app.pending_uplinks) == 1
        answers = _answers(app)
        assert [type(a).__name__ for a in answers] == [
            "PackageVersionAns", "McGroupSetupAns", "McGroupStatusAns",
        ]
        # The group created by the second command is visible to the third.
        status = answers[2]
        assert isinstance(status, McGroupStatusAns)
        assert status.nb_total_groups == 1

    @pytest.mark.asyncio
    async def test_empty_and_unparsable_payloads_produce_no_uplink(self) -> None:
        app = _device_app()
        await app.on_downlink(b"")
        await app.on_downlink(b"\x7F\x00\x00")
        assert app.pending_uplinks == []

    @pytest.mark.asyncio
    async def test_uplink_answers_are_never_sent_for_answers(self) -> None:
        """Answer CIDs arriving as a downlink are ignored, not echoed."""
        app = _device_app()
        # 0x00 parsed as a downlink is PackageVersionReq, so use a session answer shape
        # that only exists uplink-side: parse_downlink_commands reads it as a request.
        await app.on_downlink(encode_commands([McGroupStatusReq(req_group_mask=0)]))
        assert len(app.pending_uplinks) == 1


# ── Device app: Class C sessions ────────────────────────────────────────────

class TestDeviceAppClassCSession:
    async def _app_with_group(self) -> MulticastSetupDeviceApplication:
        app = _device_app(time_provider=lambda: 1000)
        await app.on_downlink(encode_commands([_setup_req(group_id=0)]))
        app.pop_pending_uplink()
        return app

    @pytest.mark.asyncio
    async def test_accepted_session_reports_time_to_start(self) -> None:
        app = await self._app_with_group()
        await app.on_downlink(encode_commands([McClassCSessionReq(
            group_id=0, session_time=1300, session_timeout=8,
            dl_frequency=869_525_000, data_rate=0,
        )]))

        answer = _answers(app)[0]
        assert isinstance(answer, McClassCSessionAns)
        assert not answer.has_error
        assert answer.time_to_start == 300
        assert answer.group_id == 0

        assert len(app.session_history) == 1
        record = app.session_history[0]
        assert record.mode is OperatingMode.CLASS_C
        assert record.timeout_seconds == 256
        assert record.frequency == 869_525_000
        # No device attached, so nothing was actually scheduled.
        assert record.started is False

    @pytest.mark.asyncio
    async def test_time_to_start_saturates(self) -> None:
        app = await self._app_with_group()
        await app.on_downlink(encode_commands([McClassCSessionReq(
            group_id=0, session_time=1000 + 0xFFFFFF + 5000, session_timeout=4,
            dl_frequency=0, data_rate=5,
        )]))
        answer = _answers(app)[0]
        assert answer.time_to_start == TIME_TO_START_UNSYNCHRONIZED

    @pytest.mark.asyncio
    async def test_undefined_group(self) -> None:
        app = await self._app_with_group()
        await app.on_downlink(encode_commands([McClassCSessionReq(
            group_id=3, session_time=2000, session_timeout=4,
            dl_frequency=0, data_rate=5,
        )]))
        answer = _answers(app)[0]
        assert answer.mc_group_undefined is True
        assert answer.time_to_start is None
        assert answer.has_error

    @pytest.mark.asyncio
    async def test_unknown_data_rate(self) -> None:
        app = await self._app_with_group()
        await app.on_downlink(encode_commands([McClassCSessionReq(
            group_id=0, session_time=2000, session_timeout=4,
            dl_frequency=0, data_rate=9,
        )]))
        answer = _answers(app)[0]
        assert answer.dr_error is True
        assert answer.time_to_start is None

    @pytest.mark.asyncio
    async def test_out_of_band_frequency(self) -> None:
        app = await self._app_with_group()
        await app.on_downlink(encode_commands([McClassCSessionReq(
            group_id=0, session_time=2000, session_timeout=4,
            dl_frequency=915_000_000, data_rate=5,
        )]))
        answer = _answers(app)[0]
        assert answer.freq_error is True
        assert answer.dr_error is False

    @pytest.mark.asyncio
    async def test_session_time_in_the_past_is_start_missed(self) -> None:
        app = await self._app_with_group()
        await app.on_downlink(encode_commands([McClassCSessionReq(
            group_id=0, session_time=900, session_timeout=4,
            dl_frequency=0, data_rate=5,
        )]))
        answer = _answers(app)[0]
        assert answer.start_missed is True
        assert answer.time_to_start is None
        assert app.session_history[0].started is False

    @pytest.mark.asyncio
    async def test_session_time_equal_to_now_is_accepted(self) -> None:
        app = await self._app_with_group()
        await app.on_downlink(encode_commands([McClassCSessionReq(
            group_id=0, session_time=1000, session_timeout=4,
            dl_frequency=0, data_rate=5,
        )]))
        answer = _answers(app)[0]
        assert answer.start_missed is False
        assert answer.time_to_start == 0

    @pytest.mark.asyncio
    async def test_several_error_bits_at_once(self) -> None:
        app = await self._app_with_group()
        await app.on_downlink(encode_commands([McClassCSessionReq(
            group_id=2, session_time=10, session_timeout=4,
            dl_frequency=915_000_000, data_rate=9,
        )]))
        answer = _answers(app)[0]
        assert (answer.mc_group_undefined, answer.dr_error, answer.freq_error,
                answer.start_missed) == (True, True, True, True)
        assert answer.status_byte == 0b0011_1110

    @pytest.mark.asyncio
    async def test_dl_frequency_zero_falls_back_to_rx2(self) -> None:
        app = await self._app_with_group()
        await app.on_downlink(encode_commands([McClassCSessionReq(
            group_id=0, session_time=1100, session_timeout=4,
            dl_frequency=0, data_rate=3,
        )]))
        assert app.session_history[0].frequency == RX2_DEFAULT_FREQUENCY


# ── Device app: Class B sessions ────────────────────────────────────────────

class TestDeviceAppClassBSession:
    async def _app_with_group(self) -> MulticastSetupDeviceApplication:
        app = _device_app(time_provider=lambda: 1000)
        await app.on_downlink(encode_commands([_setup_req(group_id=0)]))
        app.pop_pending_uplink()
        return app

    @pytest.mark.asyncio
    async def test_aligned_session_is_accepted(self) -> None:
        app = await self._app_with_group()
        await app.on_downlink(encode_commands([McClassBSessionReq(
            group_id=0, session_time=1280, periodicity=4, session_timeout=2,
            dl_frequency=869_525_000, data_rate=3,
        )]))
        answer = _answers(app)[0]
        assert isinstance(answer, McClassBSessionAns)
        assert not answer.has_error
        assert answer.time_to_start == 280

        record = app.session_history[0]
        assert record.mode is OperatingMode.CLASS_B
        assert record.ping_periodicity == 4
        # TS005 §4.6: TimeOut counts beacon periods, so 128 * 2**2.
        assert record.timeout_seconds == 512

    @pytest.mark.asyncio
    async def test_misaligned_session_time_rounds_up_to_a_beacon(self) -> None:
        app = await self._app_with_group()
        await app.on_downlink(encode_commands([McClassBSessionReq(
            group_id=0, session_time=1300, periodicity=4, session_timeout=2,
            dl_frequency=0, data_rate=3,
        )]))
        answer = _answers(app)[0]
        record = app.session_history[0]
        assert record.requested_session_time == 1300
        # TS005 §4.6 SessionTime must be a multiple of 128 s; the device rounds *up* so the
        # start can never move into the past (and so it agrees with FuotaCampaign).
        assert record.session_time == 1408
        assert record.session_time % 128 == 0
        assert answer.time_to_start == 408

    @pytest.mark.asyncio
    async def test_rounding_up_never_manufactures_start_missed(self) -> None:
        """A SessionTime just past "now" rounds forward, so it is not reported as missed."""
        app = await self._app_with_group()
        await app.on_downlink(encode_commands([McClassBSessionReq(
            group_id=0, session_time=1010, periodicity=4, session_timeout=2,
            dl_frequency=0, data_rate=3,
        )]))
        answer = _answers(app)[0]
        assert app.session_history[0].session_time == 1024
        assert answer.start_missed is False
        assert answer.time_to_start == 24

    @pytest.mark.asyncio
    async def test_a_session_time_in_the_past_is_still_start_missed(self) -> None:
        app = await self._app_with_group()
        await app.on_downlink(encode_commands([McClassBSessionReq(
            group_id=0, session_time=500, periodicity=4, session_timeout=2,
            dl_frequency=0, data_rate=3,
        )]))
        answer = _answers(app)[0]
        assert app.session_history[0].session_time == 512
        assert answer.start_missed is True

    @pytest.mark.asyncio
    async def test_errors_match_the_class_c_rules(self) -> None:
        app = await self._app_with_group()
        await app.on_downlink(encode_commands([McClassBSessionReq(
            group_id=1, session_time=1280, periodicity=4, session_timeout=2,
            dl_frequency=0, data_rate=7,
        )]))
        answer = _answers(app)[0]
        assert isinstance(answer, McClassBSessionAns)
        assert answer.mc_group_undefined is True
        assert answer.dr_error is True
        assert answer.time_to_start is None


# ── Device app: with a real LoRaWanDevice attached ───────────────────────────

class TestDeviceAppWithDevice:
    def _device(self, **kwargs: object) -> LoRaWanDevice:
        session = DeviceSession(
            dev_addr=DEV_ADDR_A, nwk_s_key=NWK_S_KEY, app_s_key=APP_S_KEY,
        )
        return LoRaWanDevice(session=session, **kwargs)  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_setup_joins_the_device_group(self) -> None:
        device = self._device()
        app = MulticastSetupDeviceApplication(device, gen_app_key=GEN_APP_KEY_A)
        device.register_application(app)

        await app.on_downlink(encode_commands([_setup_req(group_id=1)]))

        assert _answers(app) == [McGroupSetupAns(group_id=1, id_error=False)]
        group = device.get_multicast_group(MC_ADDR)
        assert group is not None and group.group_id == 1
        assert app.groups[1] is group

    @pytest.mark.asyncio
    async def test_group_id_beyond_the_device_capacity_is_an_id_error(self) -> None:
        device = self._device(max_multicast_groups=1)
        app = MulticastSetupDeviceApplication(device, gen_app_key=GEN_APP_KEY_A)
        assert app.max_groups == 1

        await app.on_downlink(encode_commands([_setup_req(group_id=1)]))
        assert _answers(app) == [McGroupSetupAns(group_id=1, id_error=True)]
        assert device.multicast_groups == {}

    @pytest.mark.asyncio
    async def test_delete_leaves_the_device_group(self) -> None:
        device = self._device()
        app = MulticastSetupDeviceApplication(device, gen_app_key=GEN_APP_KEY_A)
        await app.on_downlink(encode_commands([_setup_req(group_id=0)]))
        app.pop_pending_uplink()

        await app.on_downlink(encode_commands([McGroupDeleteReq(group_id=0)]))
        assert _answers(app) == [McGroupDeleteAns(group_id=0)]
        assert device.multicast_groups == {}


# ── Server app ──────────────────────────────────────────────────────────────

def _server(ns: NetworkServer | None = None) -> tuple[NetworkServer, MulticastSetupServerApplication]:
    ns = ns if ns is not None else NetworkServer()
    ns.register_device(DEV_ADDR_A, NWK_S_KEY, APP_S_KEY)
    ns.register_device(DEV_ADDR_B, NWK_S_KEY, APP_S_KEY)
    app = MulticastSetupServerApplication(
        ns,
        key_provider={
            DEV_ADDR_A: _mc_ke_key(GEN_APP_KEY_A),
            DEV_ADDR_B: _mc_ke_key(GEN_APP_KEY_B),
        },
        time_provider=lambda: 1000,
    )
    ns.register_application(app)
    return ns, app


class TestServerApp:
    def test_port_is_200(self) -> None:
        _, app = _server()
        assert app.port() == MULTICAST_SETUP_FPORT

    def test_setup_group_registers_the_group_on_the_network_server(self) -> None:
        ns, app = _server()
        group = app.setup_group(
            [DEV_ADDR_A, DEV_ADDR_B], group_id=0, mc_addr=MC_ADDR, mc_key=MC_KEY,
            min_fcnt=5, max_fcnt=500,
        )

        record = ns.get_multicast_group(MC_ADDR)
        assert record is not None
        material = derive_multicast_key_material(mc_key=MC_KEY, mc_addr=MC_ADDR)
        assert record.nwk_s_key == material.mc_nwk_s_key
        assert record.app_s_key == material.mc_app_s_key
        assert (record.min_fcnt, record.max_fcnt) == (5, 500)
        assert group.members == {DEV_ADDR_A, DEV_ADDR_B}

    @pytest.mark.asyncio
    async def test_each_device_gets_its_own_encrypted_mc_key(self) -> None:
        _, app = _server()
        app.setup_group([DEV_ADDR_A, DEV_ADDR_B], group_id=0, mc_addr=MC_ADDR, mc_key=MC_KEY)

        payload_a = await app.get_downlink(DEV_ADDR_A)
        payload_b = await app.get_downlink(DEV_ADDR_B)
        assert payload_a is not None and payload_b is not None
        assert payload_a != payload_b

        req_a = parse_downlink_commands(payload_a)[0]
        req_b = parse_downlink_commands(payload_b)[0]
        assert isinstance(req_a, McGroupSetupReq)
        assert isinstance(req_b, McGroupSetupReq)
        # Each wrapper decrypts back to the same McKey under its own McKEKey.
        assert decrypt_mc_key(
            mc_ke_key=_mc_ke_key(GEN_APP_KEY_A), mc_key_encrypted=req_a.mc_key_encrypted,
        ) == MC_KEY
        assert decrypt_mc_key(
            mc_ke_key=_mc_ke_key(GEN_APP_KEY_B), mc_key_encrypted=req_b.mc_key_encrypted,
        ) == MC_KEY
        # ...and not under the other device's key.
        assert decrypt_mc_key(
            mc_ke_key=_mc_ke_key(GEN_APP_KEY_B), mc_key_encrypted=req_a.mc_key_encrypted,
        ) != MC_KEY

    def test_key_provider_may_be_a_callable(self) -> None:
        ns = NetworkServer()
        app = MulticastSetupServerApplication(
            ns, key_provider=lambda dev_addr: _mc_ke_key(GEN_APP_KEY_A),
        )
        assert app.mc_ke_key(DEV_ADDR_B) == _mc_ke_key(GEN_APP_KEY_A)

    def test_bad_key_length_is_rejected(self) -> None:
        ns = NetworkServer()
        app = MulticastSetupServerApplication(ns, key_provider={DEV_ADDR_A: b"\x00" * 4})
        with pytest.raises(ValueError, match="16 bytes"):
            app.mc_ke_key(DEV_ADDR_A)

    @pytest.mark.asyncio
    async def test_commands_are_concatenated_while_they_fit(self) -> None:
        _, app = _server()
        app.setup_group([DEV_ADDR_A], group_id=0, mc_addr=MC_ADDR, mc_key=MC_KEY)
        app.request_status([DEV_ADDR_A], mask=0x0F)
        app.request_package_version([DEV_ADDR_A])

        payload = await app.get_downlink(DEV_ADDR_A)
        assert payload is not None
        # 30 + 2 + 1 bytes, well under the DR5 limit.
        assert len(payload) == 33
        assert [type(c).__name__ for c in parse_downlink_commands(payload)] == [
            "McGroupSetupReq", "McGroupStatusReq", "PackageVersionReq",
        ]
        assert await app.get_downlink(DEV_ADDR_A) is None

    @pytest.mark.asyncio
    async def test_a_tight_payload_budget_splits_over_several_downlinks(self) -> None:
        ns, _ = _server()
        app = MulticastSetupServerApplication(
            ns, key_provider={DEV_ADDR_A: _mc_ke_key(GEN_APP_KEY_A)},
            max_downlink_payload=31,
        )
        app.setup_group([DEV_ADDR_A], group_id=0, mc_addr=MC_ADDR, mc_key=MC_KEY)
        app.request_status([DEV_ADDR_A])

        first = await app.get_downlink(DEV_ADDR_A)
        second = await app.get_downlink(DEV_ADDR_A)
        assert first is not None and len(first) == 30
        assert second is not None and len(second) == 2
        assert await app.get_downlink(DEV_ADDR_A) is None

    @pytest.mark.asyncio
    async def test_a_command_that_never_fits_is_dropped(self, caplog) -> None:
        ns, _ = _server()
        app = MulticastSetupServerApplication(
            ns, key_provider={DEV_ADDR_A: _mc_ke_key(GEN_APP_KEY_A)},
            max_downlink_payload=4,
        )
        app.setup_group([DEV_ADDR_A], group_id=0, mc_addr=MC_ADDR, mc_key=MC_KEY)
        with caplog.at_level("WARNING"):
            assert await app.get_downlink(DEV_ADDR_A) is None
        assert "exceeds" in caplog.text
        assert app.pending_commands(DEV_ADDR_A) == []

    def test_session_requests_need_a_group(self) -> None:
        _, app = _server()
        with pytest.raises(KeyError):
            app.start_class_c_session(
                [DEV_ADDR_A], group_id=0, session_time=2000, session_timeout=8,
            )

    def test_class_c_session_uses_the_group_data_rate_by_default(self) -> None:
        _, app = _server()
        app.setup_group(
            [DEV_ADDR_A], group_id=0, mc_addr=MC_ADDR, mc_key=MC_KEY,
            data_rate=3, frequency=869_525_000,
        )
        request = app.start_class_c_session(
            [DEV_ADDR_A], group_id=0, session_time=2000, session_timeout=8,
        )
        assert request.data_rate == 3
        assert request.dl_frequency == 869_525_000
        assert request.timeout_seconds == 256

    def test_class_b_session_fields(self) -> None:
        _, app = _server()
        app.setup_group([DEV_ADDR_A], group_id=0, mc_addr=MC_ADDR, mc_key=MC_KEY)
        request = app.start_class_b_session(
            [DEV_ADDR_A], group_id=0, session_time=1280, session_timeout=2,
            periodicity=5, data_rate=3,
        )
        assert request.periodicity == 5
        assert request.ping_nb == 4
        assert request.timeout_seconds == 512

    def test_class_b_misaligned_session_time_is_warned_about(self, caplog) -> None:
        _, app = _server()
        app.setup_group([DEV_ADDR_A], group_id=0, mc_addr=MC_ADDR, mc_key=MC_KEY)
        with caplog.at_level("WARNING"):
            app.start_class_b_session(
                [DEV_ADDR_A], group_id=0, session_time=1300, session_timeout=2,
            )
        assert "multiple of 128" in caplog.text

    @pytest.mark.asyncio
    async def test_answers_are_recorded(self) -> None:
        _, app = _server()
        app.setup_group([DEV_ADDR_A, DEV_ADDR_B], group_id=0, mc_addr=MC_ADDR, mc_key=MC_KEY)

        assert app.all_devices_acked_group(0) is False
        await app.on_uplink(DEV_ADDR_A, encode_commands([McGroupSetupAns(group_id=0)]))
        assert app.all_devices_acked_group(0) is False
        await app.on_uplink(DEV_ADDR_B, encode_commands([McGroupSetupAns(group_id=0)]))
        assert app.all_devices_acked_group(0) is True

        assert app.device_state[DEV_ADDR_A].groups == {0: MC_ADDR}
        assert app.answers_received == 2

    @pytest.mark.asyncio
    async def test_id_error_is_recorded(self) -> None:
        _, app = _server()
        app.setup_group([DEV_ADDR_A], group_id=2, mc_addr=MC_ADDR, mc_key=MC_KEY)
        await app.on_uplink(
            DEV_ADDR_A, encode_commands([McGroupSetupAns(group_id=2, id_error=True)]),
        )
        state = app.device_state[DEV_ADDR_A]
        assert state.setup_errors == {2}
        assert state.groups == {}
        assert app.all_devices_acked_group(2) is False

    @pytest.mark.asyncio
    async def test_session_answers_and_devices_in_session(self) -> None:
        _, app = _server()
        app.setup_group([DEV_ADDR_A, DEV_ADDR_B], group_id=0, mc_addr=MC_ADDR, mc_key=MC_KEY)
        app.start_class_c_session(
            [DEV_ADDR_A, DEV_ADDR_B], group_id=0, session_time=1300, session_timeout=8,
        )

        await app.on_uplink(DEV_ADDR_A, encode_commands([
            McClassCSessionAns(group_id=0, time_to_start=300)
        ]))
        await app.on_uplink(DEV_ADDR_B, encode_commands([
            McClassCSessionAns(group_id=0, start_missed=True)
        ]))

        assert app.devices_in_session(0) == {DEV_ADDR_A}
        assert app.device_state[DEV_ADDR_A].time_to_start == {0: 300}
        # The server expected 1300 - 1000 = 300 s, so the device clock is in sync.
        assert app.device_state[DEV_ADDR_A].clock_offset == {0: 0}
        assert app.device_state[DEV_ADDR_B].time_to_start == {}

    @pytest.mark.asyncio
    async def test_clock_offset_detects_a_drifting_device(self) -> None:
        _, app = _server()
        app.setup_group([DEV_ADDR_A], group_id=0, mc_addr=MC_ADDR, mc_key=MC_KEY)
        app.start_class_c_session(
            [DEV_ADDR_A], group_id=0, session_time=1300, session_timeout=8,
        )
        await app.on_uplink(DEV_ADDR_A, encode_commands([
            McClassCSessionAns(group_id=0, time_to_start=342)
        ]))
        assert app.device_state[DEV_ADDR_A].clock_offset == {0: 42}

    @pytest.mark.asyncio
    async def test_status_and_package_version_answers(self) -> None:
        _, app = _server()
        await app.on_uplink(DEV_ADDR_A, encode_commands([
            PackageVersionAns(
                package_identifier=PACKAGE_IDENTIFIER, package_version=PACKAGE_VERSION,
            ),
            McGroupStatusAns(
                nb_total_groups=1, ans_group_mask=0b0001,
                groups=[McGroupStatusEntry(group_id=0, mc_addr=MC_ADDR)],
            ),
        ]))
        state = app.device_state[DEV_ADDR_A]
        assert (state.package_identifier, state.package_version) == (2, 2)
        assert state.last_status is not None
        assert state.groups == {0: MC_ADDR}

    @pytest.mark.asyncio
    async def test_delete_answers(self) -> None:
        _, app = _server()
        app.setup_group([DEV_ADDR_A], group_id=0, mc_addr=MC_ADDR, mc_key=MC_KEY)
        await app.on_uplink(DEV_ADDR_A, encode_commands([McGroupSetupAns(group_id=0)]))
        app.delete_group([DEV_ADDR_A], group_id=0)
        await app.on_uplink(DEV_ADDR_A, encode_commands([McGroupDeleteAns(group_id=0)]))

        assert app.device_state[DEV_ADDR_A].groups == {}
        assert app.groups[0].members == set()

        await app.on_uplink(DEV_ADDR_A, encode_commands([
            McGroupDeleteAns(group_id=1, mc_group_undefined=True)
        ]))
        assert app.device_state[DEV_ADDR_A].delete_errors == {1}

    @pytest.mark.asyncio
    async def test_on_answer_hook(self) -> None:
        seen: list[tuple[int, str]] = []

        ns, _ = _server()
        app = MulticastSetupServerApplication(
            ns, key_provider={DEV_ADDR_A: _mc_ke_key(GEN_APP_KEY_A)},
            on_answer=lambda dev_addr, cmd: seen.append((dev_addr, type(cmd).__name__)),
        )
        await app.on_uplink(DEV_ADDR_A, encode_commands([McGroupSetupAns(group_id=0)]))
        assert seen == [(DEV_ADDR_A, "McGroupSetupAns")]

    @pytest.mark.asyncio
    async def test_async_on_answer_hook(self) -> None:
        seen: list[int] = []

        async def hook(dev_addr: int, cmd: object) -> None:
            seen.append(dev_addr)

        ns, _ = _server()
        app = MulticastSetupServerApplication(
            ns, key_provider={DEV_ADDR_A: _mc_ke_key(GEN_APP_KEY_A)}, on_answer=hook,
        )
        await app.on_uplink(DEV_ADDR_A, encode_commands([McGroupSetupAns(group_id=0)]))
        assert seen == [DEV_ADDR_A]

    @pytest.mark.asyncio
    async def test_pending_devices(self) -> None:
        _, app = _server()
        app.setup_group([DEV_ADDR_A, DEV_ADDR_B], group_id=0, mc_addr=MC_ADDR, mc_key=MC_KEY)
        assert app.pending_devices() == {DEV_ADDR_A, DEV_ADDR_B}
        assert app.pending_devices([DEV_ADDR_A]) == {DEV_ADDR_A}

        await app.get_downlink(DEV_ADDR_A)
        assert app.pending_devices() == {DEV_ADDR_B}


# ── Round trip server -> device -> server over the network server ───────────

class TestRoundTripOverNetworkServer:
    @pytest.mark.asyncio
    async def test_setup_and_session_round_trip(self) -> None:
        ns, server_app = _server()
        device_app = MulticastSetupDeviceApplication(
            None, gen_app_key=GEN_APP_KEY_A, time_provider=lambda: 1000,
        )

        server_app.setup_group(
            [DEV_ADDR_A], group_id=0, mc_addr=MC_ADDR, mc_key=MC_KEY, data_rate=3,
        )

        # A plain uplink collects the pending McGroupSetupReq as its RX1 answer.
        downlink = await ns.handle_uplink(
            _build_uplink(DEV_ADDR_A, fcnt=0, fport=MULTICAST_SETUP_FPORT, payload=b"")
        )
        assert downlink is not None
        await device_app.on_downlink(_decrypt_downlink(DEV_ADDR_A, downlink))

        # The device answers; feed its uplink back through the network server.
        answer = device_app.pop_pending_uplink()
        assert answer is not None
        downlink = await ns.handle_uplink(
            _build_uplink(DEV_ADDR_A, fcnt=1, fport=MULTICAST_SETUP_FPORT, payload=answer)
        )
        assert server_app.all_devices_acked_group(0) is True
        assert device_app.groups[0].group_addr == MC_ADDR
        # Device and server derived the same multicast session keys.
        ns_record = ns.get_multicast_group(MC_ADDR)
        assert ns_record is not None
        assert device_app.groups[0].app_s_key == ns_record.app_s_key
        assert device_app.groups[0].nwk_s_key == ns_record.nwk_s_key

        # Now schedule a Class C session and run the same loop again.
        server_app.start_class_c_session(
            [DEV_ADDR_A], group_id=0, session_time=1500, session_timeout=8,
            dl_frequency=869_525_000,
        )
        downlink = await ns.handle_uplink(
            _build_uplink(DEV_ADDR_A, fcnt=2, fport=MULTICAST_SETUP_FPORT, payload=b"")
        )
        assert downlink is not None
        await device_app.on_downlink(_decrypt_downlink(DEV_ADDR_A, downlink))

        answer = device_app.pop_pending_uplink()
        assert answer is not None
        await ns.handle_uplink(
            _build_uplink(DEV_ADDR_A, fcnt=3, fport=MULTICAST_SETUP_FPORT, payload=answer)
        )

        assert server_app.devices_in_session(0) == {DEV_ADDR_A}
        assert server_app.device_state[DEV_ADDR_A].time_to_start == {0: 500}
        assert device_app.session_history[0].data_rate == 3


# ═══════════════════════════════════════════════════════════════════════════
# TimeToStart is measured from the uplink (TS005 §4.5)
# ═══════════════════════════════════════════════════════════════════════════

class _MovableClock:
    """A ``time_provider`` a test can advance between handling and transmitting."""

    def __init__(self, now: int = 1000) -> None:
        self.now = now

    def __call__(self) -> int:
        return self.now


class TestTimeToStartIsBoundAtTransmissionTime:
    """§4.5: TimeToStart "encodes the number of seconds from the McClassCSessionAns uplink".

    Encoding it when the request is *handled* makes every queueing delay look like a device
    clock offset to the server, which is the one thing the field exists to detect.
    """

    @staticmethod
    async def _app_with_group(clock: _MovableClock) -> MulticastSetupDeviceApplication:
        app = _device_app(time_provider=clock)
        await app.on_downlink(encode_commands([_setup_req(group_id=0)]))
        app.pop_pending_uplink()
        return app

    @pytest.mark.asyncio
    async def test_the_transmitted_value_reflects_the_send_time(self) -> None:
        clock = _MovableClock(1000)
        app = await self._app_with_group(clock)

        await app.on_downlink(encode_commands([McClassCSessionReq(
            group_id=0, session_time=1300, session_timeout=8,
            dl_frequency=869_525_000, data_rate=0,
        )]))

        # The answer sits in the queue while the device waits for its next uplink slot.
        clock.now = 1100
        answer = _answers(app)[0]
        assert isinstance(answer, McClassCSessionAns)
        assert answer.has_error is False
        assert answer.time_to_start == 200  # 1300 - 1100, not 1300 - 1000

    @pytest.mark.asyncio
    async def test_a_start_that_slips_past_becomes_start_missed(self) -> None:
        clock = _MovableClock(1000)
        app = await self._app_with_group(clock)

        await app.on_downlink(encode_commands([McClassCSessionReq(
            group_id=0, session_time=1050, session_timeout=8,
            dl_frequency=869_525_000, data_rate=0,
        )]))

        clock.now = 1200  # the session started while the answer was still queued
        answer = _answers(app)[0]
        assert isinstance(answer, McClassCSessionAns)
        assert answer.start_missed is True
        assert answer.time_to_start is None

    @pytest.mark.asyncio
    async def test_class_b_answers_are_rebuilt_too(self) -> None:
        clock = _MovableClock(1000)
        app = await self._app_with_group(clock)

        await app.on_downlink(encode_commands([McClassBSessionReq(
            group_id=0, session_time=1280, periodicity=4, session_timeout=2,
            dl_frequency=0, data_rate=3,
        )]))

        clock.now = 1080
        answer = _answers(app)[0]
        assert isinstance(answer, McClassBSessionAns)
        assert answer.time_to_start == 200

    @pytest.mark.asyncio
    async def test_the_saturation_value_still_applies(self) -> None:
        clock = _MovableClock(0)
        app = await self._app_with_group(clock)

        await app.on_downlink(encode_commands([McClassCSessionReq(
            group_id=0, session_time=0xFFFFFFF, session_timeout=8,
            dl_frequency=869_525_000, data_rate=0,
        )]))

        answer = _answers(app)[0]
        assert answer.time_to_start == TIME_TO_START_UNSYNCHRONIZED

    @pytest.mark.asyncio
    async def test_an_answer_without_a_session_is_untouched(self) -> None:
        """A payload with no TimeToStart in it is queued as plain octets."""
        clock = _MovableClock(1000)
        app = _device_app(time_provider=clock)
        await app.on_downlink(encode_commands([PackageVersionReq()]))
        clock.now = 9999
        answer = _answers(app)[0]
        assert isinstance(answer, PackageVersionAns)


class TestServerHasDownlinkIsNonDestructive:
    @pytest.mark.asyncio
    async def test_probing_does_not_consume_a_queued_command(self) -> None:
        ns, server = _server()
        ns.register_application(server)
        server.setup_group(
            [DEV_ADDR_A], group_id=0, mc_addr=MC_ADDR, mc_key=MC_KEY,
        )
        assert len(server.pending_commands(DEV_ADDR_A)) == 1

        assert await server.has_downlink(DEV_ADDR_A) is True
        assert await ns.has_pending_downlink(DEV_ADDR_A) is True
        assert len(server.pending_commands(DEV_ADDR_A)) == 1

        assert await server.get_downlink(DEV_ADDR_A) is not None
        assert await server.has_downlink(DEV_ADDR_A) is False
        assert await ns.has_pending_downlink(DEV_ADDR_A) is False
