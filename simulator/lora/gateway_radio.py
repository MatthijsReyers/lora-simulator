
from typing import Any, Iterable, List

from simulator.lora.enums.bandwidth import Bandwidth
from simulator.lora.enums.spreading_factor import SpreadingFactor
from simulator.lora.radio import LoraRadio, TooManyRxChainsException
from simulator.lora.radio_config import DEFAULT_FREQUENCY, LoraConfig
from simulator.lora.radio_power_profile import RadioPowerProfile, Stm32wl55PowerProfile
from simulator.lora.rx_chain import RxChain


class LoraGatewayRadio(LoraRadio):
    """
        A LoRa gateway concentrator, e.g. the Semtech SX1301/SX1302.

        Unlike the transceiver in an end device, a concentrator has several receive chains that
        run in parallel, letting the gateway demodulate packets on multiple channels (and with
        different spreading factors) at the same time. Each chain has its own frequency and
        modulation parameters and does its own collision bookkeeping, packets received by any of
        the chains end up in the same receive queue.

        Multi-chain behaviour is trivially turned off by constructing the radio with
        `max_chains=1`, in which case it behaves exactly like a `LoraClientRadio`.

        Note that transmission stays single-channel: like real gateways this radio has one
        transmitter, so it can only send on one frequency at a time (see `set_tx_config`).
    """

    __max_chains: int

    def __init__(
            self,
            position: tuple[float, float] = (0.0, 0.0),
            power_profile: RadioPowerProfile = Stm32wl55PowerProfile(),
            max_chains: int = 8,
            frequencies: Iterable[int]|None = None,
            spreading_factor: SpreadingFactor|int = SpreadingFactor.SF7,
            bandwidth: Bandwidth|int = Bandwidth.KHz125,
        ):
        """
            :param position: X,Y position of the gateway in meters, used to estimate path loss.
            :param power_profile: Power profile of the radio.
            :param max_chains: How many receive chains the concentrator has, the default of 8
                matches the number of channels of a typical SX1301 based gateway. Set this to 1
                for a single channel gateway.
            :param frequencies: Convenience parameter, when given a receive chain is set up for
                each of these frequencies (in hertz) using the spreading factor and bandwidth
                given below. Leave this empty to configure the chains yourself.
            :param spreading_factor: Spreading factor used for the chains created by `frequencies`.
            :param bandwidth: Bandwidth used for the chains created by `frequencies`.
        """
        assert max_chains >= 1, "A radio needs at least one receive chain"
        self.__max_chains = int(max_chains)

        super().__init__(position=position, power_profile=power_profile)

        if frequencies is not None:
            self.set_channel_plan(
                frequencies=frequencies,
                spreading_factor=spreading_factor,
                bandwidth=bandwidth,
            )


    @property
    def max_rx_chains(self) -> int:
        return self.__max_chains


    def add_rx_chain(self, frequency: int = DEFAULT_FREQUENCY, **kwargs: Any) -> int:
        """
            Adds an additional receive chain to the radio, the accepted keyword arguments are
            those of the `LoraConfig` constructor.

            :returns: The index of the newly added chain, use it with `set_rx_chain_config`.
        """
        if len(self._rx_chains) >= self.__max_chains:
            raise TooManyRxChainsException(self.__max_chains)

        chain_id = len(self._rx_chains)
        config = LoraConfig(frequency=frequency, **kwargs)
        self._rx_chains.append(RxChain(chain_id=chain_id, config=config))

        self.logger.debug(
            f"radio={self._radio_id} add_rx_chain() -> {self._rx_chains[chain_id]}"
        )

        return chain_id


    def set_rx_chain_config(self, chain: int, **kwargs: Any) -> None:
        """
            Reconfigures one of the receive chains, the accepted keyword arguments are those of
            the `LoraConfig` constructor. When no `frequency` is given the chain keeps the one it
            is currently tuned to.
        """
        self._set_chain_config(chain=chain, **kwargs)


    def enable_rx_chain(self, chain: int, enabled: bool = True) -> None:
        """
            Enables or disables one of the receive chains. A disabled chain does not receive
            anything and does not consume any power, but keeps its configuration so it can be
            switched back on later.
        """
        assert 0 <= chain < len(self._rx_chains), f"Radio has no receive chain {chain}"

        rx_chain = self._rx_chains[chain]
        rx_chain.enabled = enabled

        if not enabled:
            # Whatever the chain was busy receiving is lost when it is switched off.
            for meta in rx_chain.packets_in_transit.values():
                meta.interrupted = True


    def set_channel_plan(
            self,
            frequencies: Iterable[int],
            spreading_factor: SpreadingFactor|int = SpreadingFactor.SF7,
            bandwidth: Bandwidth|int = Bandwidth.KHz125,
            **kwargs: Any,
        ) -> None:
        """
            Configures one receive chain per given frequency, all of them using the same
            modulation parameters. This is the quick way to set up a gateway that listens to a
            whole channel plan, e.g. the three EU868 default uplink channels.

            Any chains beyond the given frequencies are disabled.
        """
        freqs: List[int] = list(frequencies)

        assert len(freqs) > 0, "Channel plan needs at least one frequency"
        if len(freqs) > self.__max_chains:
            raise TooManyRxChainsException(self.__max_chains)

        # Grow the number of chains to match the channel plan.
        while len(self._rx_chains) < len(freqs):
            self.add_rx_chain()

        for (chain, frequency) in enumerate(freqs):
            self._set_chain_config(
                chain=chain,
                frequency=frequency,
                spreading_factor=spreading_factor,
                bandwidth=bandwidth,
                **kwargs,
            )
            self._rx_chains[chain].enabled = True

        # Any leftover chains from a previous plan are switched off.
        for chain in range(len(freqs), len(self._rx_chains)):
            self.enable_rx_chain(chain, enabled=False)


    def rx_chain_for(self, frequency: int) -> int|None:
        """
            Finds the (first) enabled receive chain listening on the given frequency, returns None
            if the gateway is not listening to that frequency at all.
        """
        for chain in self._rx_chains:
            if chain.enabled and chain.config.frequency == frequency:
                return chain.chain_id
        return None
