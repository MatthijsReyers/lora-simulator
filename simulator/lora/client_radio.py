
from simulator.lora.enums.bandwidth import Bandwidth
from simulator.lora.enums.code_rate import CodeRate
from simulator.lora.enums.spreading_factor import SpreadingFactor
from simulator.lora.radio import LoraRadio
from simulator.lora.radio_config import LoraConfig


class LoraClientRadio(LoraRadio):
    """
        A LoRa transceiver as found in end devices, e.g. the Semtech SX126x used in the STM32WL.
        This class models radios with symmetric frontends with only one send and receive chain.

    """

    @property
    def max_rx_chains(self) -> int:
        return 1


    @property
    def rx_config(self) -> LoraConfig:
        """ The receive configuration of the radio's single receive chain. """
        return self._rx_chains[0].config.copy()


    @property
    def rx_frequency(self) -> int:
        """ The frequency the radio's single receive chain is tuned to, in hertz. """
        return self._rx_chains[0].config.frequency


    def set_channel(self, frequency: int) -> None:
        """
            Retunes the radio to a different carrier frequency, this changes both the transmit
            frequency and the frequency of the primary receive chain (mimicking the `SetChannel`
            call of most radio HALs).

            :param frequency: In hertz, the carrier frequency to use.
        """
        assert type(frequency) is int, "Frequency must be an integer number of hertz"
        assert frequency > 0, "Frequency must be positive"

        self.logger.debug(f"radio={self._radio_id} set_channel(frequency={frequency})")

        self._tx_config.frequency = frequency

        chain = self._rx_chains[0]
        chain.config.frequency = frequency

        # Retuning the radio kills whatever it was busy receiving.
        for meta in chain.packets_in_transit.values():
            meta.interrupted = True


    def set_rx_config(
        self,
        bandwidth: Bandwidth|int = Bandwidth.KHz125,
        spreading_factor: SpreadingFactor|int = SpreadingFactor.SF7,
        code_rate: CodeRate|int = CodeRate.CR4_5,
        preamble_len: int = 8,
        max_payload_len: int = 64,
        symbols: int = 0,
        fixed_payload_len: bool = False,
        crc_enabled: bool = True,
        iq_inverted: bool = False,
        rx_continuous: bool = True,
        frequency: int|None = None,
    ):
        """
            Sets the radio's receive configuration. This method is designed to mimic the RX config
            method of the STM32WLX5 HAL library and uses the same default values.

            :param bandwidth: LoRa bandwidth (e.g., 125 kHz, 250 kHz, 500 kHz)
            :param spreading_factor: LoRa spreading factor (e.g., SF7, SF8, SF9)
            :param code_rate: LoRa code rate (e.g., 4/5, 4/6, 4/7, 4/8) used by incoming packets
                note that this is only relevant when using implicit mode (i.e. when you set 
                `fixed_payload_len` to true), otherwise the packet header will indicate the used
                code rate.
            :param preamble_len: Preamble length in number of symbols
            :param max_payload_len: Longest payload length the radio should expect to receive
            :param symbols: Description
            :param fixed_payload_len: Does the radio expect fixed length payloads? I.e. will the 
                received packet have a header indicating their length, or is the length known 
                ahead of time? (Set expected length with `max_payload_len` parameter)
            :param crc_enabled: Should the radio expect incoming packets to have a CRC?
            :param iq_inverted: Description
            :param rx_continuous: Should the radio remain in receive mode until explicitly turned
                off?
            :param frequency: In hertz, the carrier frequency to listen on. When left empty the
                chain keeps whatever frequency it is currently tuned to, this mirrors real radio
                HALs where the RX/TX config calls do not touch the frequency and only an explicit
                `set_channel` retunes the radio.
        """
        assert type(rx_continuous) is bool, "RX continuous must be a boolean"
        self._rx_continuous = rx_continuous

        self._set_chain_config(
            chain=0,
            bandwidth=bandwidth,
            spreading_factor=spreading_factor,
            code_rate=code_rate,
            preamble_len=preamble_len,
            payload_len=max_payload_len,
            symbols=symbols,
            fixed_payload_len=fixed_payload_len,
            crc_enabled=crc_enabled,
            iq_inverted=iq_inverted,
            frequency=frequency,
        )