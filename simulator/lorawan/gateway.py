
from __future__ import annotations
import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from simulator.environment import simulation_env as sim
from simulator.exceptions import SimulatorException
from simulator.lora.airtime import estimate_airtime
from simulator.lora.gateway_radio import LoraGatewayRadio
from simulator.lora.packet import LoraPacket
from simulator.lorawan.beacon import encode_beacon, compute_ping_slot_times
from simulator.lorawan.network_server import NetworkServer, ScheduledMulticastDownlink
from simulator.lorawan.region import (
    EU868_DATA_RATES, RECEIVE_DELAY1,
    BEACON_INTERVAL, BEACON_RESERVED, BEACON_GUARD,
)

logger = logging.getLogger(__name__)


#: Callback invoked after a scheduled multicast frame left the antenna. It is handed the queue
#: entry and the raw PHYPayload that went on the air; the return value may be awaitable.
MulticastCallback = Callable[
    [ScheduledMulticastDownlink, bytes], Awaitable[None] | None
]


class DutyCycleLimiter:
    """Time-on-air budget for one transmitter.

    Models the usual sub-band limit the simple way: after a frame of ``airtime`` seconds the
    transmitter stays quiet for ``airtime * (1/duty_cycle - 1)`` seconds, so over any long
    stretch at most ``duty_cycle`` of the time is spent transmitting. Set it to 0.10 for the
    EU868 869.4-869.65 MHz sub-band or 0.01 for the 1% sub-bands.

    Reference: ETSI EN 300 220-2, as applied in RP002-1.0.4 §2.4.3.
    """

    def __init__(self, duty_cycle: float) -> None:
        assert 0 < duty_cycle <= 1, "Duty cycle must be a fraction in (0, 1]"
        self.duty_cycle = duty_cycle
        self.quiet_until = 0.0

    def quiet_time(self, airtime: float) -> float:
        """How long the transmitter must stay off after a frame of *airtime* seconds."""
        return airtime * (1 / self.duty_cycle - 1)

    def reserve(self, airtime: float) -> None:
        """Book the medium for a frame starting now plus the quiet time that follows it."""
        end_of_quiet = sim.current_time() + airtime + self.quiet_time(airtime)
        self.quiet_until = max(self.quiet_until, end_of_quiet)

    async def wait_for_slot(self) -> None:
        """Sleep until the transmitter is allowed to send again.

        The wake-up is never scheduled before the next tick: asking the environment to sleep
        until the tick it is already on would register an event that can no longer fire.
        """
        if sim.current_time() < self.quiet_until:
            await sim.sleep_until(max(self.quiet_until, sim.next_tick()))


class LoRaWanGateway:
    """
        A LoRaWAN gateway that forwards frames between radio and network server.

        This gateway is a simple forwarder: it receives uplink PHYPayloads over the radio,
        forwards them to the network server, and transmits any downlink response in the
        device's RX window.

        When *class_b_enabled* is ``True`` the gateway also broadcasts beacons every
        ``BEACON_INTERVAL`` seconds and transmits pending Class B downlinks at the
        correct ping slot times.

        A third background task drains the network server's multicast downlink queue (see
        ``NetworkServer.schedule_multicast_downlink``) and puts those frames on the air at
        their scheduled time. All three tasks share one transmitter, so they take a lock
        around it and respect a fixed priority: beacons first (their timing is what the whole
        Class B network is synchronised to), then RX1 replies (the device only listens for a
        fraction of a second), then multicast frames, which simply wait their turn.

        In a real deployment, the gateway communicates with the network server over IP
        (e.g., via the SemTech UDP protocol or gRPC). In simulation, it calls the
        network server directly.

        Reference: LoRaWAN L2 1.0.4 Specification §3 & §12.
    """

    #: How long the multicast scheduler sleeps between checks of an empty or not-yet-due
    #: queue. Small enough to hit ping-slot scale timing, large enough not to dominate the
    #: event queue.
    MULTICAST_POLL_INTERVAL = 0.05

    #: How long the uplink loop waits before listening again when it finds the transceiver
    #: busy transmitting. Nothing can be received during a transmission, so this only costs
    #: a fraction of the frame that is already on the air.
    TX_BACKOFF = 0.01

    def __init__(
        self,
        network_server: NetworkServer,
        radio: LoraGatewayRadio|None = None,
        data_rate: int = 5,
        tx_power: int = 14,
        class_b_enabled: bool = False,
        duty_cycle: float | None = None,
        on_multicast_transmitted: MulticastCallback | None = None,
    ):
        """
            :param duty_cycle: Transmit duty cycle as a fraction, e.g. ``0.10`` for a 10%
                sub-band. None (the default) leaves the transmitter unlimited, which is how
                the gateway behaved before this parameter existed.
            :param on_multicast_transmitted: Optional hook called after each scheduled
                multicast frame goes out.
        """
        self.radio = radio if radio else LoraGatewayRadio()
        self.network_server = network_server
        self.data_rate = data_rate
        self.tx_power = tx_power
        self.frames_forwarded = 0
        #: RX1 replies dropped because the single transmitter was busy with another
        #: device's window or with a beacon. The uplink itself was still forwarded.
        self.downlinks_skipped = 0
        #: Number of scheduled multicast frames put on the air by the scheduler task.
        self.multicast_frames_sent = 0
        #: ``(time, group_addr, fport, len(raw))`` for every multicast frame transmitted.
        self.multicast_log: list[tuple[float, int, int, int]] = []
        self.on_multicast_transmitted = on_multicast_transmitted
        self.duty_cycle = DutyCycleLimiter(duty_cycle) if duty_cycle is not None else None
        self._class_b_enabled = class_b_enabled
        # Guards the single transmitter against concurrent use by the uplink loop, the
        # beacon loop and the multicast scheduler.
        self._radio_lock = asyncio.Lock()
        # Non-zero while an RX1 reply or a beacon is pending; the multicast scheduler yields.
        self._priority_tx = 0
        self._multicast_batch: list[ScheduledMulticastDownlink] = []
        self._configure_radio()
        sim.create_task(self._run())
        sim.create_task(self._multicast_loop())
        if class_b_enabled:
            sim.create_task(self._beacon_loop())

    def _configure_radio(self) -> None:
        dr = EU868_DATA_RATES[self.data_rate]
        self.radio.set_rx_chain_config(
            chain=0,
            spreading_factor=dr.spreading_factor.value,
            bandwidth=dr.bandwidth.to_khz(),
        )
        self.radio.set_tx_config(
            power=self.tx_power,
            spreading_factor=dr.spreading_factor.value,
            bandwidth=dr.bandwidth.to_khz(),
        )

    # ---- Shared transmitter ----

    async def _transmit(self, raw: bytes) -> float:
        """Transmit with the current radio configuration, holding the transmitter lock.

        **Simulator choice — the duty cycle is a budget, not a gate, on this path.** Only
        :meth:`_multicast_scheduler` ever calls ``duty_cycle.wait_for_slot()``; RX1 replies
        and Class B beacons are unmovable in time (a device opens its window once and a
        beacon paces the whole Class B network), so they are transmitted unconditionally and
        merely *charged* to the budget. The practical effect is that unicast and beacon
        traffic can push the multicast scheduler into a longer quiet period, which is the
        conservative direction for a FUOTA study.
        """
        async with self._radio_lock:
            if self.duty_cycle is not None:
                # Book the medium once the transmitter is actually ours: reserving before
                # the lock would start the quiet period while the frame is still waiting,
                # and end it too early. Anything that checks the budget in the tick this
                # transmission ends in has to already see the quiet time.
                self.duty_cycle.reserve(self._estimated_airtime(raw, None))
            airtime = await self.radio.transmit_data_blocking(raw)
            await self.radio.receive(continuous=True)
        return float(airtime)

    async def transmit_multicast(
        self, raw: bytes, data_rate: int | None = None, frequency: int | None = None,
    ) -> float:
        """Transmit a prebuilt multicast frame, optionally on other radio parameters.

        The transmitter is retuned for the frame and put back on the gateway's default
        configuration afterwards, so an RX1 reply that follows still goes out with the
        parameters the device expects.

        :param data_rate: EU868 data rate index for this frame, or None for the gateway's
            own rate.
        :param frequency: Carrier frequency in hertz, or None to stay on the current channel.
        :returns: The frame's time on air in seconds.
        """
        if data_rate is None and frequency is None:
            return await self._transmit(raw)

        dr = EU868_DATA_RATES[data_rate if data_rate is not None else self.data_rate]
        if self.duty_cycle is not None:
            self.duty_cycle.reserve(self._estimated_airtime(raw, data_rate))
        async with self._radio_lock:
            self.radio.set_tx_config(
                power=self.tx_power,
                spreading_factor=dr.spreading_factor.value,
                bandwidth=dr.bandwidth.to_khz(),
                frequency=frequency,
            )
            try:
                airtime = await self.radio.transmit_data_blocking(raw)
                await self.radio.receive(continuous=True)
            finally:
                self._configure_radio()
        return float(airtime)

    def _estimated_airtime(self, raw: bytes, data_rate: int | None) -> float:
        """Time on air a frame would take, used by the duty cycle limiter before sending."""
        dr = EU868_DATA_RATES[data_rate if data_rate is not None else self.data_rate]
        return estimate_airtime(
            payload_len=len(raw),
            bandwidth=dr.bandwidth.to_khz(),
            spreading_factor=dr.spreading_factor.value,
            code_rate=5,
        )

    async def _run(self) -> None:
        """Main gateway loop: receive uplinks and hand each one to its own task.

        The receiver is never parked: waiting out the ``RECEIVE_DELAY1`` of one device's
        reply inside this loop would make the gateway deaf for a full second after every
        uplink, so a fleet transmitting less than ~1.5 s apart would lose uplinks that never
        even reached the network server. Each uplink therefore gets a child task that
        forwards it and, a second later, tries its RX1 reply, while this loop goes straight
        back to listening. The radio itself is still a single transceiver — see
        :meth:`_reply_in_rx1` for what happens when two RX1 windows collide.
        """
        while sim.is_running():
            try:
                result = await self.radio.receive_data_wait()
            except TimeoutError:
                return
            except RuntimeError:
                # The single transceiver is in the middle of a transmission (an RX1 reply
                # from a sibling task, a beacon, a multicast fragment). Nothing can be
                # received while it is keyed, so wait for it to finish and listen again.
                await sim.sleep(self.TX_BACKOFF)
                continue

            assert isinstance(result, LoraPacket)
            # The radio hands the frame over as soon as its last symbol lands, so this is
            # effectively the end of the uplink (bar a few ticks of processing delay).
            uplink_end = sim.current_time()
            raw_uplink = result.payload
            self.frames_forwarded += 1

            logger.debug(
                f"{sim.current_time():.2f}s  GW  received uplink ({len(raw_uplink)} bytes)"
            )

            await sim.start_child_task(self._forward_uplink(raw_uplink, uplink_end))

    async def _forward_uplink(self, raw_uplink: bytes, uplink_end: float) -> None:
        """Forward one uplink to the network server and reply in its RX1 window."""
        try:
            downlink_raw = await self.network_server.handle_uplink(raw_uplink)
            if downlink_raw is None:
                return
            await self._reply_in_rx1(downlink_raw, uplink_end)
        except SimulatorException:
            return

    async def _reply_in_rx1(self, downlink_raw: bytes, uplink_end: float) -> None:
        """Transmit a downlink in a device's RX1 window, if the transmitter is free.

        The window opens exactly ``RECEIVE_DELAY1`` after the uplink ended and is a few
        hundred milliseconds long; sending any earlier only reaches devices that
        (incorrectly) leave their receiver on in between. The window is short and
        unmovable, so the multicast scheduler is told to stay off the air until the reply
        has gone out.

        When the transmitter is busy at that instant — another device's RX1 reply, a
        beacon — the reply is **skipped** rather than transmitted late into a window that
        has already closed. The uplink has still been forwarded, so the network server's
        state advanced; the command that was popped for this frame is retransmitted on the
        device's next uplink by the FUOTA packages, which keep a command in flight until it
        is answered.
        """
        self._priority_tx += 1
        try:
            await sim.sleep_until(max(uplink_end + RECEIVE_DELAY1, sim.next_tick()))
            if not sim.is_running():
                return
            if self._radio_lock.locked():
                self.downlinks_skipped += 1
                logger.debug(
                    f"{sim.current_time():.2f}s  GW  skipping an RX1 reply "
                    f"({len(downlink_raw)} bytes): the transmitter is busy"
                )
                return
            logger.debug(
                f"{sim.current_time():.2f}s  GW  sending downlink "
                f"({len(downlink_raw)} bytes)"
            )
            await self._transmit(downlink_raw)
        finally:
            self._priority_tx -= 1

    # ---- Multicast downlink scheduling ----

    async def _multicast_loop(self) -> None:
        """Background task: put queued multicast downlinks on the air when they are due.

        The loop polls, so it is always sleeping when the simulation runs out; swallow that
        so the environment does not treat a normal shutdown as a task failure.
        """
        try:
            await self._multicast_scheduler()
        except SimulatorException:
            return

    async def _multicast_scheduler(self) -> None:
        while sim.is_running():
            if not self._multicast_batch:
                due_time = self.network_server.next_multicast_downlink_time()
                if due_time is None:
                    await sim.sleep(self.MULTICAST_POLL_INTERVAL)
                    continue
                now = sim.current_time()
                if due_time > now:
                    await sim.sleep_until(max(
                        min(due_time, now + self.MULTICAST_POLL_INTERVAL),
                        sim.next_tick(),
                    ))
                    continue
                self._multicast_batch = (
                    self.network_server.pop_due_multicast_downlinks(sim.current_time())
                )
                if not self._multicast_batch:
                    await sim.sleep(self.MULTICAST_POLL_INTERVAL)
                    continue

            # A pending RX1 reply or beacon wins; the multicast frame simply waits.
            if self._priority_tx > 0 or self._radio_lock.locked():
                await sim.sleep(self.MULTICAST_POLL_INTERVAL)
                continue

            if self.duty_cycle is not None and sim.current_time() < self.duty_cycle.quiet_until:
                await self.duty_cycle.wait_for_slot()
                continue

            entry = self._multicast_batch.pop(0)
            await self._send_multicast(entry)

    async def _send_multicast(self, entry: ScheduledMulticastDownlink) -> None:
        """Build and transmit one queued multicast downlink."""
        group = self.network_server.get_multicast_group(entry.group_addr)
        if group is None:
            logger.warning(
                f"{sim.current_time():.2f}s  GW  dropping multicast for unknown group "
                f"0x{entry.group_addr:08X}"
            )
            return

        try:
            raw = self.network_server.build_multicast_downlink(
                entry.group_addr, fport=entry.fport, payload=entry.payload,
            )
        except ValueError as exc:
            # This runs inside the gateway's multicast task, where an exception is a fatal
            # simulation error. An oversized or over-counted frame is an application bug:
            # log it and drop the frame, the way a real concentrator would reject it.
            logger.error(
                f"{sim.current_time():.2f}s  GW  dropping multicast for "
                f"0x{entry.group_addr:08X}: {exc}"
            )
            return

        logger.debug(
            f"{sim.current_time():.2f}s  GW  multicast TX  "
            f"GroupAddr=0x{entry.group_addr:08X}  FPort={entry.fport}  {len(raw)} bytes"
        )

        await self.transmit_multicast(raw, group.data_rate, group.frequency)

        self.multicast_frames_sent += 1
        self.multicast_log.append(
            (sim.current_time(), entry.group_addr, entry.fport, len(raw))
        )

        if self.on_multicast_transmitted is not None:
            outcome: Any = self.on_multicast_transmitted(entry, raw)
            if inspect.isawaitable(outcome):
                await outcome

    # ---- Class B: beacon broadcasting & ping slot downlinks ----

    async def _beacon_loop(self) -> None:
        """Broadcast beacons every ``BEACON_INTERVAL`` and send Class B downlinks.

        The loop is always waiting for the next beacon or ping slot when the simulation
        runs out, and ``sleep_until`` wakes it one last time at the final tick; swallow
        that so a normal shutdown is not reported as a task failure (same contract as
        ``_multicast_loop``).
        """
        try:
            await self._beacon_scheduler()
        except SimulatorException:
            return

    async def _beacon_scheduler(self) -> None:
        next_beacon = 0.0

        while sim.is_running():
            if next_beacon > sim.current_time():
                await sim.sleep_until(next_beacon)
                # The environment wakes every sleeper once more at the final tick; the
                # radio is already torn down by then, so there is nothing left to send.
                if not sim.is_running():
                    return

            beacon_time = int(next_beacon)
            beacon_data = encode_beacon(beacon_time)

            logger.debug(
                f"{sim.current_time():.2f}s  GW  beacon broadcast time={beacon_time}"
            )

            # Nothing may delay a beacon: the whole Class B network keys its timing off it.
            self._priority_tx += 1
            try:
                await self._transmit(beacon_data)
            finally:
                self._priority_tx -= 1

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
            if not sim.is_running():
                return

            logger.debug(
                f"{sim.current_time():.2f}s  GW  ping slot TX for "
                f"0x{dev_addr:08X}"
            )

            self._priority_tx += 1
            try:
                await self._transmit(raw)
            finally:
                self._priority_tx -= 1
