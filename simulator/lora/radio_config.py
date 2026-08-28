

from simulator.lora.enums.bandwidth import Bandwidth
from simulator.lora.enums.code_rate import CodeRate
from simulator.lora.enums.spreading_factor import SpreadingFactor

# Default carrier frequency in hertz, this is the first EU868 uplink channel which is what most
# LoRa radios ship with as their default configuration.
DEFAULT_FREQUENCY = 868_100_000

class LoraConfig():
    """
        Configuration parameters for a LoRa radio. These settings define how the radio operates
        in transmission and receive mode.
    """
    frequency: int = DEFAULT_FREQUENCY
    bandwidth: Bandwidth = Bandwidth.KHz125
    spreading_factor: SpreadingFactor = SpreadingFactor.SF7
    code_rate: CodeRate = CodeRate.CR4_5
    preamble_len: int = 8
    payload_len: int = 64
    symbols: int = 0
    fixed_payload_len: bool = False
    crc_enabled: bool = True
    iq_inverted: bool = False

    def __init__(
        self,
        bandwidth: Bandwidth|int = Bandwidth.KHz125,
        spreading_factor: SpreadingFactor|int = SpreadingFactor.SF7,
        code_rate: CodeRate|int = CodeRate.CR4_5,
        preamble_len: int = 8,
        payload_len: int = 64,
        symbols: int = 0,
        fixed_payload_len: bool = False,
        crc_enabled: bool = True,
        iq_inverted: bool = False,
        frequency: int = DEFAULT_FREQUENCY,
    ):
        # Plain integers are accepted as convenient shorthands for the matching enum values:
        # the bandwidth in kHz, the spreading factor number, and the code rate denominator (4/x).
        if type(bandwidth) is int:
            bandwidth = Bandwidth.from_khz(bandwidth)
        if type(spreading_factor) is int:
            spreading_factor = SpreadingFactor(spreading_factor)
        if type(code_rate) is int:
            code_rate = CodeRate.from_denominator(code_rate)

        assert isinstance(bandwidth, Bandwidth), "Invalid bandwidth"
        assert isinstance(spreading_factor, SpreadingFactor), "Invalid spreading factor"
        assert isinstance(code_rate, CodeRate), "Invalid code rate"
        assert preamble_len > 0, "Preamble length must be positive"
        assert preamble_len <= 0xFFFF, "Preamble length must fit in u16"
        assert payload_len >= 0, "Payload length must be non-negative"
        assert symbols >= 0, "Symbols must be non-negative"
        assert type(fixed_payload_len) is bool, "Fixed payload length must be a boolean"
        assert type(crc_enabled) is bool, "CRC enabled must be a boolean"
        assert type(iq_inverted) is bool, "IQ inverted must be a boolean"
        assert type(frequency) is int, "Frequency must be an integer number of hertz"
        assert frequency > 0, "Frequency must be positive"

        self.bandwidth = bandwidth
        self.spreading_factor = spreading_factor
        self.code_rate = code_rate
        self.preamble_len = preamble_len
        self.payload_len = payload_len
        self.symbols = symbols
        self.fixed_payload_len = fixed_payload_len
        self.crc_enabled = crc_enabled
        self.iq_inverted = iq_inverted
        self.frequency = frequency


    def copy(self) -> 'LoraConfig':
        """
            Makes a copy of this configuration instance.
        """
        return LoraConfig(
            bandwidth=self.bandwidth,
            spreading_factor=self.spreading_factor,
            code_rate=self.code_rate,
            preamble_len=self.preamble_len,
            payload_len=self.payload_len,
            symbols=self.symbols,
            fixed_payload_len=self.fixed_payload_len,
            crc_enabled=self.crc_enabled,
            iq_inverted=self.iq_inverted,
            frequency=self.frequency,
        )


    def channel_edges(self) -> tuple[float, float]:
        """
            The lower and upper edge of the spectrum occupied by this configuration, in hertz. We
            approximate the occupied bandwidth of a LoRa signal by its configured bandwidth, real
            signals have side lobes outside of this range but they are far enough below the noise
            floor to be irrelevant here.
        """
        half = self.bandwidth.to_hz() / 2
        return (self.frequency - half, self.frequency + half)


    def overlaps(self, other: 'LoraConfig') -> bool:
        """
            Determines whether the spectrum occupied by this configuration overlaps with that of
            another configuration. Signals that do not overlap in spectrum cannot interfere with
            each other and cannot be demodulated by each other's radios.

            Note that this is not the same as the two configurations being on the same channel, a
            500kHz signal on 868.1MHz overlaps with a 125kHz signal on 868.3MHz even though the two
            are on different channels.
        """
        (low, high) = self.channel_edges()
        (other_low, other_high) = other.channel_edges()
        return low < other_high and other_low < high


    def same_channel(self, other: 'LoraConfig') -> bool:
        """
            Determines whether two configurations are tuned to the exact same channel, i.e. the
            same carrier frequency and the same bandwidth.
        """
        return self.frequency == other.frequency and self.bandwidth == other.bandwidth
