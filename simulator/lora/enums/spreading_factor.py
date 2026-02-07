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
    
    def minimum_snr(self) -> float:
        """ Returns the minimum SNR (in dB) required for this spreading factor. """
        sf_snr_map = {
            SpreadingFactor.SF6: -5.0,
            SpreadingFactor.SF7: -7.5,
            SpreadingFactor.SF8: -10.0,
            SpreadingFactor.SF9: -12.5,
            SpreadingFactor.SF10: -15.0,
            SpreadingFactor.SF11: -17.5,
            SpreadingFactor.SF12: -20.0,
        }
        return sf_snr_map[self]
