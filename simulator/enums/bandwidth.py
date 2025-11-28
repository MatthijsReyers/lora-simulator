from enum import IntEnum

class Bandwidth(IntEnum):
    KHz125 = 0
    KHz250 = 1
    KHz500 = 2
    Reserved = 3

    def __str__(self):
        return self.name
