"""
Time on air per the Semtech formula (SX1276 datasheet section 4.1.1.7, LoRa Design Guide). The
explicit PHY header is folded into the same ceiling as the payload bits; computing it as a
separate code block over-counted one block (CR + 4 symbols) for about half of all payload
length / spreading factor combinations.
"""
import math

import pytest

from simulator.lora.airtime import (
    data_airtime, estimate_airtime, header_airtime, payload_airtime, preamble_airtime, symbol_airtime,
)


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
