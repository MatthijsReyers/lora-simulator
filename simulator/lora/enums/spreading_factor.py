from enum import IntEnum

class SpreadingFactor(IntEnum):
    # Umm, pretty sure SF6 does not exist but the radio docs claim to support it?
    SF6 = 6
    SF7 = 7
    SF8 = 8
    SF9 = 9
    SF10 = 10
    SF11 = 11
    SF12 = 12

    def __str__(self):
        return self.name
