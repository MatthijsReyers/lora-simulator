
from simulator.lora.radio_config import LoraConfig
from simulator.lora.airtime import estimate_airtime

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
            airtime: float = None
        ):
        assert isinstance(config, LoraConfig), "config must be a LoraConfig object"
        assert isinstance(payload, bytes), "Payload must be of type bytes"
        assert type(tx_power) is int, "tx_power must be an integer"

        global _packet_id_counter
        _packet_id_counter += 1
        self.id = _packet_id_counter
        
        self.config = config
        self.payload = payload
        self.tx_power = tx_power
        self.snr = snr
        self.rssi = rssi
        self._airtime = airtime

    def airtime(self) -> float:
        """ Estimates the total airtime of the packet in seconds. """
        # Airtime is cached after first calculation since its kind of expensive to compute
        if self._airtime is not None:
            return self._airtime
        airtime = estimate_airtime(
            payload_len=len(self.payload),
            bandwidth=self.config.bandwidth,
            spreading_factor=self.config.spreading_factor,
            code_rate=self.config.code_rate,
            preamble_len=self.config.preamble_len,
            fixed_payload_len=self.config.fixed_payload_len,
            crc_enabled=self.config.crc_enabled,
            low_data_rate_optimize=False, # TODO: Determine when to enable this based on SF and BW
        )
        self._airtime = airtime
        return airtime

    def __repr__(self):
        return (f"LoRaPacket(id={self.id}, payload={self.payload}, spreading_factor={self.config.spreading_factor}, bandwidth={self.config.bandwidth}, code_rate={self.config.code_rate})")
    
    def __copy__(self) -> 'LoraPacket':
        return LoraPacket(
            payload=bytes(self.payload),
            tx_power=self.tx_power,
            config=self.config.copy(),
            snr=self.snr,
            rssi=self.rssi,
        )
