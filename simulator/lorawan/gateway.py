
from __future__ import annotations
import logging

from simulator.environment import simulation_env as sim
from simulator.lora.radio import LoraRadio
from simulator.lora.packet import LoraPacket
from simulator.lorawan.beacon import encode_beacon, compute_ping_slot_times
from simulator.lorawan.network_server import NetworkServer
from simulator.lorawan.region import (
    EU868_DATA_RATES,
    BEACON_INTERVAL, BEACON_RESERVED, BEACON_GUARD,
)

logger = logging.getLogger(__name__)


class LoRaWanGateway:
    """
        A LoRaWAN gateway that forwards frames between radio and network server.

        This gateway is a simple forwarder: it receives uplink PHYPayloads over the radio,
        forwards them to the network server, and transmits any downlink response in the
        device's RX window.

        When *class_b_enabled* is ``True`` the gateway also broadcasts beacons every
        ``BEACON_INTERVAL`` seconds and transmits pending Class B downlinks at the
        correct ping slot times.

        In a real deployment, the gateway communicates with the network server over IP
        (e.g., via the SemTech UDP protocol or gRPC). In simulation, it calls the
        network server directly.

        Reference: LoRaWAN L2 1.0.4 Specification §3 & §12.
    """

    def __init__(
        self,
        network_server: NetworkServer,
        radio: LoraRadio|None = None,
        data_rate: int = 5,
        tx_power: int = 14,
        class_b_enabled: bool = False,
    ):
        self.radio = radio if radio else LoraRadio()
        self.network_server = network_server
        self.data_rate = data_rate
        self.tx_power = tx_power
        self.frames_forwarded = 0
        self._class_b_enabled = class_b_enabled
        self._configure_radio()
        sim.create_task(self._run())
        if class_b_enabled:
            sim.create_task(self._beacon_loop())

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

    # ---- Class B: beacon broadcasting & ping slot downlinks ----

    async def _beacon_loop(self) -> None:
        """Broadcast beacons every ``BEACON_INTERVAL`` and send Class B downlinks."""
        next_beacon = 0.0

        while sim.is_running():
            if next_beacon > sim.current_time():
                await sim.sleep_until(next_beacon)

            beacon_time = int(next_beacon)
            beacon_data = encode_beacon(beacon_time)

            logger.debug(
                f"{sim.current_time():.2f}s  GW  beacon broadcast time={beacon_time}"
            )

            await self.radio.transmit_data_blocking(beacon_data)
            await self.radio.receive(continuous=True)

            # Transmit pending Class B downlinks at the correct ping slot times
            await self._send_class_b_downlinks(beacon_time)

            next_beacon += BEACON_INTERVAL

    async def _send_class_b_downlinks(self, beacon_time: int) -> None:
        """Send queued Class B downlinks at computed ping slot times."""
        schedule = await self.network_server.get_class_b_downlink_schedule(
            beacon_time
        )

        guard_time = beacon_time + BEACON_INTERVAL - BEACON_GUARD

        for dev_addr, raw, slot_time in schedule:
            if slot_time >= guard_time:
                continue
            if slot_time <= sim.current_time():
                continue

            await sim.sleep_until(slot_time)

            logger.debug(
                f"{sim.current_time():.2f}s  GW  ping slot TX for "
                f"0x{dev_addr:08X}"
            )

            await self.radio.transmit_data_blocking(raw)
            await self.radio.receive(continuous=True)
