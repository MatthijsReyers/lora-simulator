from enum import IntEnum

class RadioState(IntEnum):
    OFF = 0

    RX = 1
    RX_DONE = 2
    RX_TIMEOUT = 3
    RX_ERROR = 3

    TX = 4
    TX_DONE = 5
    TX_TIMEOUT = 6

    def __str__(self):
        return self.name
