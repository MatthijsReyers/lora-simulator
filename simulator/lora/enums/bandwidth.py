from enum import IntEnum

class Bandwidth(IntEnum):
    KHz125 = 0
    KHz250 = 1
    KHz500 = 2
    Reserved = 3

    def __str__(self):
        return self.name

    def to_hz(self) -> int:
        match self:
            case Bandwidth.KHz125 | 0:
                return 125_000
            case Bandwidth.KHz250 | 1:
                return 250_000
            case Bandwidth.KHz500 | 2:
                return 500_000

    @staticmethod
    def from_hz(hz: int) -> 'Bandwidth':
        match hz:
            case 125_000:
                return Bandwidth.KHz125
            case 250_000:
                return Bandwidth.KHz250
            case 500_000:
                return Bandwidth.KHz500
        raise ValueError(f"Bandwidth of {hz}hz is not supported by LoRa.")
    
    @staticmethod
    def from_khz(khz: int) -> 'Bandwidth':
        match khz:
            case 125:
                return Bandwidth.KHz125
            case 250:
                return Bandwidth.KHz250
            case 500:
                return Bandwidth.KHz500
        raise ValueError(f"Bandwidth of {khz}khz is not supported by LoRa.")
    