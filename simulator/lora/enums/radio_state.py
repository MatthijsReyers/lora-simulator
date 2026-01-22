from enum import IntEnum

class RadioState(IntEnum):
    OFF = 1
    STANDBY = 2
    RX = 3
    TX = 4

    def __str__(self):
        return self.name
