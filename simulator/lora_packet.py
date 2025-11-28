
class LoraPacket:
    def __init__(self, payload: bytes, frequency: float, spreading_factor: int, bandwidth: int):
        self.payload = payload
        self.frequency = frequency
        self.spreading_factor = spreading_factor
        self.bandwidth = bandwidth

    def __repr__(self):
        return (f"LoRaPacket(payload={self.payload}, frequency={self.frequency}, "
                f"spreading_factor={self.spreading_factor}, bandwidth={self.bandwidth})")
    
    