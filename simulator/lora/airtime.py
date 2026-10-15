
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


def low_data_rate_optimize_required(
    bandwidth: Bandwidth|int,
    spreading_factor: SpreadingFactor|int,
) -> bool:
    """
        Whether the radio enables low data rate optimization for this SF/BW combination.

        Semtech recommends it when the symbol time is 16 ms or longer (SF11/SF12 at 125 kHz and
        SF12 at 250 kHz), and the STM32WL SubGHz_Phy driver enables it automatically in exactly
        those cases.
    """
    return symbol_airtime(bandwidth, spreading_factor) >= 16e-3


def _resolve_low_data_rate_optimize(
    bandwidth: Bandwidth|int,
    spreading_factor: SpreadingFactor|int,
    low_data_rate_optimize: bool|None,
) -> bool:
    if low_data_rate_optimize is None:
        return low_data_rate_optimize_required(bandwidth, spreading_factor)
    return low_data_rate_optimize


def _payload_symbols(
    payload_len: int,
    spreading_factor: SpreadingFactor,
    code_rate: CodeRate,
    explicit_header: bool,
    crc_enabled: bool,
    low_data_rate_optimize: bool,
) -> int:
    """
        Number of symbols after the preamble (header + data), from the SX1276 datasheet formula.

        The header and data bits share a single ceil(): rounding them up separately can add a
        whole extra block of (CR+4) symbols.
    """
    # Variable names taken from the formulas in the LoRa Design Guide
    pl = payload_len
    sf = spreading_factor.value
    h = 0 if explicit_header else 1
    de = 1 if low_data_rate_optimize else 0
    cr = code_rate.to_denominator() - 4
    crc = 1 if crc_enabled else 0

    return 8 + max(
        0,
        math.ceil(
            (8*pl - 4*sf + 28 + 16*crc - 20*h) / (4*(sf - 2*de))
        ) * (cr + 4)
    )


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
    low_data_rate_optimize: bool|None = None,
) -> float:
    """
        Calculate the explicit header time on air for a LoRa packet (in seconds).
        
        The explicit header is 20 bits and contains payload length, coding rate, and CRC presence.
        In implicit header mode (fixed_payload_len=True), there is no header.

        This is the header's own share rounded up to whole blocks, so it is an approximation of
        when the header ends; use payload_airtime() for the total, which rounds only once.

        low_data_rate_optimize=None picks what the radio would use for this SF/BW.

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
    de = 1 if _resolve_low_data_rate_optimize(bandwidth, spreading_factor, low_data_rate_optimize) else 0
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
    low_data_rate_optimize: bool|None = None,
) -> float:
    """
        Calculate the data (without header) time on air for a LoRa packet (in seconds).
        
        This calculates the airtime for the payload data portion only, assuming implicit header mode.

        low_data_rate_optimize=None picks what the radio would use for this SF/BW.

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
    payload_symbols = _payload_symbols(
        payload_len=payload_len,
        spreading_factor=spreading_factor,
        code_rate=code_rate,
        explicit_header=False,
        crc_enabled=crc_enabled,
        low_data_rate_optimize=_resolve_low_data_rate_optimize(
            bandwidth, spreading_factor, low_data_rate_optimize
        ),
    )
    return payload_symbols * t_sym


def payload_airtime(
    payload_len: int,
    bandwidth: Bandwidth|int,
    spreading_factor: SpreadingFactor|int,
    code_rate: CodeRate|int,
    fixed_payload_len: bool = False,
    crc_enabled: bool = True,
    low_data_rate_optimize: bool|None = None,
) -> float:
    """
        Calculate the payload (header + data) time on air for a LoRa packet (in seconds).

        low_data_rate_optimize=None picks what the radio would use for this SF/BW.

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
    payload_symbols = _payload_symbols(
        payload_len=payload_len,
        spreading_factor=spreading_factor,
        code_rate=code_rate,
        # Implicit header mode (fixed length) has no header
        explicit_header=not fixed_payload_len,
        crc_enabled=crc_enabled,
        low_data_rate_optimize=_resolve_low_data_rate_optimize(
            bandwidth, spreading_factor, low_data_rate_optimize
        ),
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
    low_data_rate_optimize: bool|None = None,
) -> float:
    """
        Calculate the time on air for a LoRa packet (in seconds).

        low_data_rate_optimize=None picks what the radio would use for this SF/BW.

        See: https://www.openhacks.com/uploadsproductos/loradesignguide_std.pdf

        Validated against:
        - https://www.semtech.com/design-support/lora-calculator
        - https://www.thethingsnetwork.org/airtime-calculator/
        - https://iftnt.github.io/lora-air-time/index.html
        - Measured TX durations of an STM32WL55 (tests/test_airtime.py)
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
