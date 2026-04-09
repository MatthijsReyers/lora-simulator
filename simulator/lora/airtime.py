
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
    assert isinstance(spreading_factor, SpreadingFactor)
    assert isinstance(bandwidth, Bandwidth)
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


def header_airtime(
    bandwidth: Bandwidth|int,
    spreading_factor: SpreadingFactor|int,
    code_rate: CodeRate|int,
    low_data_rate_optimize: bool = False,
) -> float:
    """
        Calculate the explicit header time on air for a LoRa packet (in seconds).
        
        The explicit header is 20 bits and contains payload length, coding rate, and CRC presence.
        In implicit header mode (fixed_payload_len=True), there is no header.

        See: https://www.openhacks.com/uploadsproductos/loradesignguide_std.pdf
    """
    if type(spreading_factor) is int:
        spreading_factor = SpreadingFactor(spreading_factor)
    if type(code_rate) is int:
        code_rate = CodeRate.from_denominator(code_rate)
    assert isinstance(spreading_factor, SpreadingFactor)
    assert isinstance(code_rate, CodeRate)

    t_sym = symbol_airtime(bandwidth, spreading_factor)

    sf = spreading_factor.value
    de = 1 if low_data_rate_optimize else 0
    cr = code_rate.to_denominator() - 4

    # Header contributes 20 bits to the payload calculation
    # This is derived from the formula: the -20*h term when h=0 (explicit header)
    header_symbols = math.ceil(20 / (4 * (sf - 2 * de))) * (cr + 4)

    return header_symbols * t_sym


def data_airtime(
    payload_len: int,
    bandwidth: Bandwidth|int,
    spreading_factor: SpreadingFactor|int,
    code_rate: CodeRate|int,
    crc_enabled: bool = True,
    low_data_rate_optimize: bool = False,
) -> float:
    """
        Calculate the data (without header) time on air for a LoRa packet (in seconds).
        
        This calculates the airtime for the payload data portion only, assuming implicit header mode.

        See: https://www.openhacks.com/uploadsproductos/loradesignguide_std.pdf
    """
    if type(spreading_factor) is int:
        spreading_factor = SpreadingFactor(spreading_factor)
    if type(code_rate) is int:
        code_rate = CodeRate.from_denominator(code_rate)
    assert isinstance(spreading_factor, SpreadingFactor)
    assert isinstance(code_rate, CodeRate)

    assert payload_len > 0, "Payload length must be positive"
    assert type(payload_len) is int, "Payload length must be an integer"

    t_sym = symbol_airtime(bandwidth, spreading_factor)

    # Variable names taken from the formulas in the LoRa Design Guide
    pl = payload_len
    sf = spreading_factor.value
    h = 1  # Implicit header (no header in this calculation)
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
    t_data = data_airtime(
        payload_len=payload_len,
        bandwidth=bandwidth,
        spreading_factor=spreading_factor,
        code_rate=code_rate,
        crc_enabled=crc_enabled,
        low_data_rate_optimize=low_data_rate_optimize,
    )
    
    if fixed_payload_len:
        # Implicit header mode - no header
        return t_data
    else:
        # Explicit header mode - include header
        t_header = header_airtime(
            bandwidth=bandwidth,
            spreading_factor=spreading_factor,
            code_rate=code_rate,
            low_data_rate_optimize=low_data_rate_optimize,
        )
        return t_header + t_data


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
