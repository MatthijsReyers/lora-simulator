from __future__ import annotations

import math

from simulator.lora.airtime import symbol_airtime
from simulator.lora.enums.bandwidth import Bandwidth
from simulator.lora.enums.code_rate import CodeRate
from simulator.lora.enums.spreading_factor import SpreadingFactor
from simulator.lora.radio_config import LoraConfig


class DataRate:
    """A LoRaWAN data rate entry mapping DR index to LoRa modulation parameters.

    ``max_payload`` is RP002's **N** column: the largest application payload (FRMPayload)
    the data rate may carry when the frame header holds no FOpts. It is *already* net of
    the MAC header, the mandatory frame header and the FPort octet — RP002's **M** column
    is ``N + 8``.
    """

    def __init__(self, spreading_factor: SpreadingFactor, bandwidth: Bandwidth, max_payload: int):
        self.spreading_factor = spreading_factor
        self.bandwidth = bandwidth
        self.max_payload = max_payload

    def to_lora_config(self, **kwargs: object) -> LoraConfig:
        """Create a LoraConfig from this data rate."""
        return LoraConfig(
            spreading_factor=self.spreading_factor,
            bandwidth=self.bandwidth,
            code_rate=CodeRate.CR4_5,
            **kwargs,  # type: ignore[arg-type]
        )


# EU868 region parameters for LoRaWAN 1.0.4.
# Reference: LoRaWAN Regional Parameters RP002-1.0.4 (EU863-870).
EU868_DATA_RATES: dict[int, DataRate] = {
    0: DataRate(SpreadingFactor.SF12, Bandwidth.KHz125, max_payload=51),
    1: DataRate(SpreadingFactor.SF11, Bandwidth.KHz125, max_payload=51),
    2: DataRate(SpreadingFactor.SF10, Bandwidth.KHz125, max_payload=51),
    3: DataRate(SpreadingFactor.SF9,  Bandwidth.KHz125, max_payload=115),
    4: DataRate(SpreadingFactor.SF8,  Bandwidth.KHz125, max_payload=222),
    5: DataRate(SpreadingFactor.SF7,  Bandwidth.KHz125, max_payload=222),
}

# Fixed overhead, in octets, that a data frame carries in front of its FRMPayload: the MAC header
# (1), the mandatory part of the frame header (DevAddr 4 + FCtrl 1 + FCnt 2 = 7) and the FPort (1).
# Any FOpts carried in the frame header come on top of this.
#
# NOTE: this is the overhead of the whole PHY payload. It is *not* subtracted by
# :func:`max_frm_payload`, because ``DataRate.max_payload`` is already RP002's N column
# (the application-payload limit, net of FHDR + FPort). It is kept for callers that size a
# complete frame rather than its FRMPayload.
FRAME_OVERHEAD = 9


def max_frm_payload(data_rate: int, fopts_len: int = 0) -> int:
    """Maximum FRMPayload size, in octets, for a data rate.

    RP002's EU863-870 table gives two columns per data rate: **M**, the largest MACPayload,
    and **N**, the largest application payload, with ``N = M - 8`` because every frame
    spends 7 octets on the mandatory frame header and 1 on the FPort. ``DataRate.max_payload``
    holds **N**, so the only thing still to subtract here is the FOpts the frame carries —
    MAC commands in FOpts eat into the same budget.

    For EU868 with an empty FOpts this yields 51 octets for DR0–DR2, 115 for DR3 and 222 for
    DR4–DR5.

    Reference: LoRaWAN Regional Parameters RP002-1.0.4 §2.4.6 (EU863-870 maximum payload size).

    :param data_rate: EU868 data rate index (0–5).
    :param fopts_len: Number of FOpts octets the frame will carry (0–15).
    :returns: Maximum number of plaintext FRMPayload octets, never negative.
    """
    assert data_rate in EU868_DATA_RATES, f"Unknown data rate DR{data_rate}"
    assert 0 <= fopts_len <= 15, f"FOpts is at most 15 octets, got {fopts_len}"
    return max(0, EU868_DATA_RATES[data_rate].max_payload - fopts_len)


# The three default uplink channels every EU868 device and gateway must support, a gateway
# typically listens to all of these (and usually a few more) simultaneously.
EU868_DEFAULT_UPLINK_CHANNELS = [
    868_100_000,   # 868.1 MHz
    868_300_000,   # 868.3 MHz
    868_500_000,   # 868.5 MHz
]

# Downlinks (RX1/RX2 replies, Class B beacons and ping-slot frames, Class C and multicast
# traffic) are transmitted with inverted IQ and uplinks with normal IQ. A receiver tuned to one
# polarity does not even detect the preamble of the other, which is what keeps end devices from
# hearing each other's uplinks on a shared channel and keeps the gateway from hearing its own
# kind. Reference: LoRaWAN L2 1.0.4 §3 (and RP002-1.0.4 §2.4.5 for EU868).
DOWNLINK_IQ_INVERTED = True

# Default RX2 parameters for EU868
RX2_DEFAULT_DR = 0                    # DR0 (SF12/125kHz)
RX2_DEFAULT_FREQUENCY = 869_525_000   # 869.525 MHz

# Default receive delays (seconds)
RECEIVE_DELAY1 = 1   # RX1 opens 1 second after TX end
RECEIVE_DELAY2 = 2   # RX2 opens 2 seconds after TX end (RECEIVE_DELAY1 + 1)

# How early a device starts listening before a receive window's nominal opening time (seconds).
# A device that only starts listening at the nominal time would miss a downlink that is exactly
# on time, because the radio still needs to come out of sleep. Real devices also use this margin
# to absorb clock drift between themselves and the network.
RX_WINDOW_GUARD = 0.005

# How long a device keeps a receive window open when nothing arrives.
#
# A device only has to listen long enough to find out whether a preamble is coming in; once it
# has locked onto one it keeps the receiver on until the frame is over. The detection timeout is
# therefore expressed in *symbols* at the window's data rate, which makes the window longer at
# the slower data rates: six symbols last 6 ms at SF7/125 kHz and almost 200 ms at SF12/125 kHz.
# Keeping the receiver on for a fixed half second instead (which is what this used to be) made
# every empty window -- the vast majority of them -- cost 10-80x the energy it costs on hardware,
# and empty windows are where a Class A or Class B device spends nearly all of its receive time.
#
# The timeout is the larger of a minimum number of symbols and however many symbols it takes to
# absorb the timing error between the device and the network at both ends of the window. This is
# the computation LoRaMac-node performs in ``RegionCommonComputeRxWindowParameters``
# (``RegionCommon.c``) from its ``MinRxSymbols`` and ``SystemMaxRxError`` settings.
RX_WINDOW_MIN_SYMBOLS = 6

# Worst-case timing error between the device and the network, in seconds, that a window has to
# absorb. The window is opened this much early (``RX_WINDOW_GUARD``) and the detection timeout
# is stretched to cover the same amount of lateness.
RX_WINDOW_MAX_RX_ERROR = RX_WINDOW_GUARD


def rx_window_timeout_symbols(
    spreading_factor: SpreadingFactor | int,
    bandwidth: Bandwidth | int,
    min_symbols: int = RX_WINDOW_MIN_SYMBOLS,
    rx_error: float = RX_WINDOW_MAX_RX_ERROR,
) -> int:
    """Number of symbols a receive window waits for a preamble before closing.

    ``max(ceil(((2 * min_symbols - 8) * t_symbol + 2 * rx_error) / t_symbol), min_symbols)``,
    as in LoRaMac-node: at least ``min_symbols``, and at the fast data rates (where a symbol is
    much shorter than the timing error) enough extra symbols to still catch a preamble that
    arrives ``rx_error`` late.

    :param spreading_factor: Spreading factor of the window's data rate.
    :param bandwidth: Bandwidth of the window's data rate.
    :param min_symbols: Fewest symbols the demodulator needs to detect a preamble.
    :param rx_error: Worst-case timing error to absorb, in seconds.
    """
    assert min_symbols > 0, "A receive window needs at least one symbol"
    assert rx_error >= 0.0, "The timing error cannot be negative"
    t_symbol = symbol_airtime(bandwidth, spreading_factor)
    symbols = math.ceil(((2 * min_symbols - 8) * t_symbol + 2 * rx_error) / t_symbol)
    return max(symbols, min_symbols)


def rx_window_duration(
    spreading_factor: SpreadingFactor | int,
    bandwidth: Bandwidth | int,
    min_symbols: int = RX_WINDOW_MIN_SYMBOLS,
    rx_error: float = RX_WINDOW_MAX_RX_ERROR,
) -> float:
    """How long, in seconds, a receive window stays open when no preamble arrives.

    See :func:`rx_window_timeout_symbols`; this is that count multiplied by the symbol time of
    the window's data rate. For EU868 with the defaults: ~14 ms at DR5 (SF7), ~29 ms at DR3
    (SF9) and ~197 ms at DR0 (SF12).
    """
    symbols = rx_window_timeout_symbols(spreading_factor, bandwidth, min_symbols, rx_error)
    return symbols * symbol_airtime(bandwidth, spreading_factor)

# Join-accept delays
JOIN_ACCEPT_DELAY1 = 5  # seconds
JOIN_ACCEPT_DELAY2 = 6  # seconds

# Maximum frame counter value (32-bit)
MAX_FCNT = 0xFFFFFFFF

# ------ Class B timing (EU868) ------
# Reference: LoRaWAN L2 1.0.4 §12, RP002-1.0.4 §2.8
BEACON_INTERVAL = 128          # seconds between beacon broadcasts
BEACON_RESERVED = 2.120        # seconds reserved for beacon transmission
BEACON_GUARD = 3.0             # guard time before next beacon window
# How much later than its nominal time a device still expects a beacon to start. The gateway
# never *delays* a beacon on purpose, but a frame that was already on the air when the beacon
# came due (an RX1 reply, a multicast fragment of up to ~2.6 s at DR0) finishes first.
BEACON_LATE_TOLERANCE = 1.0
PING_SLOT_LEN = 0.030          # 30 ms per ping slot
CLASS_B_DEFAULT_PING_NB = 16   # default number of ping slots per beacon period
MAX_BEACON_LESS_PERIOD = 7200  # 2 hours: max time without beacon before sync loss
