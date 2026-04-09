from enum import Enum


class OperatingMode(Enum):
    """LoRaWAN device operating mode """
    CLASS_A = "A"   # Baseline: uplink-initiated, two short RX windows after TX
    CLASS_B = "B"   # Beacon-synchronized periodic RX slots (future)
    CLASS_C = "C"   # Nearly continuous RX, except when transmitting
