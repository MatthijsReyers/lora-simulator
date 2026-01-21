

from simulator.lora.enums.bandwidth import Bandwidth
from simulator.lora.enums.code_rate import CodeRate
from simulator.lora.enums.spreading_factor import SpreadingFactor

class LoraConfig():
    """
        Configuration parameters for a LoRa radio. These settings define how the radio operates
        in transmission and receive mode.
    """
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
        bandwidth: Bandwidth = Bandwidth.KHz125,
        spreading_factor: SpreadingFactor = SpreadingFactor.SF7,
        code_rate: CodeRate = CodeRate.CR4_5,
        preamble_len: int = 8,
        payload_len: int = 64,
        symbols: int = 0,
        fixed_payload_len: bool = False,
        crc_enabled: bool = True,
        iq_inverted: bool = False
    ):
        self.bandwidth = bandwidth
        self.spreading_factor = spreading_factor
        self.code_rate = code_rate
        self.preamble_len = preamble_len
        self.payload_len = payload_len
        self.symbols = symbols
        self.fixed_payload_len = fixed_payload_len
        self.crc_enabled = crc_enabled
        self.iq_inverted = iq_inverted


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
            iq_inverted=self.iq_inverted
        )