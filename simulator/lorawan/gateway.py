
from __future__ import annotations
import logging

from simulator.environment import simulation_env as sim
from simulator.lora.radio import LoraRadio
from simulator.lora.packet import LoraPacket
from simulator.lorawan.network_server import NetworkServer
from simulator.lorawan.region import EU868_DATA_RATES

logger = logging.getLogger(__name__)


class LoRaWanGateway:
    """
        A LoRaWAN gateway that forwards frames between radio and network server.

        This gateway is a simple forwarder: it receives uplink PHYPayloads over the radio,
        forwards them to the network server, and transmits any downlink response in the
        device's RX window.

        In a real deployment, the gateway communicates with the network server over IP
        (e.g., via the SemTech UDP protocol or gRPC). In simulation, it calls the
        network server directly.

        Reference: LoRaWAN L2 1.0.4 Specification §3.
    """

    def __init__(
        self,
        network_server: NetworkServer,
        radio: LoraRadio|None = None,
        data_rate: int = 5,
        tx_power: int = 14,
    ):
        self.radio = radio if radio else LoraRadio()
        self.network_server = network_server
        self.data_rate = data_rate
        self.tx_power = tx_power
        self.frames_forwarded = 0
        self._configure_radio()
        sim.create_task(self._run())

    def _configure_radio(self) -> None:
        dr = EU868_DATA_RATES[self.data_rate]
        self.radio.set_rx_config(
            spreading_factor=dr.spreading_factor.value,
            bandwidth=dr.bandwidth.to_khz(),
        )
        self.radio.set_tx_config(
            power=self.tx_power,
            spreading_factor=dr.spreading_factor.value,
            bandwidth=dr.bandwidth.to_khz(),
        )

    async def _run(self) -> None:
        """Main gateway loop: receive uplinks, forward to NS, send downlinks."""
        while sim.is_running():
            try:
                result = await self.radio.receive_data_wait()
            except TimeoutError:
                return

            assert isinstance(result, LoraPacket)
            raw_uplink = result.payload
            self.frames_forwarded += 1

            logger.debug(
                f"{sim.current_time():.2f}s  GW  received uplink ({len(raw_uplink)} bytes)"
            )

            # Forward to network server and get downlink response (if any)
            downlink_raw = await self.network_server.handle_uplink(raw_uplink)

            if downlink_raw is not None:
                # Transmit downlink in RX1 window
                # The device expects the downlink RECEIVE_DELAY1 after its TX ended.
                # By now, some processing time has passed. We transmit immediately —
                # the device's RX window should still be open.
                logger.debug(
                    f"{sim.current_time():.2f}s  GW  sending downlink ({len(downlink_raw)} bytes)"
                )
                await self.radio.transmit_data_blocking(downlink_raw)
                # Return to RX mode after transmitting
                await self.radio.receive(continuous=True)
