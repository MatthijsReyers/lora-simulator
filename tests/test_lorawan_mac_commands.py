"""Tests for LoRaWAN Phase 3: MAC commands."""

import pytest

from simulator.lorawan.mac_commands import (
    CID,
    LinkCheckReq, LinkCheckAns,
    LinkADRReq, LinkADRAns,
    DutyCycleReq, DutyCycleAns,
    RXParamSetupReq, RXParamSetupAns,
    DevStatusReq, DevStatusAns,
    NewChannelReq, NewChannelAns,
    RXTimingSetupReq, RXTimingSetupAns,
    encode_mac_commands,
    parse_downlink_commands,
    parse_uplink_commands,
)


# ── CID values ──────────────────────────────────────────────────────────────

class TestCID:
    def test_cid_values(self) -> None:
        assert CID.LINK_CHECK == 0x02
        assert CID.LINK_ADR == 0x03
        assert CID.DUTY_CYCLE == 0x04
        assert CID.RX_PARAM_SETUP == 0x05
        assert CID.DEV_STATUS == 0x06
        assert CID.NEW_CHANNEL == 0x07
        assert CID.RX_TIMING_SETUP == 0x08


# ── LinkCheck ────────────────────────────────────────────────────────────────

class TestLinkCheck:
    def test_link_check_req_encode(self) -> None:
        cmd = LinkCheckReq()
        assert cmd.encode() == bytes([0x02])

    def test_link_check_ans_roundtrip(self) -> None:
        cmd = LinkCheckAns(margin=20, gw_cnt=3)
        data = cmd.encode()
        assert data == bytes([0x02, 20, 3])
        decoded = LinkCheckAns.decode_payload(data[1:])
        assert decoded.margin == 20
        assert decoded.gw_cnt == 3

    def test_link_check_ans_zero_values(self) -> None:
        cmd = LinkCheckAns(margin=0, gw_cnt=0)
        decoded = LinkCheckAns.decode_payload(cmd.encode_payload())
        assert decoded.margin == 0
        assert decoded.gw_cnt == 0


# ── LinkADR ──────────────────────────────────────────────────────────────────

class TestLinkADR:
    def test_link_adr_req_roundtrip(self) -> None:
        cmd = LinkADRReq(data_rate=5, tx_power=2, ch_mask=0x00FF,
                         ch_mask_cntl=0, nb_trans=1)
        data = cmd.encode()
        assert data[0] == 0x03  # CID
        decoded = LinkADRReq.decode_payload(data[1:])
        assert decoded.data_rate == 5
        assert decoded.tx_power == 2
        assert decoded.ch_mask == 0x00FF
        assert decoded.ch_mask_cntl == 0
        assert decoded.nb_trans == 1

    def test_link_adr_req_keep_current(self) -> None:
        """DR and TXPower of 0xF means 'keep current'."""
        cmd = LinkADRReq(data_rate=0x0F, tx_power=0x0F)
        decoded = LinkADRReq.decode_payload(cmd.encode_payload())
        assert decoded.data_rate == 0x0F
        assert decoded.tx_power == 0x0F

    def test_link_adr_ans_all_ok(self) -> None:
        cmd = LinkADRAns(channel_mask_ack=True, data_rate_ack=True, power_ack=True)
        data = cmd.encode()
        assert data == bytes([0x03, 0x07])
        decoded = LinkADRAns.decode_payload(data[1:])
        assert decoded.channel_mask_ack is True
        assert decoded.data_rate_ack is True
        assert decoded.power_ack is True

    def test_link_adr_ans_partial_reject(self) -> None:
        cmd = LinkADRAns(channel_mask_ack=True, data_rate_ack=False, power_ack=True)
        decoded = LinkADRAns.decode_payload(cmd.encode_payload())
        assert decoded.channel_mask_ack is True
        assert decoded.data_rate_ack is False
        assert decoded.power_ack is True


# ── DutyCycle ────────────────────────────────────────────────────────────────

class TestDutyCycle:
    def test_duty_cycle_req_roundtrip(self) -> None:
        cmd = DutyCycleReq(max_duty_cycle=4)
        data = cmd.encode()
        assert data[0] == 0x04
        decoded = DutyCycleReq.decode_payload(data[1:])
        assert decoded.max_duty_cycle == 4

    def test_duty_cycle_ans_encode(self) -> None:
        cmd = DutyCycleAns()
        assert cmd.encode() == bytes([0x04])


# ── RXParamSetup ─────────────────────────────────────────────────────────────

class TestRXParamSetup:
    def test_rx_param_setup_req_roundtrip(self) -> None:
        cmd = RXParamSetupReq(rx1_dr_offset=3, rx2_data_rate=0)
        data = cmd.encode()
        assert data[0] == 0x05
        decoded = RXParamSetupReq.decode_payload(data[1:])
        assert decoded.rx1_dr_offset == 3
        assert decoded.rx2_data_rate == 0

    def test_rx_param_setup_ans_all_ok(self) -> None:
        cmd = RXParamSetupAns(rx1_dr_offset_ack=True, rx2_data_rate_ack=True, channel_ack=True)
        data = cmd.encode()
        assert data == bytes([0x05, 0x07])
        decoded = RXParamSetupAns.decode_payload(data[1:])
        assert decoded.rx1_dr_offset_ack is True
        assert decoded.rx2_data_rate_ack is True
        assert decoded.channel_ack is True


# ── DevStatus ────────────────────────────────────────────────────────────────

class TestDevStatus:
    def test_dev_status_req_encode(self) -> None:
        cmd = DevStatusReq()
        assert cmd.encode() == bytes([0x06])

    def test_dev_status_ans_roundtrip(self) -> None:
        cmd = DevStatusAns(battery=128, margin=10)
        data = cmd.encode()
        assert data[0] == 0x06
        decoded = DevStatusAns.decode_payload(data[1:])
        assert decoded.battery == 128
        assert decoded.margin == 10

    def test_dev_status_ans_negative_margin(self) -> None:
        cmd = DevStatusAns(battery=0, margin=-15)
        decoded = DevStatusAns.decode_payload(cmd.encode_payload())
        assert decoded.battery == 0
        assert decoded.margin == -15

    def test_dev_status_ans_external_power(self) -> None:
        cmd = DevStatusAns(battery=0, margin=0)
        decoded = DevStatusAns.decode_payload(cmd.encode_payload())
        assert decoded.battery == 0

    def test_dev_status_ans_margin_boundary_values(self) -> None:
        """Test min/max of signed 6-bit margin (-32 to 31)."""
        for margin in [-32, -1, 0, 1, 31]:
            cmd = DevStatusAns(battery=100, margin=margin)
            decoded = DevStatusAns.decode_payload(cmd.encode_payload())
            assert decoded.margin == margin


# ── NewChannel ───────────────────────────────────────────────────────────────

class TestNewChannel:
    def test_new_channel_req_roundtrip(self) -> None:
        cmd = NewChannelReq(ch_index=3, frequency=868_100_000, min_dr=0, max_dr=5)
        data = cmd.encode()
        assert data[0] == 0x07
        decoded = NewChannelReq.decode_payload(data[1:])
        assert decoded.ch_index == 3
        assert decoded.frequency == 868_100_000
        assert decoded.min_dr == 0
        assert decoded.max_dr == 5

    def test_new_channel_ans_all_ok(self) -> None:
        cmd = NewChannelAns(data_rate_range_ok=True, channel_freq_ok=True)
        data = cmd.encode()
        assert data == bytes([0x07, 0x03])
        decoded = NewChannelAns.decode_payload(data[1:])
        assert decoded.data_rate_range_ok is True
        assert decoded.channel_freq_ok is True


# ── RXTimingSetup ────────────────────────────────────────────────────────────

class TestRXTimingSetup:
    def test_rx_timing_setup_req_roundtrip(self) -> None:
        cmd = RXTimingSetupReq(delay=5)
        data = cmd.encode()
        assert data[0] == 0x08
        decoded = RXTimingSetupReq.decode_payload(data[1:])
        assert decoded.delay == 5

    def test_rx_timing_setup_req_delay_zero_maps_to_one(self) -> None:
        """Wire value 0 maps to 1 second delay."""
        cmd = RXTimingSetupReq(delay=1)
        raw = cmd.encode_payload()
        assert raw == bytes([0])  # Wire value is 0
        decoded = RXTimingSetupReq.decode_payload(raw)
        assert decoded.delay == 1

    def test_rx_timing_setup_ans_encode(self) -> None:
        cmd = RXTimingSetupAns()
        assert cmd.encode() == bytes([0x08])


# ── Stream parsing ───────────────────────────────────────────────────────────

class TestStreamParsing:
    def test_parse_downlink_empty(self) -> None:
        assert parse_downlink_commands(b"") == []

    def test_parse_uplink_empty(self) -> None:
        assert parse_uplink_commands(b"") == []

    def test_parse_single_downlink_command(self) -> None:
        cmd = LinkADRReq(data_rate=3, tx_power=4, ch_mask=0xFFFF, nb_trans=1)
        data = cmd.encode()
        result = parse_downlink_commands(data)
        assert len(result) == 1
        assert isinstance(result[0], LinkADRReq)
        assert result[0].data_rate == 3
        assert result[0].tx_power == 4

    def test_parse_single_uplink_command(self) -> None:
        cmd = LinkADRAns(channel_mask_ack=True, data_rate_ack=True, power_ack=True)
        data = cmd.encode()
        result = parse_uplink_commands(data)
        assert len(result) == 1
        assert isinstance(result[0], LinkADRAns)
        assert result[0].channel_mask_ack is True

    def test_parse_multiple_downlink_commands(self) -> None:
        """Multiple MAC commands concatenated in a single FOpts."""
        commands = [
            LinkADRReq(data_rate=5, tx_power=2),
            DevStatusReq(),
        ]
        data = encode_mac_commands(commands)
        result = parse_downlink_commands(data)
        assert len(result) == 2
        assert isinstance(result[0], LinkADRReq)
        assert isinstance(result[1], DevStatusReq)
        assert result[0].data_rate == 5

    def test_parse_multiple_uplink_commands(self) -> None:
        commands = [
            LinkADRAns(data_rate_ack=True, power_ack=True, channel_mask_ack=True),
            DevStatusAns(battery=200, margin=5),
        ]
        data = encode_mac_commands(commands)
        result = parse_uplink_commands(data)
        assert len(result) == 2
        assert isinstance(result[0], LinkADRAns)
        assert isinstance(result[1], DevStatusAns)
        assert result[1].battery == 200
        assert result[1].margin == 5

    def test_parse_stops_on_unknown_cid(self) -> None:
        data = bytes([0x02]) + bytes([0xFF, 0x01, 0x02])  # LinkCheckReq then unknown
        result = parse_uplink_commands(data)
        assert len(result) == 1
        assert isinstance(result[0], LinkCheckReq)

    def test_parse_stops_on_truncated_payload(self) -> None:
        # LinkADRReq needs 4 bytes of payload, give it only 2
        data = bytes([0x03, 0x52, 0xFF])
        result = parse_downlink_commands(data)
        assert len(result) == 0


# ── encode_mac_commands ──────────────────────────────────────────────────────

class TestEncodeMacCommands:
    def test_encode_empty(self) -> None:
        assert encode_mac_commands([]) == b""

    def test_encode_roundtrip(self) -> None:
        commands = [
            RXTimingSetupReq(delay=3),
            DutyCycleReq(max_duty_cycle=2),
        ]
        data = encode_mac_commands(commands)
        result = parse_downlink_commands(data)
        assert len(result) == 2
        assert isinstance(result[0], RXTimingSetupReq)
        assert result[0].delay == 3
        assert isinstance(result[1], DutyCycleReq)
        assert result[1].max_duty_cycle == 2
