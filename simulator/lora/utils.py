
from simulator.lora.enums.bandwidth import Bandwidth
from simulator.lora.enums.code_rate import CodeRate
from simulator.lora.enums.spreading_factor import SpreadingFactor
import math

def symbol_airtime(bandwidth: Bandwidth|int, spreading_factor: SpreadingFactor|int) -> float:
    """
        Calculate the airtime for one LoRa symbol (in seconds).
    """
    if type(bandwidth) is int:
        bandwidth = Bandwidth.from_khz(bandwidth)
    if type(spreading_factor) is int:
        spreading_factor = SpreadingFactor(spreading_factor)
    return (2 ** spreading_factor.value) / (bandwidth.to_hz())


def preamble_airtime(
    bandwidth: Bandwidth|int,
    spreading_factor: SpreadingFactor|int,
    preamble_len: int = 8,
) -> float:
    """
        Calculate the preamble time on air for a LoRa packet (in seconds).
        
        Note that the radio always adds 4.25 symbols to the preamble.
    """
    if type(bandwidth) is int:
        bandwidth = Bandwidth.from_khz(bandwidth)
    if type(spreading_factor) is int:
        spreading_factor = SpreadingFactor(spreading_factor)

    assert preamble_len >= 0, "Preamble length must be non-negative"
    assert type(preamble_len) is int, "Preamble length must be an integer"

    t_sym = symbol_airtime(bandwidth, spreading_factor)
    preamble_symbols = (preamble_len + 4.25)
    return preamble_symbols * t_sym


def payload_airtime(
    payload_len: int,
    bandwidth: Bandwidth|int,
    spreading_factor: SpreadingFactor|int,
    code_rate: CodeRate|int,
    fixed_payload_len: bool = False,
    crc_enabled: bool = True,
    low_data_rate_optimize: bool = False,
) -> float:
    """
        Calculate the payload (header + data) time on air for a LoRa packet (in seconds).

        See: https://www.openhacks.com/uploadsproductos/loradesignguide_std.pdf
    """
    if type(spreading_factor) is int:
        spreading_factor = SpreadingFactor(spreading_factor)
    if type(code_rate) is int:
        code_rate = CodeRate.from_denominator(code_rate)

    assert payload_len > 0, "Payload length must be positive"
    assert type(payload_len) is int, "Payload length must be an integer"

    t_sym = symbol_airtime(bandwidth, spreading_factor)

    # Variable names taken from the formulas in the LoRa Design Guide
    pl = payload_len
    sf = spreading_factor.value
    h = 1 if fixed_payload_len else 0 # Implicit header 
    de = 1 if low_data_rate_optimize else 0
    cr = code_rate.to_denominator() - 4
    crc = 1 if crc_enabled else 0

    # Formula from SX1276/77/78/79 Semtech datasheet
    payload_symbols = 8 + max(
        0, 
        math.ceil(
            (8*pl - 4*sf + 28 + 16*crc - 20*h) / (4*(sf - 2*de))
        ) * (cr + 4)
    )

    return payload_symbols * t_sym


def estimate_airtime(
    payload_len: int,
    bandwidth: Bandwidth|int,
    spreading_factor: SpreadingFactor|int,
    code_rate: CodeRate|int,
    preamble_len: int = 8,
    fixed_payload_len: bool = False,
    crc_enabled: bool = True,
    low_data_rate_optimize: bool = False,
) -> float:
    """
        Calculate the time on air for a LoRa packet (in seconds).

        See: https://www.openhacks.com/uploadsproductos/loradesignguide_std.pdf

        Validated against:
        - https://www.semtech.com/design-support/lora-calculator
        - https://www.thethingsnetwork.org/airtime-calculator/
        - https://iftnt.github.io/lora-air-time/index.html
    """
    t_preamble = preamble_airtime(
        bandwidth=bandwidth,
        spreading_factor=spreading_factor,
        preamble_len=preamble_len,
    )
    t_payload = payload_airtime(
        payload_len=payload_len,
        bandwidth=bandwidth,
        spreading_factor=spreading_factor,
        code_rate=code_rate,
        fixed_payload_len=fixed_payload_len,
        crc_enabled=crc_enabled,
        low_data_rate_optimize=low_data_rate_optimize,
    )
    return t_preamble + t_payload
