
from simulator.lora.enums.bandwidth import Bandwidth
from simulator.lora.enums.code_rate import CodeRate
from simulator.lora.enums.spreading_factor import SpreadingFactor

# Global packet counter for unique packet IDs
_packet_id_counter = 0

class LoraPacket:
    def __init__(
            self, 
            payload: bytes, 
            tx_power: int,
            code_rate: CodeRate|int,
            spreading_factor: SpreadingFactor|int,
            bandwidth: Bandwidth|int,
            crc_enabled: bool = True,
            preamble_len: int = 8,
            fixed_len: bool = False,
            iq_inverted: bool = False,
            symbols: int = 0,
            snr: float = None,
            rssi: float = None,
        ):
        if type(bandwidth) is int:
            bandwidth = Bandwidth.from_value(bandwidth)
        if type(spreading_factor) is int:
            spreading_factor = SpreadingFactor(spreading_factor)
        if type(code_rate) is int:
            code_rate = CodeRate.from_denominator(code_rate)
        
        assert isinstance(payload, bytes), "Payload must be of type bytes"
        assert isinstance(spreading_factor, SpreadingFactor), "Invalid spreading factor"
        assert isinstance(bandwidth, Bandwidth), "Invalid bandwidth"
        assert bandwidth != Bandwidth.Reserved, "Invalid bandwidth"

        global _packet_id_counter
        _packet_id_counter += 1
        self.id = _packet_id_counter
        
        self.payload = payload
        self.tx_power = tx_power
        self.code_rate = code_rate
        self.spreading_factor = spreading_factor
        self.bandwidth = bandwidth
        self.crc_enabled = crc_enabled
        self.preamble_len = preamble_len
        self.fixed_len = fixed_len
        self.iq_inverted = iq_inverted
        self.symbols = symbols
        self.snr = snr
        self.rssi = rssi

    def __repr__(self):
        return (f"LoRaPacket(payload={self.payload}, spreading_factor={self.spreading_factor}," \
                 f" bandwidth={self.bandwidth}, code_rate={self.code_rate})")
    
    def copy(self) -> 'LoraPacket':
        return LoraPacket(
            payload=self.payload,
            tx_power=self.tx_power,
            code_rate=self.code_rate,
            spreading_factor=self.spreading_factor,
            bandwidth=self.bandwidth,
            crc_enabled=self.crc_enabled,
            preamble_len=self.preamble_len,
            fixed_len=self.fixed_len,
            snr=self.snr,
            rssi=self.rssi,
        )
