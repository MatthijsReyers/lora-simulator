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
