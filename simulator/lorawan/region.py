from simulator.lora.enums.bandwidth import Bandwidth
from simulator.lora.enums.code_rate import CodeRate
from simulator.lora.enums.spreading_factor import SpreadingFactor
from simulator.lora.radio_config import LoraConfig


class DataRate:
    """A LoRaWAN data rate entry mapping DR index to LoRa modulation parameters."""

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

# The three default uplink channels every EU868 device and gateway must support, a gateway
# typically listens to all of these (and usually a few more) simultaneously.
EU868_DEFAULT_UPLINK_CHANNELS = [
    868_100_000,   # 868.1 MHz
    868_300_000,   # 868.3 MHz
    868_500_000,   # 868.5 MHz
]

# Default RX2 parameters for EU868
RX2_DEFAULT_DR = 0                    # DR0 (SF12/125kHz)
RX2_DEFAULT_FREQUENCY = 869_525_000   # 869.525 MHz

# Default receive delays (seconds)
RECEIVE_DELAY1 = 1   # RX1 opens 1 second after TX end
RECEIVE_DELAY2 = 2   # RX2 opens 2 seconds after TX end (RECEIVE_DELAY1 + 1)

# Default RX window duration (seconds). Not strictly defined in the spec — the device must keep
# the window open long enough to detect a preamble. We use the time for 6 symbols at the given DR
# as the minimum, but in practice a fixed value works for simulation.
RX_WINDOW_DURATION = 0.5

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
PING_SLOT_LEN = 0.030          # 30 ms per ping slot
CLASS_B_DEFAULT_PING_NB = 16   # default number of ping slots per beacon period
MAX_BEACON_LESS_PERIOD = 7200  # 2 hours: max time without beacon before sync loss
