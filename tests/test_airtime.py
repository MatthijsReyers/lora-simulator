import math

import pytest

from simulator.lora.airtime import (
    estimate_airtime,
    header_airtime,
    low_data_rate_optimize_required,
    payload_airtime,
    preamble_airtime,
)
from simulator.lora.packet import LoraPacket
from simulator.lora.radio_config import LoraConfig


# TX durations measured on an STM32WL55 (Nucleo-WL55JC1) running gateway-radio-firmware, 125kHz,
# CR 4/5, 8 preamble symbols, explicit header, CRC on (median of 2-5 transmissions each).
# The firmware reports the duration in RTC ticks of 1/1024s, not in milliseconds.
# Columns: (spreading factor, payload length, measured ticks)
STM32WL_MEASUREMENTS = [
    (7, 10, 44), (7, 11, 44), (7, 12, 44), (7, 13, 49), (7, 14, 49), (7, 20, 60),
    (9, 10, 150), (9, 11, 149), (9, 12, 150), (9, 13, 171), (9, 14, 171), (9, 20, 191),
    (12, 12, 1184.5),
    # SF11 at 125kHz: the radio turns on low data rate optimization by itself.
    (11, 51, 1348),
]

# The measurement starts at Radio.Send() and stops in the TxDone interrupt, so it includes a fixed
# start-up overhead of ~1.8ms on top of the airtime, plus one tick of quantisation.
MIN_OVERHEAD = 0.0
MAX_OVERHEAD = 3.5e-3


@pytest.mark.parametrize("sf, payload_len, ticks", STM32WL_MEASUREMENTS)
def test_airtime_matches_stm32wl_measurement(sf, payload_len, ticks):
    measured = ticks / 1024
    estimate = estimate_airtime(payload_len, 125, sf, 5)
    assert MIN_OVERHEAD <= measured - estimate <= MAX_OVERHEAD


@pytest.mark.parametrize("sf, payload_len, expected_ms", [
    # Values from the Semtech LoRa calculator / TTN airtime calculator (LoRaWAN EU868 settings)
    (7, 12, 41.216),
    (7, 51, 102.656),
    (9, 12, 144.384),
    (12, 12, 1155.072),
    (12, 51, 2465.792),
])
def test_airtime_matches_reference_calculators(sf, payload_len, expected_ms):
    assert math.isclose(estimate_airtime(payload_len, 125, sf, 5) * 1e3, expected_ms, abs_tol=1e-3)


def test_header_and_data_share_one_rounding_step():
    # SF7 PL=12 and PL=10 fit in the same number of symbol blocks; rounding the header up on its
    # own used to add a whole extra block (5 symbols) to PL=12.
    assert estimate_airtime(12, 125, 7, 5) == estimate_airtime(10, 125, 7, 5)
    assert estimate_airtime(13, 125, 7, 5) - estimate_airtime(12, 125, 7, 5) == pytest.approx(5 * 1.024e-3)


def test_implicit_header_is_shorter_than_explicit():
    explicit = payload_airtime(50, 125, 9, 5, fixed_payload_len=False)
    implicit = payload_airtime(50, 125, 9, 5, fixed_payload_len=True)
    assert implicit < explicit


@pytest.mark.parametrize("bandwidth, sf, required", [
    (125, 10, False), (125, 11, True), (125, 12, True),
    (250, 11, False), (250, 12, True),
    (500, 12, False),
])
def test_low_data_rate_optimize_follows_the_radio(bandwidth, sf, required):
    assert low_data_rate_optimize_required(bandwidth, sf) is required


def test_low_data_rate_optimize_can_be_overridden():
    auto = estimate_airtime(51, 125, 12, 5)
    forced_on = estimate_airtime(51, 125, 12, 5, low_data_rate_optimize=True)
    forced_off = estimate_airtime(51, 125, 12, 5, low_data_rate_optimize=False)
    assert auto == forced_on
    assert forced_off < forced_on


@pytest.mark.parametrize("sf", [7, 11, 12])
def test_packet_airtime_matches_estimate(sf):
    config = LoraConfig(bandwidth=125, spreading_factor=sf, code_rate=5)
    packet = LoraPacket(bytes(51), tx_power=14, tx_location=(0.0, 0.0), config=config)
    assert packet.airtime == pytest.approx(estimate_airtime(51, 125, sf, 5))
    assert packet.preamble_airtime == pytest.approx(preamble_airtime(125, sf))
    assert packet.header_airtime == pytest.approx(header_airtime(125, sf, 5))
