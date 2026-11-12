"""
    The EWS gateway, reduced to what it puts on air: an ACK 60 ms after every ping and, once per
    ping interval, a multi-packet download to one sensor that has not been updated yet. That
    sensor's ping is first moved behind everyone's static slot with SET_PING_SCHEDULE, in the ACK
    of its regular ping, and moved back in the ACK of its first ping after the update.

    The gateway's own FUOTA sender was not available when this was written, so the burst logic
    follows the sensor firmware's model of it (libs/transport/src/download.rs).
"""
import asyncio
import random

from simulator.environment import simulation_env as sim
from simulator.exceptions import SimulatorException
from simulator.lora.gateway_radio import LoraGatewayRadio

import frames
from frames import Frame
from scenario import RadioLog, Scenario


class Gateway:

    def __init__(self, scenario: Scenario, pending: list[int]):
        """
            :param pending: Net IDs of the sensors that still need the update.
        """
        self.scenario = scenario
        self.pending = set(pending)
        self.updated: dict[int, float] = {}  # net ID -> time the transfer finished
        self.failed_transfers = 0
        self.targets: dict[int, int | None] = {}  # ping interval -> net ID to update in it
        self.offered: set[int] = set()  # ping intervals in which MP_START went out
        self.moved: set[int] = set()  # net IDs whose ping is in a late slot at the moment
        self.log = RadioLog("gateway")
        self.rng = random.Random(scenario.seed * 100_003)

        self.modulation = dict(
            bandwidth=scenario.bandwidth,
            spreading_factor=scenario.spreading_factor,
            code_rate=scenario.code_rate,
            preamble_len=scenario.preamble_len,
        )
        self.radio = LoraGatewayRadio(position=(0.0, 0.0))
        self.radio.log_packets = False
        self.radio.set_channel_plan(
            frequencies=[scenario.async_channel] + scenario.channel_pool,
            payload_len=frames.MAX_FRAME_LEN,
            **self.modulation,
        )

        sim.create_task(self.run())


    async def run(self):
        scenario = self.scenario
        try:
            await self.radio.receive()
            while True:
                (frame, uplink_end) = await self.receive(None)
                if frame.name != "PING":
                    continue

                reply_at = uplink_end + scenario.gateway_ack_delay
                if sim.current_time() >= reply_at:
                    continue  # too late for this sensor's window

                interval = int(uplink_end // scenario.ping_interval)
                net_id = frame.net_id
                target = net_id == self.target(interval) and interval not in self.offered
                if target != (net_id in self.moved):
                    # Into a late slot for the update, or back to the static one after it.
                    start = scenario.slot_start(net_id, late=target)
                    command = frames.set_ping_schedule(net_id, start, scenario.ping_interval)
                    await self.transmit(command, scenario.async_channel, reply_at)
                    if await self.answer(net_id) is not None:
                        self.moved.symmetric_difference_update({net_id})
                elif target:
                    self.offered.add(interval)
                    start = frames.mp_start(frame.net_id, scenario.total_packets)
                    await self.transmit(start, scenario.async_channel, reply_at)
                    if await self.transfer(frame.net_id):
                        self.pending.discard(frame.net_id)
                        self.updated[frame.net_id] = sim.current_time()
                    else:
                        self.failed_transfers += 1
                else:
                    ack = frames.downlink_ack(frame.net_id)
                    await self.transmit(ack, scenario.async_channel, reply_at)
        except (SimulatorException, asyncio.TimeoutError):
            return


    def target(self, interval: int) -> int | None:
        """
            The sensor that gets the update in the given ping interval, decided the first time
            it is asked for: the lowest net ID that still needs it.
        """
        if interval not in self.targets:
            self.targets[interval] = min(self.pending) if self.pending else None
        return self.targets[interval]


    async def transfer(self, net_id: int) -> bool:
        scenario = self.scenario
        pool = scenario.channel_pool

        accept = await self.answer(net_id)
        if accept is None:
            return False  # offered again at one of the sensor's next pings

        unacked = list(range(scenario.total_packets))
        next_burst = accept + scenario.gateway_turnaround
        highest_sent = -1
        position = 0
        dwell = 0.0

        while unacked:
            if dwell >= scenario.channel_dwell:
                dwell = 0.0
                position = (position + 1) % len(pool)

            burst = unacked[:scenario.packets_per_burst]
            for (index, packet_id) in enumerate(burst):
                last = index == len(burst) - 1
                fragment = frames.fragment(
                    net_id, packet_id, scenario.fragment_data_len(packet_id), last_of_burst=last,
                )
                await self.transmit(fragment, pool[position], next_burst)
                next_burst = sim.current_time() + scenario.packet_gap
            dwell += scenario.burst_time(len(burst))
            highest_sent = max(highest_sent, burst[-1])

            # Without an answer, ask for one instead of repeating the burst.
            asks = 0
            while True:
                answer = await self.answer(net_id)
                if answer is not None:
                    break
                if asks == scenario.highest_asks:
                    await self.transmit(frames.mp_cancel(net_id), pool[position])
                    return False
                asks += 1
                await self.transmit(frames.mp_highest(net_id, highest_sent), pool[position])

            # A full list only says something about the IDs up to its last one.
            missing = self.last_answer.missing_ids
            covered = highest_sent
            if len(missing) == frames.MP_MISSING_MAX_IDS:
                covered = missing[-1]
            unacked = [i for i in unacked if i > covered or i in missing]
            next_burst = answer + scenario.gateway_turnaround

        return True


    async def answer(self, net_id: int) -> float | None:
        """
            Waits for the sensor's ACK or MP_MISSING, returns the time it ended (the frame itself
            is left in `last_answer`) or None if nothing came.
        """
        deadline = sim.current_time() + self.scenario.answer_wait
        while sim.current_time() < deadline:
            try:
                (frame, end) = await self.receive(deadline - sim.current_time())
            except asyncio.TimeoutError:
                break
            # Pings of other sensors go unanswered while a transfer keeps the transmitter busy.
            if frame.net_id == net_id and frame.no_ack:
                self.last_answer = frame
                return end
        return None


    async def receive(self, timeout: float | None) -> tuple[Frame, float]:
        """ The next uplink that is heard; raises a TimeoutError when `timeout` passes first. """
        deadline = None if timeout is None else sim.current_time() + timeout
        while True:
            if deadline is None:
                got = await self.radio.receive_data_wait(metadata=True)
            elif deadline > sim.current_time():
                got = await self.radio.receive_data_within(
                    deadline - sim.current_time(), metadata=True,
                )
            else:
                raise asyncio.TimeoutError()
            if self.rng.random() >= self.scenario.uplink_loss:
                break
        (packet, meta) = got  # type: ignore[misc]
        assert meta.arrival_time is not None
        frame = Frame.decode(packet.payload)
        self.log.add(
            "rx", meta.arrival_time - packet.airtime, packet.config.frequency,
            len(packet.payload), frame.name, end=meta.arrival_time,
        )
        return (frame, meta.arrival_time)


    async def transmit(self, frame: Frame, frequency: int, at: float | None = None) -> None:
        if at is not None and at > sim.current_time():
            await sim.sleep_until(at)
        self.radio.set_tx_config(
            power=self.scenario.gateway_tx_power, frequency=frequency, **self.modulation,
        )
        data = frame.encode()
        airtime = await self.radio.transmit_data_blocking(data)
        end = self.radio.tx_end_time
        assert end is not None
        self.log.add("tx", end - airtime, frequency, len(data), frame.name, end=end)
