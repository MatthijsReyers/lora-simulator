"""
    An EWS sensor: asleep, except for a ping every interval and, when the gateway answers that
    ping with MP_START, the multi-packet download of a firmware image.
"""
import asyncio
import math
import random
from dataclasses import dataclass

from simulator.environment import simulation_env as sim
from simulator.exceptions import SimulatorException
from simulator.lora.client_radio import LoraClientRadio
from simulator.power_consumer import PowerConsumer

import frames
from frames import Frame
from simulator.lora.radio_power_profile import EwsSensorPowerProfile

from scenario import RadioLog, Scenario


@dataclass
class Cycle:
    """ One wake-up of a sensor. """
    sensor: str
    start: float
    kind: str  # ping | update
    outcome: str  # ack | command | no_ack | complete | lost | cancelled
    awake: float
    energy: float
    fragments: int = 0


class Sensor:

    def __init__(self, scenario: Scenario, net_id: int):
        self.scenario = scenario
        self.net_id = net_id
        self.name = f"sensor_{net_id:02d}"
        # Start of the ping schedule; the gateway moves it with SET_PING_SCHEDULE.
        self.schedule_start = scenario.slot_start(net_id)
        self.rng = random.Random(scenario.seed * 100_003 + net_id)

        self.profile = EwsSensorPowerProfile()
        self.radio = LoraClientRadio(
            position=scenario.position(net_id), power_profile=self.profile,
        )
        self.radio.log_packets = False
        modulation = dict(
            bandwidth=scenario.bandwidth,
            spreading_factor=scenario.spreading_factor,
            code_rate=scenario.code_rate,
            preamble_len=scenario.preamble_len,
        )
        self.radio.set_tx_config(power=scenario.sensor_tx_power, **modulation)
        self.radio.set_rx_config(max_payload_len=frames.MAX_FRAME_LEN, **modulation)

        self.mcu = PowerConsumer(current_power=self.profile.SLEEP_POWER)
        self.log = RadioLog(self.name)
        self.cycles: list[Cycle] = []
        self.sessions = 0
        self.fragments = 0
        self.timed_out = False  # whether the last receive window ran to its timeout
        # The update as the protocol comparison counts it: from the wake-up in which the
        # gateway moved this sensor's ping to the end of the one that completed the download.
        self.update_started: float | None = None
        self.update_completed: float | None = None

        sim.create_task(self.run())


    def energy(self) -> float:
        """ Joules used so far by the radio, the MCU and the radio sessions. """
        return (
            self.radio.power_consumer.get_total_energy_consumed()
            + self.mcu.get_total_energy_consumed()
            + self.sessions * self.profile.SESSION_ENERGY
        )


    async def run(self):
        try:
            interval = self.scenario.ping_interval
            while True:
                # The next moment of the schedule: its start plus a whole number of intervals.
                elapsed = sim.current_time() - self.schedule_start
                repeats = math.floor(elapsed / interval) + 1 if elapsed >= 0 else 0
                await sim.sleep_until(self.schedule_start + repeats * interval)
                await self.wake()
        except (SimulatorException, asyncio.TimeoutError):
            return


    async def wake(self):
        start = sim.current_time()
        energy = self.energy()
        self.fragments = 0
        self.mcu.set_power_consumption(self.profile.MCU_RUN_POWER)
        self.sessions += 1

        kind = "ping"
        reply = await self.ping()
        if reply is None:
            outcome = "no_ack"
        elif reply.command == frames.CMD_MP_START:
            kind = "update"
            await self.radio.off()
            await sim.sleep(self.scenario.accept_delay)
            self.sessions += 1
            outcome = await self.download(reply.u16)
            if outcome == "complete":
                await self.radio.off()
                self.update_completed = sim.current_time()
        elif reply.command == frames.CMD_SET_PING_SCHEDULE:
            # Apply the command, then confirm it in a radio session of its own.
            outcome = "command"
            if self.update_started is None:
                self.update_started = start
            self.schedule_start = reply.schedule_start
            await self.radio.off()
            await sim.sleep(self.scenario.accept_delay)
            self.sessions += 1
            await self.transmit(frames.uplink_ack(self.net_id))
        else:
            outcome = "ack"

        await self.radio.off()
        self.mcu.set_power_consumption(self.profile.SLEEP_POWER)
        self.cycles.append(Cycle(
            sensor=self.name, start=start, kind=kind, outcome=outcome,
            awake=sim.current_time() - start, energy=self.energy() - energy,
            fragments=self.fragments,
        ))


    async def ping(self) -> Frame | None:
        """ The routine exchange: returns the gateway's reply, if any attempt got one. """
        scenario = self.scenario
        self.radio.set_channel(scenario.async_channel)

        for attempt in range(1, scenario.max_attempts + 1):
            if attempt > 1:
                backoff = scenario.retry_backoff * (attempt - 1)
                await sim.sleep(backoff + self.rng.uniform(0, scenario.retry_jitter))
            if not await self.transmit(frames.ping(self.net_id, retry=attempt > 1)):
                continue
            reply = await self.listen(scenario.ack_timeout)
            if reply is not None:
                return reply
        return None


    async def download(self, total_packets: int) -> str:
        """ The receiving side of a multi-packet transfer, after libs/transport/download.rs. """
        scenario = self.scenario

        if not await self.transmit(frames.uplink_ack(self.net_id)):
            return "lost"

        pool = scenario.channel_pool
        position = 0
        dwell = 0.0
        # After a hop: the channel and dwell count to go back to while the gateway stays silent.
        look_back: tuple[int, float] | None = None
        self.radio.set_channel(pool[position])

        received: set[int] = set()
        highest = -1
        empty = 0

        # What the gateway's dwell count is mirrored from: the IDs the last answer listed and
        # the ID up to which that answer spoke.
        listed: list[int] = []
        through = -1
        answered_highest = -1
        burst_due = True
        heard = False

        def account(last: int) -> None:
            """ Counts the burst that ended with `last` against the channel, as the gateway does. """
            nonlocal burst_due, dwell
            if not burst_due:
                return
            burst_due = False
            packets = sum(1 for i in listed if i <= last) + max(last - through, 0)
            dwell += scenario.burst_time(max(packets, 1))

        while True:
            frame = await self.listen(scenario.fuota_window)

            if frame is None:
                if len(received) == total_packets:
                    return "complete"
                empty += 1
                if empty >= scenario.empty_windows:
                    return "lost"
                if self.timed_out and look_back is not None:
                    (previous, previous_dwell) = look_back
                    look_back = (position, dwell)
                    (position, dwell) = (previous, previous_dwell)
                    self.radio.set_channel(pool[position])
                continue

            empty = 0
            look_back = None

            if frame.command == frames.CMD_MP_CANCEL:
                return "cancelled"
            elif frame.multipacket:
                heard = True
                received.add(frame.u16)
                highest = max(highest, frame.u16)
                self.fragments += 1
                if frame.no_ack:
                    continue
                account(frame.u16)
            elif frame.command == frames.CMD_MP_HIGHEST:
                # A new ID, or fragments since the answer: a burst went out and its end was
                # lost. Otherwise the answer was.
                if heard or frame.u16 > answered_highest:
                    account(frame.u16)
                highest = max(highest, frame.u16)
            else:
                continue

            # The burst is over (or the gateway asked): tell it what is still missing.
            missing = [i for i in range(highest + 1) if i not in received]
            listed = missing[:frames.MP_MISSING_MAX_IDS]
            through = listed[-1] if len(listed) == frames.MP_MISSING_MAX_IDS else highest
            if listed:
                answer = frames.mp_missing(self.net_id, listed)
            else:
                answer = frames.uplink_ack(self.net_id)
            if not await self.transmit(answer):
                continue  # the gateway asks with MP_HIGHEST

            burst_due = True
            heard = False
            answered_highest = highest
            if len(received) < total_packets and dwell >= scenario.channel_dwell:
                # The gateway's next burst is on the next pool channel.
                look_back = (position, dwell)
                position = (position + 1) % len(pool)
                dwell = 0.0
                self.radio.set_channel(pool[position])


    async def clear_channel(self) -> bool:
        scenario = self.scenario
        for attempt in range(scenario.cca_retries + 1):
            if attempt > 0:
                await sim.sleep(self.rng.uniform(scenario.cca_defer_min, scenario.cca_defer_max))
            start = sim.current_time()
            await self.radio.receive(continuous=True)
            await sim.sleep(scenario.cca_duration)
            busy = self.radio.carrier_sense_instant()
            await self.radio.standby()
            self.log.add("cca", start, self.radio.tx_frequency, frame="busy" if busy else "free")
            if not busy:
                return True
        return False


    async def transmit(self, frame: Frame) -> bool:
        """ Returns False when the channel stayed busy and nothing was sent. """
        if not await self.clear_channel():
            return False
        data = frame.encode()
        airtime = await self.radio.transmit_data_blocking(data)
        end = self.radio.tx_end_time
        assert end is not None
        self.log.add("tx", end - airtime, self.radio.tx_frequency, len(data), frame.name, end=end)
        return True


    async def listen(self, duration: float) -> Frame | None:
        """
            One receive window: it ends at the first frame, or at the timeout unless a frame is
            being received at that moment. Returns the frame if it is a downlink for this sensor.
        """
        radio = self.radio
        radio.flush_rx_queue()
        await radio.receive(continuous=True)
        opened = sim.current_time()
        deadline = opened + duration
        frame = None
        self.timed_out = True

        while True:
            got = None
            remaining = deadline - sim.current_time()
            if remaining > 0:
                try:
                    got = await radio.receive_data_within(remaining, metadata=True)
                except asyncio.TimeoutError:
                    pass
            if got is None and radio.carrier_sense_instant():
                await radio.wait_for_channel_idle()
                got = await radio.receive_data_nowait(metadata=True)
            if got is None:
                break

            (packet, meta) = got  # type: ignore[misc]
            assert meta.arrival_time is not None
            decoded = Frame.decode(packet.payload)
            mine = decoded.downlink and decoded.net_id == self.net_id
            if mine and self.rng.random() < self.scenario.downlink_loss:
                continue  # not heard at all, the window runs on
            self.log.add(
                "rx", meta.arrival_time - packet.airtime, packet.config.frequency,
                len(packet.payload), decoded.name, end=meta.arrival_time,
            )
            frame = decoded if mine else None
            self.timed_out = False
            break

        await radio.standby()
        self.log.add("rx_window", opened, radio.rx_frequency)
        return frame
