
from simulator.lora.enums.bandwidth import Bandwidth
from simulator.lora.enums.spreading_factor import SpreadingFactor


class LoraPacket:
    def __init__(
            self, 
            payload: bytes, 
            tx_power: int,
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
        
        assert isinstance(payload, bytes), "Payload must be of type bytes"
        assert isinstance(spreading_factor, SpreadingFactor), "Invalid spreading factor"
        assert isinstance(bandwidth, Bandwidth), "Invalid bandwidth"
        assert bandwidth != Bandwidth.Reserved, "Invalid bandwidth"

        self.payload = payload
        self.tx_power = tx_power
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
                 f" bandwidth={self.bandwidth})")
    
    def copy(self) -> 'LoraPacket':
        return LoraPacket(
            payload=self.payload,
            tx_power=self.tx_power,
            spreading_factor=self.spreading_factor,
            bandwidth=self.bandwidth,
            crc_enabled=self.crc_enabled,
            preamble_len=self.preamble_len,
            fixed_len=self.fixed_len,
            snr=self.snr,
            rssi=self.rssi,
        )
