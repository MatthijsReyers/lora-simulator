"""
Time on air per the Semtech formula (SX1276 datasheet section 4.1.1.7, LoRa Design Guide). The
explicit PHY header is folded into the same ceiling as the payload bits; computing it as a
separate code block over-counted one block (CR + 4 symbols) for about half of all payload
length / spreading factor combinations.
"""
import math

import pytest

from simulator.lora.airtime import (
    data_airtime,
    estimate_airtime,
    header_airtime,
    low_data_rate_optimize_required,
    payload_airtime,
    preamble_airtime,
    symbol_airtime,
)
from simulator.lora.packet import LoraPacket
from simulator.lora.radio_config import LoraConfig


def semtech_symbols(pl: int, sf: int, cr: int = 1, crc: int = 1, ih: int = 0, de: int = 0) -> float:
    """ The datasheet formula, written out independently of the implementation. """
    return 8 + max(math.ceil((8 * pl - 4 * sf + 28 + 16 * crc - 20 * ih) / (4 * (sf - 2 * de))) * (cr + 4), 0)


def semtech_airtime(pl: int, sf: int, bw: int = 125, n_preamble: int = 8, **kwargs) -> float:
    t_sym = 2 ** sf / (bw * 1000)
    return (n_preamble + 4.25 + semtech_symbols(pl, sf, **kwargs)) * t_sym


@pytest.mark.parametrize("payload, sf, expected_ms", [
    # The well known The Things Network numbers for a 51 byte application payload (64 byte PHY
    # payload), 125 kHz, CR 4/5, explicit header, CRC on, LDRO on at SF 11 and SF 12.
    (64, 7, 118.0),
    (64, 12, 2793.5),
    # And the values of the paper of papers/fuota-sequential-sf-2025 (50 byte fragments).
    (50, 7, 97.5),
    (50, 12, 2138.1),
])
def test_known_airtimes(payload: int, sf: int, expected_ms: float):
    ldro = sf >= 11 and payload == 64
    airtime = estimate_airtime(payload, 125, sf, 5, preamble_len=8, low_data_rate_optimize=ldro)
    assert airtime * 1000 == pytest.approx(expected_ms, abs=0.1)


@pytest.mark.parametrize("sf", range(7, 13))
@pytest.mark.parametrize("payload", [1, 5, 13, 20, 50, 51, 64, 100, 128, 222, 255])
@pytest.mark.parametrize("cr", [5, 6, 7, 8])
def test_matches_semtech_formula(payload: int, sf: int, cr: int):
    for crc in (True, False):
        for fixed in (False, True):
            for ldro in (False, True):
                expected = semtech_airtime(payload, sf, cr=cr - 4, crc=int(crc), ih=int(fixed), de=int(ldro))
                actual = estimate_airtime(
                    payload, 125, sf, cr, preamble_len=8, fixed_payload_len=fixed,
                    crc_enabled=crc, low_data_rate_optimize=ldro,
                )
                assert actual == pytest.approx(expected, rel=1e-12)


def test_explicit_header_costs_at_most_one_code_block():
    for sf in range(7, 13):
        for payload in range(1, 256):
            implicit = payload_airtime(payload, 125, sf, 5, fixed_payload_len=True)
            explicit = payload_airtime(payload, 125, sf, 5, fixed_payload_len=False)
            extra_symbols = (explicit - implicit) / symbol_airtime(125, sf)
            assert round(extra_symbols, 6) in (0, 5)


def test_header_is_the_first_eight_symbols():
    # The header occupies the "8 +" block that follows the preamble; it is not an extra block.
    assert header_airtime(125, 7, 5) == pytest.approx(8 * symbol_airtime(125, 7))
    assert preamble_airtime(125, 7, 8) == pytest.approx(12.25 * symbol_airtime(125, 7))


def test_data_airtime_default_is_implicit_header():
    assert data_airtime(50, 125, 7, 5) == payload_airtime(50, 125, 7, 5, fixed_payload_len=True)


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
