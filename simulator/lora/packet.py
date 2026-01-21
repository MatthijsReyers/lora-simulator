
from simulator.lora.enums.bandwidth import Bandwidth
from simulator.lora.enums.code_rate import CodeRate
from simulator.lora.enums.spreading_factor import SpreadingFactor
from simulator.lora.radio_config import LoraConfig

# Global packet counter for unique packet IDs
_packet_id_counter = 0

class LoraPacket:
    def __init__(
            self, 
            payload: bytes, 
            tx_power: int,
            config: LoraConfig,
            snr: float = None,
            rssi: float = None,
        ):
        assert isinstance(config, LoraConfig), "config must be a LoraConfig object"
        assert isinstance(payload, bytes), "Payload must be of type bytes"
        assert isinstance(config.spreading_factor, SpreadingFactor), "Invalid spreading factor"
        assert isinstance(config.bandwidth, Bandwidth), "Invalid bandwidth"
        assert config.bandwidth != Bandwidth.Reserved, "Invalid bandwidth"

        global _packet_id_counter
        _packet_id_counter += 1
        self.id = _packet_id_counter
        
        self.config = config
        self.payload = payload
        self.tx_power = tx_power
        self.snr = snr
        self.rssi = rssi

    @property
    def code_rate(self) -> CodeRate:
        return self.config.code_rate
    
    @property
    def spreading_factor(self) -> SpreadingFactor:
        return self.config.spreading_factor
    
    @property
    def bandwidth(self) -> Bandwidth:
        return self.config.bandwidth
    
    @property
    def crc_enabled(self) -> bool:
        return self.config.crc_enabled
    
    @property
    def preamble_len(self) -> int:
        return self.config.preamble_len
    
    @property
    def fixed_len(self) -> bool:
        return self.config.fixed_payload_len
    
    @property
    def iq_inverted(self) -> bool:
        return self.config.iq_inverted
    
    @property
    def symbols(self) -> int:
        return self.config.symbols
    
    @property
    def payload_len(self) -> int:
        return self.config.payload_len

    def __repr__(self):
        return (f"LoRaPacket(id={self.id}, payload={self.payload}, spreading_factor={self.spreading_factor}, bandwidth={self.bandwidth}, code_rate={self.code_rate})")
    
    def copy(self) -> 'LoraPacket':
        return LoraPacket(
            payload=self.payload,
            tx_power=self.tx_power,
            config=self.config.copy(),
            snr=self.snr,
            rssi=self.rssi,
        )
