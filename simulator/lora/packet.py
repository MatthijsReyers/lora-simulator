
from typing import Optional, Tuple
from simulator.lora.distance import distance
from simulator.lora.enums.path_loss.log_distance_path_loss import log_distance_path_loss
from simulator.lora.radio_config import LoraConfig
from simulator.lora.airtime import estimate_airtime

# Global packet counter for unique packet IDs
_packet_id_counter = 0

class LoraPacket:
    def __init__(
            self, 
            payload: bytes, 
            tx_power: int,
            tx_location: Tuple[float, float],
            config: LoraConfig,
            snr: float = None,
            rssi: float = None,
            airtime: float = None,
            packet_id: Optional[int] = None,
        ):
        assert isinstance(config, LoraConfig), "config must be a LoraConfig object"
        assert isinstance(payload, bytes), "Payload must be of type bytes"
        assert type(tx_power) is int, "tx_power must be an integer"

        if not packet_id:
            global _packet_id_counter
            _packet_id_counter += 1
            self.id = _packet_id_counter
        else:
            self.id = packet_id
        
        self.config = config
        self.payload = payload
        self.tx_power = tx_power
        self.tx_location = tx_location
        self.rx_location = None
        self.__snr = snr
        self.__rssi = rssi

        # Lazy properties
        self.__airtime = airtime

    def __repr__(self):
        return (f"LoRaPacket(id={self.id}, payload={self.payload}, spreading_factor={self.config.spreading_factor}, bandwidth={self.config.bandwidth}, code_rate={self.config.code_rate})")
    
    def __deepcopy__(self, _memo) -> 'LoraPacket':
        return LoraPacket(
            payload=self.payload,
            tx_power=self.tx_power,
            tx_location=self.tx_location,
            config=self.config.copy(),
            snr=self.__snr,
            rssi=self.__rssi,
            packet_id=self.id
        )

    @property
    def rssi(self) -> float:
        """ Estimated RSSI of the packet in dBm. """
        assert self.rx_location is not None, "rx_location must be set to estimate RSSI" 

        # Prevent circular import
        from simulator.lora.phy_layer import LoraPhyLayer
        if self.__rssi is not None:
            return self.__rssi
        phy = LoraPhyLayer()
        dis = distance(self.tx_location, self.rx_location)
        self.__rssi = self.tx_power - phy.path_loss_estimator(
            distance=dis,
            frequency=self.config.bandwidth.to_hz()
        )
        return self.__rssi

    @property
    def snr(self) -> float:
        """ Estimated SNR of the packet in dB. """
        # Prevent circular import
        from simulator.lora.phy_layer import LoraPhyLayer
        if self.__snr is not None:
            return self.__snr
        phy = LoraPhyLayer()
        self.__snr = self.rssi - phy.noise_floor
        return self.__snr

    @property
    def airtime(self) -> float:
        """ Estimates the total airtime of the packet in seconds. """
        # Airtime is cached after first calculation since its kind of expensive to compute
        if self.__airtime is not None:
            return self.__airtime
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
        self.__airtime = airtime
        return airtime


