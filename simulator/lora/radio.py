from abc import ABC, abstractmethod
import asyncio, logging
from typing import Any, Dict, Iterable, List, Optional, Tuple
import pandas as pd
import random

from simulator.exceptions import SimulatorException
from simulator.lora.airtime import symbol_airtime
from simulator.lora.enums.code_rate import CodeRate
from simulator.lora.enums.spreading_factor import SpreadingFactor
from simulator.lora.enums.bandwidth import Bandwidth
from simulator.lora.enums.radio_state import RadioState
from simulator.lora.packet import LoraPacket
from simulator.environment import simulation_env as sim
from simulator.lora.packet_metadata import PacketMetadata
from simulator.lora.radio_config import LoraConfig
from simulator.lora.radio_power_profile import RadioPowerProfile, Stm32wl55PowerProfile
from simulator.lora.rx_chain import RxChain
from simulator.power_consumer import PowerConsumer
from simulator.queue import Queue

# Global radio counter for unique radio IDs
_radio_id_counter = 0

class AlreadyTransmittingException(SimulatorException):
    """Exception raised when attempting to transmit while already transmitting."""
    def __init__(self):
        super().__init__(
            "Radio is already transmitting."
        )

class TooManyRxChainsException(SimulatorException):
    """Exception raised when adding more receive chains than the radio supports."""
    def __init__(self, max_chains: int):
        super().__init__(
            f"Radio only supports {max_chains} receive chain(s)."
        )

class LoraRadio(ABC):
    """
        Abstract base class shared by all simulated LoRa radios.
        This class implements everything that end device radios and gateway concentrators have in
        common: the radio state machine, power consumption bookkeeping, transmission and the
        reception/collision model. 

        - `LoraClientRadio` has exactly one receive chain, just like the SX126x style transceivers
          found in end devices.
        - `LoraGatewayRadio` has several chains that demodulate different channels and spreading
          factors in parallel, like an SX1301/SX1302 concentrator.
    """
    __RECEIVE_PROCESS_DELAY = 0.0001

    __radio_state: RadioState
    __rx_queue: Queue[Tuple[LoraPacket, PacketMetadata]]

    _rx_chains: List[RxChain]
    _rx_continuous: bool = True

    _tx_config: LoraConfig
    _tx_power: int = 14  # in dBm
    # __tx_freq_hop_enable: bool = False
    # __tx_freq_hop_period: int = 0
    # __tx_timeout: int = 3_000  # in milliseconds

    _radio_id: int

    position: tuple[float, float]
    logger: logging.Logger
    power_consumer: PowerConsumer

    __state_log: List[Tuple[float, RadioState]]
    __packets_log: Dict[str, Any]


    def __init__(
            self, 
            position: tuple[float, float] = (0.0, 0.0), 
            power_profile: RadioPowerProfile = Stm32wl55PowerProfile(),
        ):
        self.__rx_queue = Queue()
        self.__idle_waiters: list[asyncio.Event] = []
        self.position = position
        self.__radio_state = RadioState.OFF
        self._tx_config = LoraConfig()

        # Every radio has at least one receive chain, radios that support more add them
        # themselves.
        self._rx_chains = [RxChain(chain_id=0, config=LoraConfig())]

        global _radio_id_counter
        _radio_id_counter += 1
        self._radio_id = _radio_id_counter

        self.logger = logging.getLogger(f"LoraRadio-{self._radio_id}")

        self.power_consumer = PowerConsumer()
        self.power_profile = power_profile

        self.__state_log = []
        self.__packets_log = {
            "id": [],
            "time": [],
            "chain": [],
            "frequency": [],
            "snr": [],
            "rssi": [],
            "collision": [],
            "missed_start": [],
            "missed_end": [],
            "interrupted": [],
        }

        # Prevents circular import
        from simulator.lora.phy_layer import LoraPhyLayer
        self.phy = LoraPhyLayer()
        self.phy.subscribe(self)

        sim.create_task(self.__on_sim_end())


    @property
    @abstractmethod
    def max_rx_chains(self) -> int:
        """
            The number of receive chains this radio supports. Client radios return 1, gateway
            concentrators return however many chains they were configured with.
        """
        ...


    @property
    def rx_chains(self) -> List[RxChain]:
        """
            All receive chains of this radio.
        """
        return list(self._rx_chains)

    @property
    def rx_continuous(self) -> bool:
        return self._rx_continuous

    @property
    def tx_config(self) -> LoraConfig:
        return self._tx_config.copy()

    @property
    def tx_power(self) -> int:
        return self._tx_power

    @property
    def tx_frequency(self) -> int:
        """ The frequency the radio transmits on, in hertz. """
        return self._tx_config.frequency


    async def __on_sim_end(self):
        await sim.wait_for_sim_end()
        self.packets_log = pd.DataFrame(self.__packets_log)


    def get_state(self) -> RadioState:
        return self.__radio_state


    def _rx_power_consumption(self) -> float:
        """
            The power consumed while the radio is in receive mode. Every enabled receive chain is
            its own demodulator drawing its own power, so a concentrator listening on eight
            channels burns roughly eight times the power of a single chain receiver.
        """
        return sum(
            self.power_profile.rx_power(chain.config)
            for chain in self._rx_chains if chain.enabled
        )


    def _set_state(self, state: RadioState) -> None:
        self.logger.debug(f"radio={self._radio_id} set_state(state={state})")
        self.__radio_state = state
        self.__state_log.append((sim.current_time(), state))
        match state:
            case RadioState.OFF:
                power = self.power_profile.disabled_power()
            case RadioState.RX:
                power = self._rx_power_consumption()
            case RadioState.TX:
                power = self.power_profile.tx_power(self._tx_power, self._tx_config)
            case RadioState.STANDBY:
                power = self.power_profile.standby_power()
        self.power_consumer.set_power_consumption(power)


    async def standby(self) -> None:
        """
            Puts the radio into standby mode.
        """
        self.logger.debug(f"radio={self._radio_id} standby()")
        if self.__radio_state != RadioState.STANDBY:
            self._set_state(RadioState.STANDBY)
            await sim.sleep(self.power_profile.standby_startup_time())


    async def off(self) -> None:
        """
            Turns the radio off.
        """
        self.logger.debug(f"radio={self._radio_id} off()")
        if self.__radio_state != RadioState.OFF:
            self._set_state(RadioState.OFF)


    async def receive(self, continuous: bool = True) -> None:
        """
            Puts the radio into receive mode without blocking or waiting for packets. The other
            blocking receive methods already put the radio into receive mode so this method is only
            needed to put the radio into receive mode after transmitting data.
        """
        self.logger.debug(f"radio={self._radio_id} receive(continuous={continuous})")

        if self.__radio_state == RadioState.TX:
            raise RuntimeError("Cannot enter receive mode while radio is transmitting.")

        if self.__radio_state == RadioState.OFF:
            await self.standby()

        self._set_state(RadioState.RX)
        self._rx_continuous = continuous


    async def carrier_sense(self, chain: int = 0) -> bool:
        """
            Performs carrier sensing to determine if the channel is currently busy.

            :param chain: Which receive chain (i.e. which channel) to sense on.
            :returns: True if the channel is busy, False otherwise.
        """
        self.logger.debug(f"radio={self._radio_id} carrier_sense(chain={chain})")

        old_state = self.__radio_state

        if self.__radio_state == RadioState.OFF:
            await self.standby()
        if self.__radio_state == RadioState.TX:
            raise RuntimeError("Cannot perform carrier sensing while radio is transmitting.")
        if self.__radio_state != RadioState.RX:
            self._set_state(RadioState.RX)

        config = self._rx_chains[chain].config
        sense_time = symbol_airtime(config.spreading_factor, config.bandwidth) * 6
        await sim.sleep(sense_time)

        medium_busy = self.carrier_sense_instant(chain=chain)

        # Go back to whatever state the radio was in before carrier sensing started.
        self._set_state(old_state)

        return medium_busy
    

    def carrier_sense_instant(self, chain: int|None = None) -> bool:
        """
            Unrealistic ideal instantaneous carrier sense that just checks if there are any packets
            currently being received with matching parameters. This is not how real carrier sensing
            works but it can be useful for testing or implementing idealized protocols that assume
            perfect carrier sensing.

            :param chain: Which receive chain (i.e. which channel) to sense on, when left empty the
                channel counts as busy if *any* of the radio's chains is busy.
            :returns: True if the channel is busy, False otherwise.
        """
        for rx_chain in self.__selected_chains(chain):
            for meta in rx_chain.packets_in_transit.values():
                if (
                    meta.packet.config.spreading_factor == rx_chain.config.spreading_factor and 
                    meta.packet.config.bandwidth == rx_chain.config.bandwidth and
                    meta.packet.config.frequency == rx_chain.config.frequency
                ):
                    return True
        return False


    def __selected_chains(self, chain: int|None) -> Iterable[RxChain]:
        """ Either one specific receive chain or all of the enabled ones. """
        if chain is not None:
            return [self._rx_chains[chain]]
        return [c for c in self._rx_chains if c.enabled]


    async def wait_for_channel_idle(self, chain: int|None = None) -> None:
        """
            Block until the channel is idle (no matching packets in transit).

            This is an event-driven wait — it does NOT poll. When the last
            in-transit packet finishes, all waiters are woken on the same
            simulation tick. This is useful for modelling idealized CSMA
            protocols with zero propagation delay (a ≈ 0).
        """
        while self.carrier_sense_instant(chain=chain):
            event = asyncio.Event()
            self.__idle_waiters.append(event)
            await sim.schedule_event_wait(event, sim.last_tick())
            # Clean up in case we were woken by the simulation-end timeout
            # rather than by the idle notification in _on_receive_end.
            if event in self.__idle_waiters:
                self.__idle_waiters.remove(event)


    async def receive_data_nowait(
            self, metadata: bool = False
        ) -> Optional[LoraPacket | Tuple[LoraPacket, PacketMetadata]]:
        """
            Tries to receive data from the modem if it is available, immediately returns None if no
            data is available in the receive queue. Note that this call does not put the radio in
            receive mode.

            :param metadata: If true, also returns the PacketMetadata along with the LoraPacket.
        """
        self.logger.debug(f"radio={self._radio_id} receive_data()")

        try:
            (packet, meta) = await self.__rx_queue.get_timeout(0)
            await sim.sleep(self.__RECEIVE_PROCESS_DELAY)
            if metadata: 
                return (packet, meta)
            return packet
        except asyncio.QueueEmpty:
            return None


    async def receive_data_wait(
            self, metadata: bool = False
        ) -> LoraPacket | Tuple[LoraPacket, PacketMetadata]:
        """
            Blocking wait that does not return until some data is received (correctly) by the radio.
            Note that if a packet is already in the reception queue it does not put the radio in 
            receive mode.

            :param metadata: If true, also returns the PacketMetadata along with the LoraPacket.
        """
        self.logger.debug(f"radio={self._radio_id} receive_data_wait()")

        # Has a packet already been received?
        try: 
            (packet, meta) = await self.__rx_queue.get_timeout(0)
            if metadata: 
                return (packet, meta)
            return packet
        except asyncio.TimeoutError: pass
        
        if self.__radio_state == RadioState.TX:
            raise RuntimeError("Cannot receive data while radio is transmitting.")

        if self.__radio_state == RadioState.OFF:
            await self.standby()

        if self.__radio_state != RadioState.RX:
            self._set_state(RadioState.RX)
        
        (packet, meta) = await self.__rx_queue.get()
        await sim.sleep(self.__RECEIVE_PROCESS_DELAY)
        if metadata: 
            return (packet, meta)
        return packet


    async def receive_data_within(
            self, timeout: float, metadata: bool = False
        ) -> LoraPacket | Tuple[LoraPacket, PacketMetadata]:
        """
            Waits for the given amount of time until some data is received over the radio and
            throws a TimeoutError if no data is received within that time.

            :param timeout: In seconds, time to wait for a packet to be received.
            :param metadata: If true, also returns the PacketMetadata along with the LoraPacket.
        """
        self.logger.debug(f"receive_data_within(timeout={timeout})")

        # Has a packet already been received?
        try: 
            (packet, meta) = await self.__rx_queue.get_timeout(0)
            if metadata: 
                return (packet, meta)
            return packet
        except asyncio.TimeoutError: pass

        if self.__radio_state == RadioState.TX:
            raise RuntimeError("Cannot receive data while radio is transmitting.")

        if self.__radio_state == RadioState.OFF:
            await self.standby()

        if self.__radio_state != RadioState.RX:
            self._set_state(RadioState.RX)

        await sim.sleep(self.__RECEIVE_PROCESS_DELAY)

        self.logger.debug(f"{sim.current_time():.4f} radio={self._radio_id} waiting for packet with timeout={timeout}s")
        (packet, meta) = await self.__rx_queue.get_timeout(timeout)
        if metadata: 
            return (packet, meta)
        return packet


    async def transmit_data(self, data: bytes) -> asyncio.Event:
        """ 
            Transmit data asynchronously. Returns an asyncio.Event that is set when the
            transmission is complete. Throws AlreadyTransmittingException if the radio is already
            currently transmitting.
        """
        assert isinstance(data, bytes), "Data to transmit must be bytes"

        if self.__radio_state == RadioState.TX:
            raise AlreadyTransmittingException()

        tx_finished_event = asyncio.Event()

        async def transmit_task():
            await self.transmit_data_blocking(data)
            tx_finished_event.set()

        await sim.start_child_task(transmit_task())

        return tx_finished_event


    async def transmit_data_blocking(self, data: bytes):
        """
            Transmit data to other radios, This call does not return until the transmission is
            complete. Throws AlreadyTransmittingException if the radio is already currently
            transmitting (via non-blocking method).
        """
        assert isinstance(data, bytes), "Data to transmit must be bytes"

        if self.__radio_state == RadioState.TX:
            raise AlreadyTransmittingException()

        # TODO: Technically one could keep track of preambles and delay TX until RX is done, but
        # that's not how most radios work so for now we just interrupt any ongoing receptions.
        if self.__radio_state == RadioState.RX:
            for rx_chain in self._rx_chains:
                for meta in rx_chain.packets_in_transit.values():
                    meta.interrupted = True

        if self.__radio_state == RadioState.OFF:
            await self.standby()

        self._set_state(RadioState.TX)

        packet = LoraPacket(
            payload=data,
            tx_power=self._tx_power,
            tx_location=self.position,
            config=self._tx_config.copy(),
        )

        # Prevents circular import
        from simulator.lora.phy_layer import LoraPhyLayer

        phy_layer = LoraPhyLayer()
        airtime = await phy_layer.transmit_packet_blocking(self, packet)

        if self._rx_continuous:
            self._set_state(RadioState.RX)
        else:
            await self.standby()
        
        return airtime



    def _set_chain_config(self, chain: int, **kwargs: Any) -> None:
        """
            Reconfigures one of the radio's receive chains, accepts the same keyword arguments as
            the `LoraConfig` constructor (which also validates them).
        """
        assert 0 <= chain < len(self._rx_chains), f"Radio has no receive chain {chain}"
        rx_chain = self._rx_chains[chain]
        # Like on real radios, reconfiguring the modulation parameters does not retune the chain:
        # when no frequency is given the chain stays on the one it is currently listening to.
        if kwargs.get('frequency') is None:
            kwargs['frequency'] = rx_chain.config.frequency
        rx_chain.config = LoraConfig(**kwargs)
        # Changing the chain config messes up the reception of all packets it had in transit.
        for meta in rx_chain.packets_in_transit.values():
            meta.interrupted = True


    def set_tx_config(
        self,
        power: int,
        bandwidth: Bandwidth|int = Bandwidth.KHz125,
        spreading_factor: SpreadingFactor|int = SpreadingFactor.SF7,
        code_rate: CodeRate|int = CodeRate.CR4_5,
        preamble_len: int = 8,
        fixed_len: bool = False,
        crc_enable: bool = True,
        freq_hop_enable: bool = False,
        freq_hop_period: int = 0,
        iq_inverted: bool = False,
        timeout: int = 3_000,
        frequency: int|None = None,
    ):
        """
            Sets the radio's transmit configuration, mimicking the TX config call of the
            STM32WLX5 HAL library.
        """
        assert type(freq_hop_enable) is bool, "Frequency hop enable must be a boolean"
        assert freq_hop_period >= 0, "Frequency hop period must be non-negative"
        assert timeout >= 0, "Timeout must be non-negative"

        # Like on real radios, reconfiguring does not retune the transmitter unless an explicit
        # frequency is given, see the docstring above.
        if frequency is None:
            frequency = self._tx_config.frequency

        if freq_hop_enable:
            raise NotImplementedError("Frequency hopping is not yet implemented.")

        self._tx_power = power
        self._tx_config = LoraConfig(
            bandwidth=bandwidth,
            spreading_factor=spreading_factor,
            code_rate=code_rate,
            preamble_len=preamble_len,
            fixed_payload_len=fixed_len,
            crc_enabled=crc_enable,
            iq_inverted=iq_inverted,
            frequency=frequency,
        )
        self.__tx_freq_hop_enable = freq_hop_enable
        self.__tx_freq_hop_period = freq_hop_period
        self.__tx_timeout = timeout


    def _on_receive_start(self, packet: LoraPacket):
        """
            Called when the radio begins receiving a packet. This is a callback function used by the 
            PHY layer to notify the radio of incoming packets, end users should never be calling
            this directly.
        """
        self.logger.debug(f"{sim.current_time():.4f} radio={self._radio_id} _on_receive_start(packet={packet.id}, rssi={packet.rssi})")

        for rx_chain in self._rx_chains:

            if not rx_chain.enabled:
                continue

            # A chain is completely oblivious to signals outside of the spectrum it listens to,
            # such packets neither get received nor cause any interference on this chain.
            if not rx_chain.config.overlaps(packet.config):
                continue

            # Is the radio turned on and able to receive the packets?
            missed_start = not self.__can_receive(packet, rx_chain)

            # Does this packet overlap with any other packets currently being sent?
            possible_collisions = rx_chain.packets_in_transit.values()

            # Is the radio able to distinguish this packet from the noise floor?
            required = rx_chain.config.spreading_factor.minimum_snr()
            demodulate_failure = packet.snr < required

            # Overlapping packets do not have to collide if they use different enough parameters
            found_collision = False
            for meta in possible_collisions:
                (fst, snd) = self.__capture_effect(meta.packet, packet)
                if not fst:
                    meta.collision = True
                if not snd:
                    found_collision = True

            # Add this packet to the list of packets in transit on this chain
            rx_chain.packets_in_transit[packet.id] = PacketMetadata(   
                packet=packet,
                collision=found_collision,
                missed_start=missed_start,
                demodulate_failure=demodulate_failure,
                chain_id=rx_chain.chain_id,
            )


    async def _on_receive_preamble(self, packet_id: int):
        """
            Called when the radio detects the preamble of a packet.
        """
        self.logger.debug(f"{sim.current_time():.4f} radio={self._radio_id} _on_receive_preamble({packet_id})")
        # TODO: cancel RX timeout for non-continuous RX mode after preamble is detected
        for rx_chain in self._rx_chains:
            metadata = rx_chain.packets_in_transit.get(packet_id)
            if metadata is None:
                # Packet is outside of this chain's part of the spectrum.
                continue
            metadata.received_preamble = (
                not metadata.missed_start and 
                not metadata.demodulate_failure and 
                not metadata.collision and
                not metadata.interrupted
            )


    async def _on_receive_end(self, packet_id: int):
        """
            Called when the radio finishes receiving a whole packet.
        """
        self.logger.debug(f"{sim.current_time():.4f} radio={self._radio_id} _on_receive_end({packet_id})")

        delivered = False

        for rx_chain in self._rx_chains:

            # Remove packet from in transit list
            metadata = rx_chain.packets_in_transit.pop(packet_id, None)
            if metadata is None:
                # Packet is outside of this chain's part of the spectrum.
                continue

            metadata.missed_end = (not self.__can_receive(metadata.packet, rx_chain))

            # Log packet metadata for later analysis
            self.__log_packet_metadata(metadata)

            # Did we successfully receive the whole packet?
            if metadata.received_successfully() and not delivered:
                self.logger.debug(f"putting packet into rx queue: {metadata.packet}")
                delivered = True
                await self.__rx_queue.put((metadata.packet, metadata))

                # Turn off the radio if it was not in continuous receive mode
                if self.__radio_state == RadioState.RX and not self._rx_continuous:
                    self._set_state(RadioState.OFF)

            elif not metadata.received_successfully():
                self.logger.debug(f"radio={self._radio_id} packet lost due to {metadata}")

        # Notify any coroutines waiting for the channel to become idle.
        if self.__idle_waiters and not self.carrier_sense_instant():
            for waiter in self.__idle_waiters:
                await sim.schedule_event_no_await(waiter, sim.next_tick())
            self.__idle_waiters.clear()


    def __can_receive(self, packet: LoraPacket, rx_chain: RxChain) -> bool:
        """
            Determines whether the given receive chain can receive the given packet based on its
            current configuration.
        """
        # Is the radio even in receive mode?
        if self.__radio_state != RadioState.RX: 
            return False

        rx_config = rx_chain.config

        # Note that a chain only ever gets to see packets that overlap with its own spectrum, but
        # overlapping is not the same as being tuned to the exact same channel.
        if not rx_config.same_channel(packet.config):
            self.logger.info(
                f"radio={self._radio_id} chain={rx_chain.chain_id} cannot receive packet \
                    {packet.id} due to channel mismatch: {packet.config.frequency}hz \
                    @{packet.config.bandwidth} != {rx_config.frequency}hz @{rx_config.bandwidth}"
            )
            return False
        
        if packet.config.code_rate != rx_config.code_rate:
            self.logger.info(
                f"radio={self._radio_id} cannot receive packet {packet.id} due to code rate \
                    mismatch: {packet.config.code_rate} != {rx_config.code_rate}"
            )
            return False
        
        if packet.config.spreading_factor != rx_config.spreading_factor:
            self.logger.info(
                f"radio={self._radio_id} cannot receive packet {packet.id} due to SF \
                    mismatch: {packet.config.spreading_factor} != {rx_config.spreading_factor}"
            )
            return False

        if packet.config.crc_enabled != rx_config.crc_enabled:
            self.logger.info(
                f"radio={self._radio_id} cannot receive packet {packet.id} due to CRC \
                    enabled mismatch: {packet.config.crc_enabled} != {rx_config.crc_enabled}"
            )
            return False
        
        if packet.config.fixed_payload_len != rx_config.fixed_payload_len:
            self.logger.info(
                f"radio={self._radio_id} cannot receive packet {packet.id} due to fixed \
                    length mode mismatch: {packet.config.fixed_payload_len} != {rx_config.fixed_payload_len}"
            )
            return False
        
        if packet.config.preamble_len != rx_config.preamble_len:
            self.logger.info(
                f"radio={self._radio_id} cannot receive packet {packet.id} due to \
                    preamble length mismatch: {packet.config.preamble_len} != {rx_config.preamble_len}"
            )
            return False
        
        if packet.config.iq_inverted != rx_config.iq_inverted:
            self.logger.info(
                f"radio={self._radio_id} cannot receive packet {packet.id} due to IQ \
                    inversion mismatch: {packet.config.iq_inverted} != {rx_config.iq_inverted}"
            )
            return False
        
        # In implicit mode, we need to check more things since the packet does not have a header to
        # indicate its parameters.
        if rx_config.fixed_payload_len:
            
            if len(packet.payload) != rx_config.payload_len:
                self.logger.info(
                    f"radio={self._radio_id} cannot receive packet {packet.id} due to \
                        payload length mismatch: {len(packet.payload)} != {rx_config.payload_len}"
                )
                return False
            
            if packet.config.code_rate != rx_config.code_rate:
                self.logger.info(
                    f"radio={self._radio_id} cannot receive packet {packet.id} due to \
                        code rate mismatch: {packet.config.code_rate} != {rx_config.code_rate}"
                )
                return False
        
        return True
    

    def __capture_effect(self, first: LoraPacket, second: LoraPacket) -> Tuple[bool, bool]:
        """
            Determines whether the second packet collides with the first packet based on their 
            TX parameters. The returned booleans indicate for each of the two packets whether it
            survives the overlap, i.e. `(True, False)` means the first packet can still be
            demodulated but the second one is lost.

            Note: this method assumes that packets do actually overlap in time.
        """
        # Signals that do not overlap in spectrum do not interfere with each other, so both of
        # them survive. This is the case for packets on different channels, but also for packets
        # on the same frequency using different bandwidths.
        if not first.config.overlaps(second.config):
            return (True, True)
        
        # Traditional SEMTech collision rules if one packet is this much stronger than the other
        # we can definitely demodulate the stronger packet.
        # https://privatevideos.hubs.vidyard.com/watch/iXBL8d2mjyjubK8DhuGcKq
        power_delta = first.snr - second.snr
        if first.config.spreading_factor == second.config.spreading_factor:
            if power_delta > 6.0:
                return (True, False)
        else:
            snir = first.config.spreading_factor.minimum_snr()
            if power_delta > snir:
                return (True, False)
            
        # Detailed capture effect calculations can be disabled for performance or when not needed,
        # in which case we just assume all overlapping packets are lost.
        if not self.phy.enable_capture_effect:
            return (False, False)

        uninterrupted_t = first.symbol_t * 6

        # See: LoRa Scalability: A Simulation Model Based on Interference Measurements (2017)
        # Measurements show that the probability of the second packet being received successfully
        # relates direct to how much of the preamble of the second packet is received without 
        # interference from the first packet.
        p1_end = first.tx_start + first.airtime
        p2_start = second.tx_start

        interfered_t = p1_end - (p2_start + uninterrupted_t)
        interfered_ratio = interfered_t / uninterrupted_t
        snd = interfered_ratio < random.random()

        # Is the first packet stronger or equal in reception to the second?
        if first.rssi >= second.rssi:

            # At least 6 symbols of the preamble of the first packet must be received without collision
            # in order to still have any chance of demodulating the first packet.
            # See: LoRa Scalability: A Simulation Model Based on Interference Measurements (2017)
            if first.tx_start + uninterrupted_t < second.tx_start:
                fst = random.random() < 0.95  # 95% chance to receive the first packet successfully
                return (fst, snd)
            else:
                return (False, snd)
            
        # Second packet is stronger than the first.
        if snd: return (False, True)

        # The second packet may be received if it interrupts the header and causes the header
        # CRC of the first packet to fail, giving the radio a chance to sync on the second packet 
        # instead of the first.
        if not first.config.fixed_payload_len:

            header_start = first.tx_start + first.preamble_airtime
            header_end = header_start + first.header_airtime
            if second.tx_start < header_end:

                # Same calculation as before now, but we use the end of the header as the "end" of
                # the first packet..
                interfered_t = header_end - (p2_start + uninterrupted_t)
                interfered_ratio = interfered_t / uninterrupted_t
                snd = interfered_ratio < random.random()

                return (False, snd)
            
        return (False, snd)


    def __log_packet_metadata(self, metadata: PacketMetadata):
        """
            Logs the metadata of a received packet for later analysis.
        """
        self.__packets_log["id"].append(metadata.packet.id)
        self.__packets_log["time"].append(sim.current_time())
        self.__packets_log["chain"].append(metadata.chain_id)
        self.__packets_log["frequency"].append(metadata.packet.config.frequency)
        self.__packets_log["snr"].append(metadata.packet.snr)
        self.__packets_log["rssi"].append(metadata.packet.rssi)
        self.__packets_log["collision"].append(metadata.collision)
        self.__packets_log["missed_start"].append(metadata.missed_start)
        self.__packets_log["missed_end"].append(metadata.missed_end)
        self.__packets_log["interrupted"].append(metadata.interrupted)
