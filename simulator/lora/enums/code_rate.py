from enum import IntEnum

class CodeRate(IntEnum):
    """ 
        LoRa Code Rate Enum. Note that the values correspond to the config value used in the
        STM32WLX5 HAL library and not the denominator of the code rate fraction. (You can use
        the from_denominator static method to convert from denominator to enum value.)
    """
    CR4_5 = 1
    CR4_6 = 2
    CR4_7 = 3
    CR4_8 = 4

    def __str__(self):
        return self.name

    @staticmethod
    def from_denominator(value: int) -> 'CodeRate':
        match value:
            case 5:
                return CodeRate.CR4_5
            case 6:
                return CodeRate.CR4_6
            case 7:
                return CodeRate.CR4_7
            case 8:
                return CodeRate.CR4_8
            case _:
                raise ValueError(f"Invalid CodeRate denominator: {value}")
            
