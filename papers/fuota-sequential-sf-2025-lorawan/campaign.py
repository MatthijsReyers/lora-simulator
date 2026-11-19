"""
The server side of the scheme, as a campaign on the LoRaWAN FUOTA stack:

    GROUP_SETUP     McGroupSetupReq to every device                       [TS005 §4.3]
    SESSION_SETUP   LadderSessionReq: the spreading factor schedule       [ladder.py]
    FRAG_SETUP      FragSessionSetupReq, inside the session's lead time   [TS004 §3.3]
    BROADCAST       DataFragment per frame slot, until every device sent
                    its FragDataBlockReceivedReq                          [TS004 §3.5, §3.6]
    CLEANUP         FragSessionDeleteReq and McGroupDeleteReq

It reuses the setup and cleanup phases of `FuotaCampaign` and replaces what lies between them:
there is no status round and no repair round, the gateway simply carries on down the ladder
until it has an acknowledgement from everyone (Section III of the paper).
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass

from simulator.environment import simulation_env as sim
from simulator.lorawan.fuota.campaign import FuotaCampaign, FuotaCampaignConfig, FuotaCampaignState
from simulator.lorawan.fuota.frag_transport import (
    FRAGMENTATION_FPORT, DataFragment, FragmentationServerApplication, encode_commands,
)

from ladder import (
    FRAGMENT_FRAME_OVERHEAD, LadderRound, LadderSchedule, LadderServerApplication,
    LadderSessionReq, data_rate_of, frame_airtime, plan_rounds, spreading_factor_of,
)

logger = logging.getLogger("fuota.campaign")

#: How long before a frame slot the server hands the frame to the gateway.
SLOT_LEAD = 0.3


def largest_fragment_size(spreading_factors: list[int]) -> int:
    """
        The largest `FragSize` every round of the ladder can carry. A fragmentation session has
        one fragment size, so the slowest data rate of the ladder decides it: 48 octets once
        SF10 is part of it, 112 with SF9, 219 for SF7 and SF8 alone.
    """
    return min(
        FragmentationServerApplication.fragment_payload_size_for(data_rate_of(sf))
        for sf in spreading_factors
    )


def pad_for_coded_only_decoding(image: bytes, frag_size: int) -> bytes:
    """
        The image with as many zero fragments appended as it takes to make `NbFrag // 2` odd.

        Every coded fragment of TS004 (N > NbFrag) is the XOR of `NbFrag // 2` uncoded ones.
        When that number is even, no combination of coded fragments has full rank, so a device
        needs at least one uncoded fragment. Those are all sent in the first round, which a
        device out of reach of the first spreading factor never hears. With an odd number the
        coded fragments suffice, which is what the scheme relies on.
    """
    fragments = math.ceil(len(image) / frag_size)
    while (fragments // 2) % 2 == 0:
        fragments += 1
    return image + bytes(fragments * frag_size - len(image))


@dataclass
class SequentialSfConfig(FuotaCampaignConfig):
    """
        `data_rate` has to be the slowest of the ladder: the stack checks the fragment size
        against it. `redundancy_ratio` and `redundancy_fragments` are not used.
    """
    spreading_factors: tuple[int, ...] = (7, 8, 9, 10, 11, 12)
    #: w of the paper. None: a tenth more than the image has fragments.
    frames_per_sf: int | None = None
    #: Of the gateway, as a fraction: the frames follow each other at this limit.
    duty_cycle: float = 0.01
    slot_margin_ms: int = 100
    #: Seconds after which the gateway gives up, counted from the first frame.
    max_broadcast_time: float | None = None


class SequentialSfCampaign(FuotaCampaign):

    def __init__(self, *args, image: bytes, config: SequentialSfConfig, **kwargs) -> None:  # type: ignore[no-untyped-def]
        sfs = list(config.spreading_factors)
        assert sfs == sorted(sfs) and len(set(sfs)) == len(sfs), "The ladder goes up"
        config.data_rate = data_rate_of(sfs[-1])
        config.redundancy_ratio = None
        if config.frag_size is None:
            config.frag_size = largest_fragment_size(sfs)
        #: The firmware itself; `self.firmware` is the data block that carries it.
        self.image = image
        block = pad_for_coded_only_decoding(image, config.frag_size) if len(sfs) > 1 else image
        nb_frag = math.ceil(len(block) / config.frag_size)
        self.rounds: tuple[LadderRound, ...] = plan_rounds(
            sfs,
            frames_per_sf=config.frames_per_sf or math.ceil(1.1 * nb_frag),
            frame_len=config.frag_size + FRAGMENT_FRAME_OVERHEAD,
            duty_cycle=config.duty_cycle,
            margin_ms=config.slot_margin_ms,
            max_duration=config.max_broadcast_time,
        )
        # Only bookkeeping: what the session could send at most, on top of the block itself.
        config.redundancy_fragments = max(sum(r.frames for r in self.rounds) - nb_frag, 0)

        super().__init__(*args, firmware=block, config=config, **kwargs)
        self.ladder = LadderServerApplication(max_command_retries=config.command_retries)
        self.network_server.register_application(self.ladder)

        self.schedule: LadderSchedule | None = None
        self.frames_by_sf: dict[int, int] = {}
        self.slots_skipped = 0
        self.broadcast_ended_at: float | None = None

    def fragmentation_plan(self) -> str:
        nb_frag = math.ceil(len(self.firmware) / self.frag_size)
        padding = (len(self.firmware) - len(self.image)) // self.frag_size
        return f"{nb_frag} fragments of {self.frag_size} B ({padding} of them padding)"

    # ── The campaign ─────────────────────────────────────────────────────────────────────

    async def _run(self) -> None:
        cfg = self.config
        self._transition(FuotaCampaignState.GROUP_SETUP)
        if not await self._phase_group_setup():
            return self._fail("no device acknowledged the multicast group")

        self._transition(FuotaCampaignState.SESSION_SETUP)
        schedule = await self._phase_ladder_setup()
        if schedule is None:
            return self._fail("no device accepted the session")

        self._transition(FuotaCampaignState.FRAG_SETUP)
        deadline = schedule.start_time - cfg.session_answer_margin
        if not await self._phase_frag_setup(deadline=deadline):
            return self._fail("no device accepted the fragmentation session")

        self._transition(FuotaCampaignState.BROADCAST)
        await self._phase_ladder_broadcast(schedule)

        self._transition(FuotaCampaignState.CLEANUP)
        await self._phase_cleanup()

        complete = self.completed_devices
        if len(complete) >= max(cfg.min_participants, 1) and complete >= self.participants:
            self._transition(FuotaCampaignState.DONE)
        else:
            self._fail(
                f"{len(self.participants - complete)} of {len(self.participants)} "
                f"participant(s) did not report a usable data block"
            )

    async def _phase_ladder_setup(self) -> LadderSchedule | None:
        cfg = self.config
        session_time = math.ceil(sim.current_time() + cfg.session_lead_time)
        request = LadderSessionReq(
            group_id=cfg.group_id, session_time=session_time, dl_frequency=cfg.dl_frequency,
            rounds=self.rounds,
        )
        self.ladder.start_session(sorted(self.participants), request)

        def accepted() -> set[int]:
            return self.ladder.devices_accepted() & self.participants

        # As in the TR002 order of `FuotaCampaign`: part of the lead time is kept for the
        # fragmentation session setup that follows.
        budget = max(
            (session_time - sim.current_time()) * (1.0 - cfg.frag_setup_share)
            - cfg.session_answer_margin,
            cfg.poll_interval,
        )
        await self._wait_until(lambda: accepted() == self.participants, budget)
        if not self._keep(accepted(), FuotaCampaignState.SESSION_SETUP):
            return None
        self.schedule = request.schedule()
        self._session_time = self.schedule.start_time
        self._session_end = self.schedule.end_time
        return self.schedule

    def _everyone_reported(self) -> bool:
        reported = self.fragmentation.devices_reassembled(self.config.frag_index)
        return self.participants <= reported

    def _transmitter_free_at(self, time: float) -> bool:
        """ Whether the gateway's duty cycle lets it start a frame at `time`. """
        if self.gateway is None or self.gateway.duty_cycle is None:
            return True
        return self.gateway.duty_cycle.quiet_until <= time + 1e-6

    async def _phase_ladder_broadcast(self, schedule: LadderSchedule) -> None:
        cfg = self.config
        session = self.fragmentation.sessions[cfg.frag_index]
        group = self.network_server.get_multicast_group(cfg.mc_addr)
        assert group is not None
        n = 1
        for slot in schedule.slots():
            hand_over = slot.time - SLOT_LEAD
            if hand_over > sim.current_time():
                await sim.sleep_until(max(hand_over, sim.next_tick()))
            if self._everyone_reported():
                break
            if not self._transmitter_free_at(slot.time):
                # A unicast reply used up the airtime. A frame that leaves late is of no use
                # to a device that opens its receiver for the slot only, so the slot is left
                # empty and the fragment goes out in the next one.
                self.slots_skipped += 1
                continue
            group.data_rate = slot.data_rate
            payload = encode_commands([DataFragment(
                frag_index=session.frag_index, index_n=n, payload=session.encoder.fragment(n),
            )])
            self.network_server.schedule_multicast_downlink(
                cfg.mc_addr, fport=FRAGMENTATION_FPORT, payload=payload, at_time=slot.time,
            )
            session.highest_n_sent = n
            session.fragments_scheduled += 1
            if n <= session.nb_frag:
                self._fragments_uncoded += 1
            else:
                self._fragments_coded += 1
            sf = spreading_factor_of(slot.data_rate)
            self.frames_by_sf[sf] = self.frames_by_sf.get(sf, 0) + 1
            n += 1
        else:
            logger.warning(f"{sim.current_time():.2f}s  CAMPAIGN  the schedule ran out")

        # The acknowledgements of the last frame are spread over the BlockAckDelay window.
        await self._wait_until(
            self._everyone_reported, float(1 << (cfg.block_ack_delay + 4)) + cfg.broadcast_settle,
        )
        self.broadcast_ended_at = sim.current_time()
        # Whoever has not reported by now keeps to the schedule, but nothing more is sent.
        self._session_end = sim.current_time()

    def _finalise(self) -> None:
        super()._finalise()
        self.result.unicast_downlinks += self.ladder.downlinks_sent
        self.result.uplinks_received += self.ladder.uplinks_received
        frame_len = self.frag_size + FRAGMENT_FRAME_OVERHEAD
        self.result.multicast_airtime = sum(
            frames * frame_airtime(data_rate_of(sf), frame_len)
            for (sf, frames) in self.frames_by_sf.items()
        )
