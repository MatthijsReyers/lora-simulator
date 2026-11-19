"""
A firmware recipient: a LoRaWAN end device with the three FUOTA packages (TS003, TS004, TS005)
and the ladder package, which follows the gateway's spreading factor schedule until it holds
the image and then acknowledges it (Section III of the paper).

How a device listens during the session is its own choice:

- `slotted`: it stays in Class A and opens a receive window for every frame of the schedule,
  the way it opens an RX1 window: awake a guard time early, asleep again as soon as no preamble
  shows up or the frame is over. This is what the paper's energy model assumes.
- `continuous`: it switches to Class C for the session and retunes at every round.

Either way the session ends for a device the moment its data block is complete.
"""
from __future__ import annotations

import asyncio
import math
import random
from typing import Literal

from simulator.environment import simulation_env as sim
from simulator.exceptions import SimulatorException
from simulator.lorawan.device import DeviceSession, LoRaWanDevice, MulticastSession
from simulator.lorawan.enums.operating_mode import OperatingMode
from simulator.lorawan.fuota.device_stack import FuotaDeviceStack
from simulator.lorawan.fuota.frag_transport import FRAGMENTATION_FPORT

from ladder import LadderDeviceApplication, LadderSchedule, spreading_factor_of

Listen = Literal["slotted", "continuous"]


class LadderDevice(LoRaWanDevice):
    """ An end device that can run a multicast session over a spreading factor ladder. """

    def __init__(self, *args, listen: Listen = "slotted", **kwargs) -> None:  # type: ignore[no-untyped-def]
        super().__init__(*args, **kwargs)
        self.listen: Listen = listen
        self.windows_opened = 0          # slotted: receive windows opened for a frame slot
        self.frames_heard = 0            # slotted: windows in which a frame was received
        self.slots_missed = 0            # slotted: slots the device was too late for
        self.session_ended_at: float | None = None
        self.on_session_end = None       # coroutine function, called once the radio is free

    async def start_ladder_session(
            self, group_addr: int, schedule: LadderSchedule, frequency: int,
        ) -> None:
        """
            Schedules the session on a joined multicast group. It replaces a session that is
            already registered for the group, and `cancel_session` ends it like any other.
        """
        assert group_addr in self._multicast_groups, f"No multicast group 0x{group_addr:08X}"
        self.cancel_session(group_addr, superseded=True)
        session = MulticastSession(
            group_addr=group_addr,
            mode=OperatingMode.CLASS_C if self.listen == "continuous" else OperatingMode.CLASS_A,
            start_time=schedule.start_time,
            end_time=schedule.end_time,
            data_rate=schedule.rounds[0].data_rate,
            frequency=frequency,
        )
        cancel = asyncio.Event()
        self._session_cancels[group_addr] = cancel
        self._multicast_sessions[group_addr] = session
        await sim.start_child_task(self._run_ladder(session, schedule, frequency, cancel))

    def stop_ladder_sessions(self) -> None:
        for group_addr in list(self._multicast_sessions):
            self.cancel_session(group_addr)

    async def _run_ladder(
            self, session: MulticastSession, schedule: LadderSchedule, frequency: int,
            cancel: asyncio.Event,
        ) -> None:
        try:
            arm_at = schedule.start_time - self._rx_wakeup_guard()
            if await self._sleep_until_or_cancel(arm_at, cancel) or not sim.is_running():
                self._forget_session(session)
                return
            previous_mode = self.operating_mode
            previous_rx = (self.rx_data_rate, self.rx_frequency)
            session.running = True
            if self.listen == "continuous":
                await self._listen_continuously(schedule, frequency, cancel)
            else:
                await self._listen_in_slots(schedule, frequency, cancel)
            session.running = False
            if not sim.is_running():
                self._forget_session(session)
                return
            if not session.superseded:
                self.set_rx_config(*previous_rx)
                await self.switch_mode(previous_mode)
            self._forget_session(session)
            self.session_ended_at = sim.current_time()
            if self.on_session_end is not None:
                await self.on_session_end()
        except SimulatorException:
            session.running = False
            self._forget_session(session)

    async def _listen_in_slots(
            self, schedule: LadderSchedule, frequency: int, cancel: asyncio.Event,
        ) -> None:
        data_rate = None
        for slot in schedule.slots():
            if cancel.is_set():
                return
            wake_at = slot.time - self._rx_wakeup_guard()
            if wake_at < sim.current_time():
                self.slots_missed += 1
                continue
            if await self._sleep_until_or_cancel(wake_at, cancel) or not sim.is_running():
                return
            if slot.data_rate != data_rate:
                data_rate = slot.data_rate
                self.set_rx_config(data_rate, frequency)
            self.windows_opened += 1
            packet = await self._rx_window(slot.time, self._rx_window_timeout())
            if packet is not None:
                self.frames_heard += 1
                await self._process_downlink(packet.payload)

    async def _listen_continuously(
            self, schedule: LadderSchedule, frequency: int, cancel: asyncio.Event,
        ) -> None:
        for (number, round_) in enumerate(schedule.rounds):
            self.set_rx_config(round_.data_rate, frequency)
            if number == 0:
                await self.switch_mode(OperatingMode.CLASS_C)
            # Retuned a guard time before the first frame of the next round.
            round_end = schedule.round_start(number) + round_.duration - self._rx_wakeup_guard()
            if await self._sleep_until_or_cancel(round_end, cancel) or not sim.is_running():
                return


class RecipientStack(FuotaDeviceStack):
    """
        The FUOTA device stack, with the acknowledgement of the data block sent as soon as the
        device has left the session instead of with its next periodic uplink: the gateway keeps
        transmitting until it has heard from every device.
    """

    def __init__(self, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
        super().__init__(*args, **kwargs)
        self.transmitting = False
        self.acknowledgements_sent = 0

    async def _slot(self) -> None:
        if self.transmitting:
            return
        self.transmitting = True
        try:
            await super()._slot()
        finally:
            self.transmitting = False

    async def send_acknowledgement(self, frag_index: int = 0) -> None:
        """
            Transmits the `FragDataBlockReceivedReq` the TS004 package queued, and its
            retransmissions, until the server answered or the package gave up. The package
            decides when each one is due (the `BlockAckDelay` spreading of TS004 §3.5).
        """
        package = self.fragmentation
        idle = 0
        try:
            while sim.is_running():
                session = package.sessions.get(frag_index)
                if session is None or not session.completed:
                    return
                if session.ack_attempts > 0 and not session.ack_pending:
                    return
                ready_at = package.next_ready_at()
                if ready_at is None or self.transmitting:
                    # Nothing queued (yet, or any more): the package's own retry interval is
                    # at most this long.
                    idle += 1
                    if idle > (1 << (session.block_ack_delay + 4)) + 5:
                        return
                    await sim.sleep(1.0)
                    continue
                idle = 0
                if ready_at > sim.current_time():
                    await sim.sleep_until(max(ready_at, sim.next_tick()))
                    continue
                payload = package.pop_pending_uplink(sim.current_time())
                if payload is None:
                    continue
                self.transmitting = True
                try:
                    await self.device.send_uplink(FRAGMENTATION_FPORT, payload)
                finally:
                    self.transmitting = False
                self.acknowledgements_sent += 1
                self.uplinks_sent += 1
                self.uplinks_by_fport[FRAGMENTATION_FPORT] = (
                    self.uplinks_by_fport.get(FRAGMENTATION_FPORT, 0) + 1
                )
        except SimulatorException:
            return


class Recipient:
    def __init__(
            self,
            node: int,
            position: tuple[float, float],
            session: DeviceSession,
            gen_app_key: bytes,
            uplink_data_rate: int,
            tx_power: int,
            listen: Listen,
            uplink_interval: float,
            first_uplink: float,
            stop_after: float,
            rng: random.Random,
        ) -> None:
        self.node = node
        self.position = position
        self.distance = math.hypot(*position)
        self.device = LadderDevice(
            session=session, data_rate=uplink_data_rate, tx_power=tx_power, listen=listen,
        )
        self.device.radio.position = position
        self.stack = RecipientStack(
            self.device, gen_app_key=gen_app_key, uplink_interval=uplink_interval,
            first_uplink=first_uplink, stop_after=stop_after, rng=rng,
        )
        self.ladder = LadderDeviceApplication(self.device, self.device.start_ladder_session)
        self.device.register_application(self.ladder)
        self.device.on_session_end = self.stack.send_acknowledgement
        self.decode_sf: int | None = None

        # The session is over for a device as soon as its data block is complete, whether or
        # not the block then passes its integrity check.
        store_block = self.stack.fragmentation.on_block_received

        def block_complete(frag_index: int, data: bytes, descriptor: int) -> None:
            if store_block is not None:
                store_block(frag_index, data, descriptor)
            if self.device.rx_data_rate is not None:
                self.decode_sf = spreading_factor_of(self.device.rx_data_rate)
            self.device.stop_ladder_sessions()

        self.stack.fragmentation.on_block_received = block_complete
        self.stack.start()

    @property
    def completion_time(self) -> float | None:
        return self.stack.completion_time

    def image(self) -> bytes | None:
        return self.stack.received_images.get(0)
