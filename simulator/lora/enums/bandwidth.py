from enum import IntEnum

class Bandwidth(IntEnum):
    KHz125 = 0
    KHz250 = 1
    KHz500 = 2
    Reserved = 3

    def __str__(self):
        return self.name

    @staticmethod
    def from_value(value: int) -> 'Bandwidth':
        match value:
            case 125:
                return Bandwidth.KHz125
            case 250:
                return Bandwidth.KHz250
            case 500:
                return Bandwidth.KHz500
        raise ValueError(f"Bandwidth of {value}hz is not supported by LoRa.")
