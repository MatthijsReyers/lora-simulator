
from simulator.lora.enums.bandwidth import Bandwidth
from simulator.lora.enums.code_rate import CodeRate
from simulator.lora.enums.spreading_factor import SpreadingFactor
from simulator.lora.packet import LoraPacket
import math

def time_on_air(
    payload_len: int,
    bandwidth: Bandwidth|int,
    spreading_factor: SpreadingFactor|int,
    code_rate: CodeRate|int,
    preamble_len: int = 8,
    fixed_len: bool = False,
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
    if type(bandwidth) is int:
        bandwidth = Bandwidth.from_khz(bandwidth)
    if type(spreading_factor) is int:
        spreading_factor = SpreadingFactor(spreading_factor)
    if type(code_rate) is int:
        code_rate = CodeRate.from_denominator(code_rate)

    assert preamble_len >= 0, "Preamble length must be non-negative"
    assert type(preamble_len) is int, "Preamble length must be an integer"
    assert payload_len > 0, "Payload length must be positive"
    assert type(payload_len) is int, "Payload length must be an integer"

    # Time it takes to send one symbol (in seconds)
    t_sym = (2 ** spreading_factor) / (bandwidth.to_hz())

    # Preamble time (note that the radio always adds 4.25 symbols)
    preamble_symbols = (preamble_len + 4.25)
    t_preamble = preamble_symbols * t_sym

    # Variable names taken from the formulas in the LoRa Design Guide
    pl = payload_len
    sf = spreading_factor.value
    h = 1 if fixed_len else 0 # Implicit header 
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

    t_payload = payload_symbols * t_sym

    return t_preamble + t_payload


def packet_airtime(packet: LoraPacket) -> float:
    """
        Calculate the time on air for a LoRa packet (in seconds).
    """
    return time_on_air(
        payload_len=len(packet.payload),
        bandwidth=packet.bandwidth,
        spreading_factor=packet.spreading_factor,
        code_rate=packet.code_rate,
        preamble_len=packet.preamble_len,
        fixed_len=packet.fixed_len,
        crc_enabled=packet.crc_enabled,
        low_data_rate_optimize=False, # TODO: Determine when to enable this based on SF and BW
    )
