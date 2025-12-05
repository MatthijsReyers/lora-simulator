from enum import IntEnum

class CodeRate(IntEnum):
    CR4_5 = 1
    CR4_6 = 2
    CR4_7 = 3
    CR4_8 = 4

    def __str__(self):
        return self.name
