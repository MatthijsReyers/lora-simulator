from enum import IntEnum


class MType(IntEnum):
    """MAC header message type (3 bits, §4.2.1)."""
    JOIN_REQUEST        = 0b000
    JOIN_ACCEPT         = 0b001
    UNCONFIRMED_DATA_UP = 0b010
    UNCONFIRMED_DATA_DN = 0b011
    CONFIRMED_DATA_UP   = 0b100
    CONFIRMED_DATA_DN   = 0b101
    # 0b110 and 0b111 are RFU (Reserved for Future Use)

    @property
    def is_uplink(self) -> bool:
        return self in (MType.JOIN_REQUEST, MType.UNCONFIRMED_DATA_UP, MType.CONFIRMED_DATA_UP)

    @property
    def is_confirmed(self) -> bool:
        return self in (MType.CONFIRMED_DATA_UP, MType.CONFIRMED_DATA_DN)


class Major(IntEnum):
    """Major version of the LoRaWAN frame format (2 bits, §4.2.1)."""
    LORAWAN_R1 = 0b00
