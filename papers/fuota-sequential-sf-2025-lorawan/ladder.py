"""
The spreading factor ladder as a LoRaWAN application package.

TS005 cannot describe the paper's transmission scheme: one `McClassCSessionReq` is one data
rate, its window is a power of two seconds long, and a device holds four groups at most. So the
multicast group itself (address, keys, frame counters) is set up with TS005 as usual, and one
extra unicast command on a port of its own tells a device when the gateway sends with which
data rate:

    LadderSessionReq   CID | McGroupID | SessionTime (4) | DLFreq (3) | Rounds (1)
                       then per round: DR (1) | Frames (2) | Period in ms (3)
    LadderSessionAns   CID | Status | TimeToStart (3, only without an error)

Round `r` consists of `Frames` frames with data rate `DR`, the first one right after the last
frame slot of round `r - 1`, each `Period` after the one before. That fixes the start of every
frame, so a device only has to open its receiver for the frames themselves. The frames are
ordinary multicast downlinks of the TS005 group carrying TS004 `DataFragment`s.

The fields the two commands share with `McClassCSessionReq` / `Ans` (TS005 §4.5) are encoded
the same way.
"""
from __future__ import annotations

import logging
import math
from collections.abc import Awaitable, Callable, Iterable, Iterator
from dataclasses import dataclass

from simulator.environment import simulation_env as sim
from simulator.lora.airtime import estimate_airtime
from simulator.lorawan.application import Application
from simulator.lorawan.device import LoRaWanDevice
from simulator.lorawan.fuota.device_app import FuotaDeviceApplication
from simulator.lorawan.region import EU868_DATA_RATES

logger = logging.getLogger("fuota.ladder")

#: FPort of the package. 200 to 202 are TS005, TS004 and TS003.
LADDER_FPORT = 190

LADDER_SESSION_CID = 0x01

#: MHDR (1), FHDR (7), FPort (1) and MIC (4) of the frame, CID and Index&N (3) of TS004.
FRAGMENT_FRAME_OVERHEAD = 13 + 3

#: Largest fragment index N a `DataFragment` can carry (14 bits).
MAX_FRAGMENT_INDEX = (1 << 14) - 1


def data_rate_of(spreading_factor: int) -> int:
    """ The EU868 data rate index with this spreading factor at 125 kHz. """
    for (index, data_rate) in EU868_DATA_RATES.items():
        if data_rate.spreading_factor.value == spreading_factor:
            return index
    raise ValueError(f"No EU868 data rate with SF{spreading_factor}")


def spreading_factor_of(data_rate: int) -> int:
    return int(EU868_DATA_RATES[data_rate].spreading_factor.value)


def frame_airtime(data_rate: int, payload_len: int) -> float:
    dr = EU868_DATA_RATES[data_rate]
    return estimate_airtime(
        payload_len=payload_len, bandwidth=dr.bandwidth.to_khz(),
        spreading_factor=dr.spreading_factor.value, code_rate=5,
    )


# ── Schedule ─────────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class LadderRound:
    data_rate: int
    frames: int
    period_ms: int  # between the starts of two frames

    @property
    def period(self) -> float:
        return self.period_ms / 1000

    @property
    def duration(self) -> float:
        return self.frames * self.period


@dataclass(frozen=True)
class LadderSlot:
    index: int        # 0-based over the whole schedule
    round: int
    time: float       # start of the frame
    data_rate: int


@dataclass(frozen=True)
class LadderSchedule:
    """ When the gateway sends with which data rate: all a device knows about the gateway. """
    start_time: float
    rounds: tuple[LadderRound, ...]

    @property
    def total_frames(self) -> int:
        return sum(r.frames for r in self.rounds)

    @property
    def end_time(self) -> float:
        return self.start_time + sum(r.duration for r in self.rounds)

    def round_start(self, index: int) -> float:
        return self.start_time + sum(r.duration for r in self.rounds[:index])

    def slots(self) -> Iterator[LadderSlot]:
        index = 0
        for (number, round_) in enumerate(self.rounds):
            start = self.round_start(number)
            for k in range(round_.frames):
                yield LadderSlot(index, number, start + k * round_.period, round_.data_rate)
                index += 1


def plan_rounds(
        spreading_factors: Iterable[int],
        frames_per_sf: int,
        frame_len: int,
        duty_cycle: float,
        margin_ms: int = 100,
        max_frames: int = MAX_FRAGMENT_INDEX,
        max_duration: float | None = None,
    ) -> tuple[LadderRound, ...]:
    """
        The rounds of the paper's scheme (Section III): `frames_per_sf` frames with every
        spreading factor in turn, then the last one for as long as the fragment index and
        `max_duration` (seconds) allow. Frames follow each other at the duty cycle limit.

        :param frame_len: Octets of a whole frame, headers included.
        :param margin_ms: Added to the period the duty cycle asks for, so that a frame that
            leaves a little late does not push the next one out of its slot.
    """
    spreading_factors = list(spreading_factors)
    rounds: list[LadderRound] = []
    (frames_left, time_left) = (max_frames, max_duration)
    for (position, sf) in enumerate(spreading_factors):
        data_rate = data_rate_of(sf)
        period_ms = math.ceil(frame_airtime(data_rate, frame_len) / duty_cycle * 1000) + margin_ms
        last = position == len(spreading_factors) - 1
        frames = frames_left if last else min(frames_per_sf, frames_left)
        if time_left is not None:
            frames = min(frames, int(time_left * 1000 // period_ms))
        if frames <= 0:
            break
        rounds.append(LadderRound(data_rate, frames, period_ms))
        frames_left -= frames
        if time_left is not None:
            time_left -= frames * period_ms / 1000
    assert rounds, "The limits leave no room for a single frame"
    return tuple(rounds)


# ── Commands ─────────────────────────────────────────────────────────────────────────────────

@dataclass
class LadderSessionReq:
    group_id: int
    session_time: int        # GPS seconds, which is the simulation time
    dl_frequency: int        # hertz
    rounds: tuple[LadderRound, ...]

    def schedule(self) -> LadderSchedule:
        return LadderSchedule(float(self.session_time), self.rounds)

    def encode(self) -> bytes:
        assert 0 <= self.group_id <= 3 and 1 <= len(self.rounds) <= 255
        data = bytes([LADDER_SESSION_CID, self.group_id])
        data += self.session_time.to_bytes(4, "little")
        data += (self.dl_frequency // 100).to_bytes(3, "little")
        data += bytes([len(self.rounds)])
        for round_ in self.rounds:
            data += bytes([round_.data_rate])
            data += round_.frames.to_bytes(2, "little") + round_.period_ms.to_bytes(3, "little")
        return data

    @staticmethod
    def decode(data: bytes) -> LadderSessionReq | None:
        if len(data) < 10 or data[0] != LADDER_SESSION_CID or len(data) != 10 + 6 * data[9]:
            return None
        rounds = []
        for position in range(10, len(data), 6):
            rounds.append(LadderRound(
                data_rate=data[position],
                frames=int.from_bytes(data[position + 1:position + 3], "little"),
                period_ms=int.from_bytes(data[position + 3:position + 6], "little"),
            ))
        return LadderSessionReq(
            group_id=data[1] & 0x03,
            session_time=int.from_bytes(data[2:6], "little"),
            dl_frequency=int.from_bytes(data[6:9], "little") * 100,
            rounds=tuple(rounds),
        )


@dataclass
class LadderSessionAns:
    group_id: int
    dr_error: bool = False
    mc_group_undefined: bool = False
    start_missed: bool = False
    time_to_start: int | None = None  # seconds, absent with an error

    @property
    def accepted(self) -> bool:
        return not (self.dr_error or self.mc_group_undefined or self.start_missed)

    def encode(self) -> bytes:
        status = (
            self.group_id | self.dr_error << 2 | self.mc_group_undefined << 4
            | self.start_missed << 5
        )
        data = bytes([LADDER_SESSION_CID, status])
        if self.accepted:
            data += min(self.time_to_start or 0, 0xFFFFFF).to_bytes(3, "little")
        return data

    @staticmethod
    def decode(data: bytes) -> LadderSessionAns | None:
        if len(data) not in (2, 5) or data[0] != LADDER_SESSION_CID:
            return None
        status = data[1]
        return LadderSessionAns(
            group_id=status & 0x03,
            dr_error=bool(status & 0x04),
            mc_group_undefined=bool(status & 0x10),
            start_missed=bool(status & 0x20),
            time_to_start=int.from_bytes(data[2:5], "little") if len(data) == 5 else None,
        )


# ── Network server side ──────────────────────────────────────────────────────────────────────

@dataclass
class _InFlight:
    payload: bytes
    attempts: int = 0


class LadderServerApplication(Application):
    """
        Hands every device its `LadderSessionReq` in the RX1 window of an uplink and keeps
        doing so until the device answers, like the TS004 and TS005 server packages.
    """

    def __init__(self, max_command_retries: int = 3) -> None:
        self.max_command_retries = max_command_retries
        self.answers: dict[int, LadderSessionAns] = {}
        self.downlinks_sent = 0
        self.uplinks_received = 0
        self.commands_abandoned = 0
        self._pending: dict[int, _InFlight] = {}

    def port(self) -> int:
        return LADDER_FPORT

    def start_session(self, dev_addrs: Iterable[int], request: LadderSessionReq) -> None:
        payload = request.encode()
        for dev_addr in dev_addrs:
            self.answers.pop(dev_addr, None)
            self._pending[dev_addr] = _InFlight(payload)

    def devices_accepted(self) -> set[int]:
        return {addr for (addr, answer) in self.answers.items() if answer.accepted}

    async def has_downlink(self, dev_addr: int) -> bool:
        return dev_addr in self._pending

    async def get_downlink(self, dev_addr: int) -> bytes | None:
        entry = self._pending.get(dev_addr)
        if entry is None:
            return None
        if entry.attempts > self.max_command_retries:
            del self._pending[dev_addr]
            self.commands_abandoned += 1
            logger.warning(
                f"{sim.current_time():.2f}s  LADDER-NS  0x{dev_addr:08X} never answered the "
                f"session request, giving up on it"
            )
            return None
        entry.attempts += 1
        self.downlinks_sent += 1
        return entry.payload

    async def on_uplink(self, dev_addr: int, payload: bytes) -> None:
        self.uplinks_received += 1
        answer = LadderSessionAns.decode(payload)
        if answer is None:
            return
        self._pending.pop(dev_addr, None)
        self.answers[dev_addr] = answer
        logger.info(
            f"{sim.current_time():.2f}s  LADDER-NS  0x{dev_addr:08X} "
            f"{'accepted' if answer.accepted else 'REFUSED'} the session"
        )


# ── Device side ──────────────────────────────────────────────────────────────────────────────

class LadderDeviceApplication(FuotaDeviceApplication):
    """
        Device side of the package: checks a `LadderSessionReq`, has the device run the session
        and answers. `start_session(group_addr, schedule, frequency)` is the device's part.
    """

    def __init__(
            self, device: LoRaWanDevice,
            start_session: Callable[[int, LadderSchedule, int], Awaitable[None]],
        ) -> None:
        super().__init__(device)
        self.start_session = start_session
        self.requests: list[LadderSessionReq] = []

    def port(self) -> int:
        return LADDER_FPORT

    async def on_downlink(self, payload: bytes) -> None:
        request = LadderSessionReq.decode(payload)
        if request is None:
            return
        assert self.device is not None
        now = int(sim.current_time())
        group = self.device.get_multicast_group_by_id(request.group_id)
        answer = LadderSessionAns(
            group_id=request.group_id,
            dr_error=any(r.data_rate not in EU868_DATA_RATES for r in request.rounds),
            mc_group_undefined=group is None,
            start_missed=request.session_time < now,
        )
        if answer.accepted:
            assert group is not None
            answer.time_to_start = request.session_time - now
            self.requests.append(request)
            await self.start_session(group.group_addr, request.schedule(), request.dl_frequency)
        await self.queue_uplink(answer.encode())
