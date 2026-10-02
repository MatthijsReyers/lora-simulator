"""
Server-side FUOTA campaign orchestration: TS003 + TS005 + TS004, end to end.

Neither TS004 nor TS005 contains a normative end-to-end flow — both describe capabilities
only. The sequence is the one of LoRa Alliance TR002 "FUOTA Process Summary" Table 3, which
:class:`FuotaCampaign` runs as a single simulation task::

    IDLE
      -> GROUP_SETUP     McGroupSetupReq to every device          [TR002 step 4, TS005 §4.3]
      -> SESSION_SETUP   McClassC/BSessionReq, SessionTime ahead  [TR002 step 5, TS005 §4.5/§4.6]
      -> FRAG_SETUP      FragSessionSetupReq, MIC'd per device    [TR002 step 6, TS004 §3.3]
      -> BROADCAST       DataFragment, N = 1..NbFrag+redundancy   [TR002 step 8, TS004 §3.6]
      -> STATUS          FragSessionStatusReq / Ans               [TS004 §3.2]
      -> REPAIR*         a new session window and more coded fragments, then STATUS again
      -> CLEANUP         FragSessionDeleteReq (+ McGroupDeleteReq)
      -> DONE | FAILED

TR002 schedules the Class B/C rendezvous (step 5) *before* it sets up the fragmentation
session (step 6), so ``FragSessionSetupReq`` is delivered inside the lead time before
``SessionTime`` and the FRAG_SETUP phase is bounded by ``SessionTime`` rather than by
``setup_timeout``: a device that has not accepted the session by then cannot be fed anyway.
``session_lead_time`` therefore has to cover one unicast round trip per device on top of the
margin the session request itself needs. The widespread implementation order — fragmentation
session first, rendezvous last, which keeps the lead time short — is available with
:attr:`FuotaCampaignConfig.session_before_frag_setup` set to False.

Every phase is bounded by a timeout and the campaign carries on with whichever devices
answered, so a silent device costs time but never deadlocks the fleet. A phase that waits
for *answers* only starts counting once the multicast session window it scheduled has
closed and the devices have had an uplink opportunity: a Class C participant is mute for
the whole window (TS005 §2.7), and a round that times out against a mute fleet learns
nothing — which TS004 §3.2 would then make look like a fleet that is already done. Timeouts are polled
with ``sim.sleep``; a simulation that ends mid-campaign surfaces as
:class:`~simulator.exceptions.SimulatorException` and leaves the campaign in ``FAILED`` with
a usable :class:`FuotaCampaignResult`.

The campaign never touches the radio and never talks to a gateway: it drives the three
server-side packages, which hand their commands to the network server through
``get_downlink`` (unicast, delivered in RX1) and ``schedule_multicast_downlink`` (the
fragments). A *gateway* may be passed in, but only so the result can report what actually
went on the air.

Reference: LoRa Alliance TS003-2.0.0, TS004-2.0.0 §3, TS005-2.0.0 §4.
"""

from __future__ import annotations

import logging
import math
import random
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import Enum

from simulator.environment import simulation_env as sim
from simulator.exceptions import SimulatorException
from simulator.lora.airtime import estimate_airtime
from simulator.lorawan.application import Application
from simulator.lorawan.applications.clock_sync import ClockSyncServerApplication
from simulator.lorawan.fuota.crypto import (
    derive_data_block_int_key,
    derive_mc_ke_key,
    derive_mc_root_key,
)
from simulator.lorawan.fuota.frag_transport import (
    FragCommandType,
    FragDataBlockReceivedReq,
    FragmentationServerApplication,
    FragSessionStatusAns,
)
from simulator.lorawan.fuota.multicast_setup import (
    MulticastSetupCommandType,
    MulticastSetupServerApplication,
)
from simulator.lorawan.gateway import LoRaWanGateway
from simulator.lorawan.network_server import NetworkServer
from simulator.lorawan.region import (
    BEACON_INTERVAL,
    EU868_DATA_RATES,
    MAX_FCNT,
    max_frm_payload,
)

logger = logging.getLogger(__name__)


#: Octets a ``DataFragment`` frame costs on top of ``FragSize``: the TS004 CID and the
#: 2-octet ``Index&N`` header, plus MHDR(1) + FHDR(7) + FPort(1) + MIC(4) of the LoRaWAN
#: frame itself.
FRAGMENT_FRAME_OVERHEAD = 3 + 13

#: Largest ``TimeOut`` exponent TS005 can encode (4 bits).
MAX_SESSION_TIMEOUT_EXPONENT = 15


class FuotaCampaignState(Enum):
    """Phases of :class:`FuotaCampaign`, in the order they are entered."""

    IDLE = "IDLE"
    GROUP_SETUP = "GROUP_SETUP"
    SESSION_SETUP = "SESSION_SETUP"
    FRAG_SETUP = "FRAG_SETUP"
    BROADCAST = "BROADCAST"
    STATUS = "STATUS"
    REPAIR = "REPAIR"
    CLEANUP = "CLEANUP"
    DONE = "DONE"
    FAILED = "FAILED"


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass
class FuotaCampaignConfig:
    """Everything a campaign needs that is not the network, the devices or the firmware.

    The defaults describe a single Class C campaign on ``McGroupID`` 0 / ``FragIndex`` 0 at
    DR5, with 20% redundancy and up to two repair rounds. Anything left as None is derived
    in :class:`FuotaCampaign`'s constructor from the region table and the data block.

    :ivar frag_size: ``FragSize`` in octets. None takes the largest fragment that still
        fits one frame at :attr:`data_rate`.
    :ivar session_timeout: The TS005 4-bit ``TimeOut`` exponent. A Class C session lasts
        ``2**TimeOut`` seconds, a Class B session ``128 * 2**TimeOut``. None picks the
        smallest exponent that covers the planned broadcast.
    :ivar fragment_interval: Seconds between consecutive ``DataFragment`` frames. None uses
        the frame's time on air divided by the gateway's duty cycle, plus
        :attr:`fragment_interval_margin`. Ignored for a Class B campaign, where the ping
        slots decide the spacing.
    :ivar session_lead_time: Seconds between queueing ``McClassC/BSessionReq`` and
        ``SessionTime``. It must cover at least one uplink interval per device, since the
        request is only delivered in the RX1 window of a device's own uplink. In the TR002
        order (:attr:`session_before_frag_setup`) the whole FRAG_SETUP phase runs inside
        this lead time as well, so it needs a second round trip per device on top.
    :ivar session_before_frag_setup: True (the default) follows TR002 Table 3 and schedules
        the multicast session (step 5) before setting up the fragmentation session (step
        6); FRAG_SETUP then ends at ``SessionTime`` at the latest. False sets up the
        fragmentation session first and schedules the rendezvous last, the order ChirpStack
        and the Semtech reference implementation use, which needs no lead time for the
        fragmentation setup.
    :ivar setup_timeout: How long each unicast setup phase waits for answers before carrying
        on with whoever answered.
    :ivar status_timeout: How long one status round waits, counted from
        :meth:`FuotaCampaign.answers_possible_at` rather than from the moment the request was
        queued.
    :ivar device_uplink_interval: The fleet's periodic uplink interval. Every unicast command
        and every answer rides on a device's own uplink, so this is the campaign's clock for
        "how long before an answer could possibly arrive". It is only used for sizing waits;
        a value that is too small costs nothing but a tighter timeout.
    :ivar max_status_rounds: How many times a status phase polls. A device that did not
        answer a round is re-polled, because "no answer" is not "complete" — a device whose
        ``FragSessionStatusReq`` was lost in RX1, or that was still mute when the round
        started, would otherwise be taken for a finished one and never repaired.
    :ivar command_retries: Retransmissions of an unanswered unicast command, handed to both
        server-side packages. See
        :class:`~simulator.lorawan.fuota.frag_transport.FragmentationServerApplication`.
    :ivar status_participants: ``Participants`` of ``FragSessionStatusReq``. None means
        "ask everyone unless the devices acknowledge completion by themselves", i.e.
        ``not ack_reception``.
    """

    # ---- Multicast group (TS005 §4.3) ----
    group_id: int = 0
    mc_addr: int = 0xFF000001
    mc_key: bytes | None = None
    min_fcnt: int = 0
    max_fcnt: int = MAX_FCNT

    # ---- Fragmentation session (TS004 §3.3) ----
    frag_index: int = 0
    frag_size: int | None = None
    redundancy_ratio: float | None = 0.2
    redundancy_fragments: int | None = None
    descriptor: int = 0
    session_cnt: int = 1
    block_ack_delay: int = 0
    ack_reception: bool = True

    # ---- Radio parameters ----
    data_rate: int = 5
    dl_frequency: int = 0

    # ---- Session timing (TS005 §4.5/§4.6) ----
    class_b: bool = False
    class_b_periodicity: int = 4
    session_timeout: int | None = None
    session_lead_time: float = 60.0
    session_answer_margin: float = 2.0
    broadcast_offset: float = 1.0
    session_before_frag_setup: bool = True

    # ---- Fragment pacing (TS004 §3.6) ----
    fragment_interval: float | None = None
    fragment_interval_margin: float = 0.1
    broadcast_timeout: float | None = None
    broadcast_settle: float = 2.0

    # ---- Phase timeouts ----
    setup_timeout: float = 120.0
    status_timeout: float = 120.0
    cleanup_timeout: float = 60.0
    poll_interval: float = 0.5
    #: The fleet's periodic uplink interval; the unit every "wait for answers" is sized in.
    device_uplink_interval: float = 30.0
    #: Retransmissions of an unanswered unicast command, per server package.
    command_retries: int = 3

    # ---- Repair (TS004 §3.2, §A.3) ----
    max_repair_rounds: int = 2
    #: Coded fragments sent on top of the fleet's worst ``MissingFrag``. ``MissingFrag`` is
    #: a *rank deficit* (TS004 §3.2), so it is the exact minimum a repair round must carry;
    #: the margin covers coded fragments that turn out to be linearly dependent on what a
    #: device already holds (§A.3).
    repair_extra_fragments: int = 2
    repair_lead_time: float | None = None
    status_participants: bool | None = None
    #: Polls per status phase. The first one asks every participant, each further one only
    #: the devices that did not answer the previous one.
    max_status_rounds: int = 2
    #: Close with a ``FragSessionStatusReq(Participants = 1)`` roll call so that a device
    #: whose ``FragDataBlockReceivedReq`` was lost on the way up still gets counted
    #: (TS004 §3.2; the closing step of the LoRa Alliance FUOTA process).
    final_roll_call: bool = True

    # ---- Cleanup ----
    delete_session: bool = True
    delete_group: bool = True

    # ---- Success criterion ----
    min_participants: int = 1


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------


@dataclass
class FuotaCampaignResult:
    """What a finished campaign cost and achieved.

    :ivar phase_durations: Seconds spent in each state, summed over repeated visits.
    :ivar fragments_uncoded: ``DataFragment`` frames with ``N <= NbFrag``, i.e. the data
        block itself.
    :ivar fragments_coded: Redundancy frames of the first broadcast (``N > NbFrag``).
    :ivar fragments_repair: Frames scheduled by repair rounds.
    :ivar multicast_frames: Frames the gateway's free-running multicast scheduler put on the
        air, when a gateway was given. A **Class B** campaign's frames go out through the
        ping-slot scheduler instead, which keeps no such log, so this stays 0 there;
        :attr:`fragments_scheduled` is the figure to use in that case.
    :ivar multicast_airtime: Their total time on air, in seconds.
    :ivar duty_cycle_quiet_time: Time the gateway's limiter forced the transmitter to stay
        quiet because of those frames, 0 without a duty cycle.
    """

    success: bool = False
    state: FuotaCampaignState = FuotaCampaignState.IDLE
    started_at: float = 0.0
    finished_at: float = 0.0
    total_time: float = 0.0
    phase_durations: dict[FuotaCampaignState, float] = field(default_factory=dict)

    devices: int = 0
    participants: int = 0
    completed: int = 0
    failed: int = 0
    excluded: set[int] = field(default_factory=set)

    nb_frag: int = 0
    frag_size: int = 0
    repair_rounds: int = 0
    fragments_uncoded: int = 0
    fragments_coded: int = 0
    fragments_repair: int = 0
    fragments_scheduled: int = 0

    multicast_frames: int = 0
    multicast_airtime: float = 0.0
    duty_cycle_quiet_time: float = 0.0
    unicast_downlinks: int = 0
    uplinks_received: int = 0

    completion_time: dict[int, float] = field(default_factory=dict)
    fragments_received: dict[int, int] = field(default_factory=dict)

    @property
    def last_completion_time(self) -> float | None:
        """When the last device reported the whole block — the headline FUOTA metric."""
        return max(self.completion_time.values(), default=None)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


class _CountingServerApplication(Application):
    """Counts the uplinks and unicast downlinks of one server-side package.

    Registered on the network server in place of the package itself. ``get_downlink`` is
    counted on the way through; the non-destructive
    :meth:`~simulator.lorawan.application.Application.has_downlink` probe is forwarded
    without counting, since nothing is transmitted for it.
    """

    def __init__(self, inner: Application, campaign: "FuotaCampaign") -> None:
        self._inner = inner
        self._campaign = campaign

    def port(self) -> int:
        return self._inner.port()

    async def on_uplink(self, dev_addr: int, payload: bytes) -> None:
        self._campaign._uplinks_received += 1
        await self._inner.on_uplink(dev_addr, payload)

    async def get_downlink(self, dev_addr: int) -> bytes | None:
        payload = await self._inner.get_downlink(dev_addr)
        if payload is not None:
            self._campaign._unicast_downlinks += 1
        return payload

    async def has_downlink(self, dev_addr: int) -> bool:
        return await self._inner.has_downlink(dev_addr)


# ---------------------------------------------------------------------------
# The campaign
# ---------------------------------------------------------------------------


class FuotaCampaign:
    """Drives a whole FUOTA campaign over TS003, TS005 and TS004.

    Construct it before ``sim.run()``, call :meth:`start` (or ``sim.create_task(c.run())``)
    and read :attr:`result` afterwards::

        campaign = FuotaCampaign(
            ns, key_provider={addr: gen_app_key, ...}, firmware=image,
            config=FuotaCampaignConfig(data_rate=5, frag_size=48),
            gateway=gateway,
        )
        campaign.start()
        sim.run(simulation_length=300)
        assert campaign.state is FuotaCampaignState.DONE

    The constructor registers the three server-side packages on the network server, so the
    caller does not register them itself.
    """

    def __init__(
        self,
        network_server: NetworkServer,
        *,
        key_provider: Mapping[int, bytes],
        firmware: bytes,
        config: FuotaCampaignConfig | None = None,
        gateway: LoRaWanGateway | None = None,
        clock_sync_server: ClockSyncServerApplication | None = None,
        register_clock_sync: bool = True,
        lorawan_1_1: bool = False,
        rng: random.Random | None = None,
    ) -> None:
        """
        :param network_server: The server the packages are registered on.
        :param key_provider: ``DevAddr`` -> that device's ``GenAppKey`` (or ``AppKey`` for
            LoRaWAN 1.1). ``McKEKey`` (TS005 §4.3) and ``DataBlockIntKey`` (TS004 §3.3) are
            derived from it, so the campaign and the devices agree without further wiring.
        :param firmware: The data block to distribute.
        :param config: Campaign parameters; the defaults are used when omitted.
        :param gateway: Only read for metrics — the campaign drives the network server.
        :param clock_sync_server: An existing TS003 server package to share between
            campaigns. A new one is created when omitted.
        :param register_clock_sync: Register the TS003 package on the network server.
        :param lorawan_1_1: Use the LoRaWAN 1.1 key derivations.
        :param rng: Random source for the default ``McKey``.
        """
        if not firmware:
            raise ValueError("A FUOTA campaign needs a non-empty firmware image")
        if not key_provider:
            raise ValueError("A FUOTA campaign needs at least one device")

        self.network_server = network_server
        self.gateway = gateway
        self.firmware = bytes(firmware)
        self.config = config if config is not None else FuotaCampaignConfig()
        self.rng = rng if rng is not None else random.Random()
        self.lorawan_1_1 = lorawan_1_1

        cfg = self.config
        self._validate_config()

        #: ``McKey`` of this campaign's group; random unless the config pins it.
        self.mc_key: bytes = (
            cfg.mc_key
            if cfg.mc_key is not None
            else bytes(self.rng.randrange(256) for _ in range(16))
        )
        #: Resolved ``FragSize``.
        self.frag_size: int = (
            cfg.frag_size
            if cfg.frag_size is not None
            else FragmentationServerApplication.fragment_payload_size_for(cfg.data_rate)
        )
        limit = FragmentationServerApplication.fragment_payload_size_for(cfg.data_rate)
        if not 1 <= self.frag_size <= limit:
            raise ValueError(
                f"FragSize {self.frag_size} does not fit one frame at DR{cfg.data_rate}: "
                f"the maximum is {limit} octets "
                f"(MaxAppPl {max_frm_payload(cfg.data_rate)} minus the DataFragment header)"
            )

        #: Every device the campaign was asked to reach.
        self.devices: set[int] = set(key_provider)
        #: Devices still taking part; phases that time out shrink this set.
        self.participants: set[int] = set(key_provider)
        #: Devices dropped along the way, with the phase that dropped them.
        self.excluded: dict[int, FuotaCampaignState] = {}

        self._gen_app_keys = dict(key_provider)
        self._mc_ke_keys = {
            addr: derive_mc_ke_key(
                mc_root_key=derive_mc_root_key(key=key, lorawan_1_1=lorawan_1_1)
            )
            for addr, key in self._gen_app_keys.items()
        }
        self._data_block_keys = {
            addr: derive_data_block_int_key(key=key, lorawan_1_1=lorawan_1_1)
            for addr, key in self._gen_app_keys.items()
        }

        # ---- The three server-side packages ----
        self.clock_sync = (
            clock_sync_server
            if clock_sync_server is not None
            else ClockSyncServerApplication()
        )
        self.multicast_setup = MulticastSetupServerApplication(
            network_server,
            key_provider=self._mc_ke_keys,
            lorawan_1_1=lorawan_1_1,
            on_answer=self._on_setup_answer,
            max_command_retries=cfg.command_retries,
        )
        self.fragmentation = FragmentationServerApplication(
            network_server,
            key_provider=self._data_block_keys,
            max_command_retries=cfg.command_retries,
        )
        self.fragmentation.on_answer = self._on_frag_answer

        self._uplinks_received = 0
        self._unicast_downlinks = 0

        if register_clock_sync:
            network_server.register_application(
                _CountingServerApplication(self.clock_sync, self)
            )
        network_server.register_application(
            _CountingServerApplication(self.multicast_setup, self)
        )
        network_server.register_application(
            _CountingServerApplication(self.fragmentation, self)
        )

        # ---- State ----
        self.state = FuotaCampaignState.IDLE
        #: ``(time, state)`` for every transition, oldest first.
        self.state_history: list[tuple[float, FuotaCampaignState]] = []
        self.result = FuotaCampaignResult(devices=len(self.devices))
        self.repair_rounds = 0
        self.failure_reason: str | None = None

        self._done = False
        self._started_at = 0.0
        self._state_entered_at = 0.0
        self._session_time: float | None = None
        #: When the multicast session window of the latest session closes. Until then a
        #: Class C participant is mute, so nothing can be answered.
        self._session_end: float | None = None
        self._status_requested_at: float | None = None
        self._status_rounds = 0
        self._completion_time: dict[int, float] = {}
        self._fragments_uncoded = 0
        self._fragments_coded = 0
        self._fragments_repair = 0

    # ---- Configuration ----

    def _validate_config(self) -> None:
        cfg = self.config
        if not 0 <= cfg.group_id <= 3:
            raise ValueError(f"McGroupID must be 0..3, got {cfg.group_id}")
        if not 0 <= cfg.frag_index <= 3:
            raise ValueError(f"FragIndex must be 0..3, got {cfg.frag_index}")
        if cfg.mc_key is not None and len(cfg.mc_key) != 16:
            raise ValueError(f"McKey must be 16 bytes, got {len(cfg.mc_key)}")
        if cfg.data_rate not in EU868_DATA_RATES:
            raise ValueError(f"Unknown data rate DR{cfg.data_rate}")
        if cfg.redundancy_fragments is not None and cfg.redundancy_ratio is not None:
            raise ValueError(
                "set either redundancy_fragments or redundancy_ratio, not both"
            )
        if not 0 <= cfg.block_ack_delay <= 7:
            raise ValueError(f"BlockAckDelay must be 0..7, got {cfg.block_ack_delay}")
        if cfg.session_timeout is not None and not (
            0 <= cfg.session_timeout <= MAX_SESSION_TIMEOUT_EXPONENT
        ):
            raise ValueError(f"TimeOut must be 0..15, got {cfg.session_timeout}")
        if not 0 <= cfg.class_b_periodicity <= 7:
            raise ValueError(
                f"Periodicity must be 0..7, got {cfg.class_b_periodicity}"
            )
        if cfg.poll_interval <= 0:
            raise ValueError("poll_interval must be positive")
        if cfg.max_repair_rounds < 0:
            raise ValueError("max_repair_rounds must not be negative")
        if cfg.max_status_rounds < 1:
            raise ValueError("max_status_rounds must be at least 1")
        if cfg.device_uplink_interval <= 0:
            raise ValueError("device_uplink_interval must be positive")
        if cfg.command_retries < 0:
            raise ValueError("command_retries must not be negative")

    @property
    def ping_nb(self) -> int:
        """Ping slots per beacon period for a Class B campaign (``128 >> Periodicity``)."""
        return 128 >> self.config.class_b_periodicity

    def fragment_frame_airtime(self) -> float:
        """Time on air of one ``DataFragment`` frame at the campaign's data rate."""
        dr = EU868_DATA_RATES[self.config.data_rate]
        return estimate_airtime(
            payload_len=self.frag_size + FRAGMENT_FRAME_OVERHEAD,
            bandwidth=dr.bandwidth.to_khz(),
            spreading_factor=dr.spreading_factor.value,
            code_rate=5,
        )

    def fragment_interval(self) -> float:
        """Seconds between two fragments, from the config or from the duty cycle."""
        if self.config.fragment_interval is not None:
            return self.config.fragment_interval
        airtime = self.fragment_frame_airtime()
        if self.gateway is not None and self.gateway.duty_cycle is not None:
            airtime = airtime / self.gateway.duty_cycle.duty_cycle
        return airtime + self.config.fragment_interval_margin

    def planned_fragments(self) -> int:
        """``NbFrag`` plus the planned redundancy, before the TS004 session exists.

        The TR002 order sizes the session window (``TimeOut``) before FRAG_SETUP has created
        the server session, so this repeats the arithmetic of
        :meth:`~simulator.lorawan.fuota.frag_transport.FragmentationServerApplication.create_session`.
        """
        cfg = self.config
        nb_frag = math.ceil(len(self.firmware) / self.frag_size)
        if cfg.redundancy_ratio is not None:
            redundancy = math.ceil(nb_frag * max(cfg.redundancy_ratio, 0.0))
        else:
            redundancy = cfg.redundancy_fragments or 0
        return nb_frag + redundancy

    def session_timeout_exponent(self, fragments: int) -> int:
        """The TS005 ``TimeOut`` exponent covering a broadcast of *fragments* frames."""
        if self.config.session_timeout is not None:
            return self.config.session_timeout
        needed = self._broadcast_duration(fragments) + self.config.broadcast_offset
        unit = BEACON_INTERVAL if self.config.class_b else 1.0
        exponent = 0
        while unit * (1 << exponent) < needed and exponent < MAX_SESSION_TIMEOUT_EXPONENT:
            exponent += 1
        return exponent

    def _broadcast_duration(self, fragments: int) -> float:
        if self.config.class_b:
            periods = math.ceil(max(fragments, 1) / self.ping_nb)
            return periods * float(BEACON_INTERVAL)
        return max(fragments - 1, 0) * self.fragment_interval() + self.fragment_frame_airtime()

    # ---- Lifecycle ----

    def start(self) -> None:
        """Register the campaign as a simulation task. Call before ``sim.run()``."""
        sim.create_task(self.run(), name="fuota-campaign")

    async def run(self) -> None:
        """Run the campaign to completion. Never raises; failures land in :attr:`result`."""
        self._started_at = sim.current_time()
        self._state_entered_at = self._started_at
        try:
            await self._run()
        except SimulatorException:
            self._fail("the simulation ended during the campaign")
        finally:
            self._finalise()

    async def wait_done(self, poll_interval: float | None = None) -> bool:
        """Block until the campaign reached ``DONE`` or ``FAILED``.

        :returns: :attr:`FuotaCampaignResult.success`.
        """
        interval = poll_interval if poll_interval is not None else self.config.poll_interval
        try:
            while not self._done:
                await sim.sleep(interval)
        except SimulatorException:
            pass
        return self.result.success

    @property
    def done(self) -> bool:
        """Whether the campaign reached a terminal state."""
        return self._done

    @property
    def completed_devices(self) -> set[int]:
        """Devices known to hold the whole data block (TS004 §3.2/§3.5)."""
        return self.fragmentation.devices_complete(self.config.frag_index) & self.devices

    @property
    def failed_devices(self) -> set[int]:
        """Devices the campaign did not finish, dropped or silent ones included."""
        return self.devices - self.completed_devices

    # ---- State machine ----

    def _transition(self, state: FuotaCampaignState) -> None:
        now = sim.current_time()
        previous = self.state
        self.result.phase_durations[previous] = (
            self.result.phase_durations.get(previous, 0.0) + now - self._state_entered_at
        )
        self.state = state
        self._state_entered_at = now
        self.state_history.append((now, state))
        logger.info(
            f"{now:.2f}s  CAMPAIGN  {previous.value} -> {state.value} "
            f"({len(self.participants)} participant(s))"
        )

    def _fail(self, reason: str) -> None:
        if self.failure_reason is None:
            self.failure_reason = reason
        if self.state not in (FuotaCampaignState.DONE, FuotaCampaignState.FAILED):
            self._transition(FuotaCampaignState.FAILED)
        logger.warning(f"{sim.current_time():.2f}s  CAMPAIGN  failed: {reason}")

    async def _run(self) -> None:
        cfg = self.config

        self._transition(FuotaCampaignState.GROUP_SETUP)
        if not await self._phase_group_setup():
            return self._fail("no device acknowledged the multicast group")

        if cfg.session_before_frag_setup:
            # TR002 Table 3: rendezvous (step 5), then fragmentation session (step 6). The
            # session window is sized from the planned fragment count, and FRAG_SETUP must
            # be over before the window opens.
            self._transition(FuotaCampaignState.SESSION_SETUP)
            session_time = await self._phase_session_setup(
                cfg.session_lead_time, self.planned_fragments()
            )
            if session_time is None:
                return self._fail("no device accepted the multicast session")

            self._transition(FuotaCampaignState.FRAG_SETUP)
            if not await self._phase_frag_setup(
                deadline=session_time - cfg.session_answer_margin
            ):
                return self._fail("no device accepted the fragmentation session")
        else:
            self._transition(FuotaCampaignState.FRAG_SETUP)
            if not await self._phase_frag_setup():
                return self._fail("no device accepted the fragmentation session")

            self._transition(FuotaCampaignState.SESSION_SETUP)
            planned = self.fragmentation.sessions[cfg.frag_index].total_fragments
            session_time = await self._phase_session_setup(cfg.session_lead_time, planned)
            if session_time is None:
                return self._fail("no device accepted the multicast session")

        self._transition(FuotaCampaignState.BROADCAST)
        await self._phase_broadcast(session_time, start_n=1, count=None)

        self._transition(FuotaCampaignState.STATUS)
        await self._phase_status()

        await self._repair_rounds()

        if cfg.final_roll_call and self.participants - self.completed_devices:
            # §3.2 Participants = 1: everyone answers, including the devices that are
            # already done. It is the only way to notice a device whose completion
            # acknowledgement was lost on the way up.
            self._transition(FuotaCampaignState.STATUS)
            await self._phase_status(participants=True)

        self._transition(FuotaCampaignState.CLEANUP)
        await self._phase_cleanup()

        complete = self.completed_devices
        if len(complete) >= max(cfg.min_participants, 1) and complete >= self.participants:
            self._transition(FuotaCampaignState.DONE)
        else:
            self._fail(
                f"{len(self.participants - complete)} of {len(self.participants)} "
                f"participant(s) did not report the complete data block"
            )

    # ---- Phase 1: multicast group setup (TS005 §4.3) ----

    async def _phase_group_setup(self) -> bool:
        cfg = self.config
        self.multicast_setup.setup_group(
            sorted(self.participants),
            group_id=cfg.group_id,
            mc_addr=cfg.mc_addr,
            mc_key=self.mc_key,
            min_fcnt=cfg.min_fcnt,
            max_fcnt=cfg.max_fcnt,
            data_rate=cfg.data_rate,
            frequency=cfg.dl_frequency or None,
        )

        def acked() -> set[int]:
            return {
                addr
                for addr in self.participants
                if cfg.group_id in self.multicast_setup.state(addr).groups
            }

        await self._wait_until(
            lambda: acked() == self.participants, cfg.setup_timeout
        )
        return self._keep(acked(), FuotaCampaignState.GROUP_SETUP)

    # ---- Phase 2: fragmentation session setup (TS004 §3.3) ----

    async def _phase_frag_setup(self, deadline: float | None = None) -> bool:
        """Set up the TS004 session on every participant and wait for the answers.

        :param deadline: Simulation time by which the phase must be over, whatever
            ``setup_timeout`` says. The TR002 order passes ``SessionTime`` (less the answer
            margin): a ``FragSessionSetupReq`` that is still undelivered when the devices
            go mute for the session window is of no use to anyone.
        """
        cfg = self.config
        self.fragmentation.create_session(
            sorted(self.participants),
            frag_index=cfg.frag_index,
            data=self.firmware,
            frag_size=self.frag_size,
            session_cnt=cfg.session_cnt,
            mc_group_bit_mask=1 << cfg.group_id,
            redundancy_fragments=cfg.redundancy_fragments,
            redundancy_ratio=cfg.redundancy_ratio,
            descriptor=cfg.descriptor,
            block_ack_delay=cfg.block_ack_delay,
            ack_reception=cfg.ack_reception,
        )

        def acked() -> set[int]:
            return self.fragmentation.devices_acked_setup(cfg.frag_index) & self.participants

        timeout = cfg.setup_timeout
        if deadline is not None:
            timeout = min(timeout, max(deadline - sim.current_time(), cfg.poll_interval))
        await self._wait_until(lambda: acked() == self.participants, timeout)
        return self._keep(acked(), FuotaCampaignState.FRAG_SETUP)

    # ---- Phase 3: multicast session scheduling (TS005 §4.5/§4.6) ----

    async def _phase_session_setup(
        self, lead_time: float, fragments: int
    ) -> float | None:
        """Queue a session request and wait for the answers. Returns ``SessionTime``."""
        cfg = self.config
        now = sim.current_time()
        session_time = now + lead_time
        timeout_exponent = self.session_timeout_exponent(fragments)

        # A repair round asks for a second session on the same group, and the devices still
        # hold their answer to the first one. Count the answers each device has given so far
        # so that only a *new* one counts as accepting this request.
        answers_before = {
            addr: len(self.multicast_setup.state(addr).session_answers)
            for addr in self.participants
        }

        if cfg.class_b:
            # TS005 §4.6: SessionTime must be a multiple of one beacon period.
            session_time = math.ceil(session_time / BEACON_INTERVAL) * BEACON_INTERVAL
            self.network_server.enable_multicast_class_b(cfg.mc_addr, ping_nb=self.ping_nb)
            self.multicast_setup.start_class_b_session(
                sorted(self.participants),
                group_id=cfg.group_id,
                session_time=int(session_time),
                session_timeout=timeout_exponent,
                periodicity=cfg.class_b_periodicity,
                dl_frequency=cfg.dl_frequency,
                data_rate=cfg.data_rate,
            )
        else:
            self.multicast_setup.start_class_c_session(
                sorted(self.participants),
                group_id=cfg.group_id,
                session_time=int(session_time),
                session_timeout=timeout_exponent,
                dl_frequency=cfg.dl_frequency,
                data_rate=cfg.data_rate,
            )

        def accepted() -> set[int]:
            in_session = self.multicast_setup.devices_in_session(cfg.group_id)
            return {
                addr
                for addr in self.participants
                if addr in in_session
                and len(self.multicast_setup.state(addr).session_answers)
                > answers_before[addr]
            }

        # Never wait past SessionTime itself: a device that has not answered by then will
        # not open its window either.
        budget = max(
            session_time - sim.current_time() - cfg.session_answer_margin,
            cfg.poll_interval,
        )
        await self._wait_until(lambda: accepted() == self.participants, budget)
        if not self._keep(accepted(), FuotaCampaignState.SESSION_SETUP):
            return None

        self._session_time = session_time
        # The window the devices keep open, and therefore the earliest moment a Class C
        # participant can transmit again (TS005 §2.7). Every phase that waits for answers
        # counts its timeout from after this.
        unit = float(BEACON_INTERVAL) if cfg.class_b else 1.0
        self._session_end = session_time + unit * (1 << timeout_exponent)
        return session_time

    # ---- Phase 4: fragment broadcast (TS004 §3.6) ----

    async def _phase_broadcast(
        self, session_time: float, *, start_n: int, count: int | None, repair: bool = False
    ) -> None:
        cfg = self.config
        session = self.fragmentation.sessions[cfg.frag_index]
        interval = 0.0 if cfg.class_b else self.fragment_interval()
        start_time = session_time + cfg.broadcast_offset

        entries = self.fragmentation.broadcast_fragments(
            cfg.frag_index,
            group_addr=cfg.mc_addr,
            start_time=start_time,
            interval=interval,
            count=count,
            start_n=start_n,
        )
        for index in range(len(entries)):
            n = start_n + index
            if repair:
                self._fragments_repair += 1
            elif n <= session.nb_frag:
                self._fragments_uncoded += 1
            else:
                self._fragments_coded += 1

        timeout = (
            cfg.broadcast_timeout
            if cfg.broadcast_timeout is not None
            else self._broadcast_duration(len(entries)) * 2.0
            + max(start_time - sim.current_time(), 0.0)
            + 60.0
        )
        await self._wait_until(lambda: not self._multicast_queue_busy(), timeout)
        await self._sleep(cfg.broadcast_settle)

    def _multicast_queue_busy(self) -> bool:
        """Whether this campaign's group still has queued multicast frames."""
        return any(
            entry.group_addr == self.config.mc_addr
            for entry in self.network_server.pending_multicast_downlinks()
        )

    # ---- Phase 5: status round (TS004 §3.2) ----

    async def _phase_status(self, participants: bool | None = None) -> None:
        """Poll the fleet, re-polling whoever stayed silent (TS004 §3.2).

        The timeout only starts once the session window has closed and the devices have had
        an uplink opportunity (:meth:`answers_possible_at`): a Class C participant is mute
        for the whole window, and a round that times out against a mute fleet collects
        nothing, which the repair logic would then read as "everybody is done".

        A device that does not answer a round is polled again, up to
        :attr:`FuotaCampaignConfig.max_status_rounds` times, because its request may simply
        have been lost in RX1. Only a device that *said* it holds the block counts as
        complete.
        """
        cfg = self.config
        if participants is not None:
            participants_flag = participants
        else:
            participants_flag = (
                cfg.status_participants
                if cfg.status_participants is not None
                else not cfg.ack_reception
            )

        targets = sorted(self.participants)
        for round_index in range(max(cfg.max_status_rounds, 1)):
            if not targets:
                return
            self._status_requested_at = sim.current_time()
            self._status_rounds += 1
            self.fragmentation.request_status(
                targets, cfg.frag_index, participants=participants_flag
            )
            answered = await self._wait_until(
                self._status_round_answered,
                cfg.status_timeout,
                not_before=self.answers_possible_at(),
            )
            if answered:
                return
            targets = sorted(self._status_silent())
            if not targets:
                return
            if round_index + 1 < max(cfg.max_status_rounds, 1):
                logger.info(
                    f"{sim.current_time():.2f}s  CAMPAIGN  {len(targets)} device(s) did "
                    f"not answer the status round, polling them again "
                    f"({round_index + 2}/{cfg.max_status_rounds})"
                )

    def _status_silent(self) -> set[int]:
        """Participants that neither answered the latest round nor are known complete."""
        cfg = self.config
        complete = self.fragmentation.devices_complete(cfg.frag_index)
        requested_at = self._status_requested_at or 0.0
        silent: set[int] = set()
        for addr in self.participants:
            if addr in complete:
                continue
            report = self.fragmentation.latest_status.get((cfg.frag_index, addr))
            if report is None or report.time < requested_at:
                silent.add(addr)
        return silent

    def _status_round_answered(self) -> bool:
        """Whether every participant either answered this round or is known complete."""
        return not self._status_silent()

    def _status_unknown(self) -> set[int]:
        """Participants that have never reported anything and are not known complete.

        Their state is *unknown*, not "complete": they are what makes a repair round
        worthwhile even when every answer the server did receive said ``MissingFrag = 0``.
        """
        cfg = self.config
        complete = self.fragmentation.devices_complete(cfg.frag_index)
        return {
            addr
            for addr in self.participants
            if addr not in complete
            and (cfg.frag_index, addr) not in self.fragmentation.latest_status
        }

    # ---- Phase 6: repair rounds (TS004 §3.2, §A.3) ----

    async def _repair_rounds(self) -> None:
        cfg = self.config
        session = self.fragmentation.sessions[cfg.frag_index]
        lead = (
            cfg.repair_lead_time
            if cfg.repair_lead_time is not None
            else cfg.session_lead_time
        )

        while self.repair_rounds < cfg.max_repair_rounds:
            missing = self.fragmentation.max_missing(cfg.frag_index)
            unknown = self._status_unknown()
            if missing <= 0 and not unknown:
                break

            self.repair_rounds += 1
            count = max(missing, 0) + cfg.repair_extra_fragments
            self._transition(FuotaCampaignState.REPAIR)
            if missing > 0:
                logger.info(
                    f"{sim.current_time():.2f}s  CAMPAIGN  repair round "
                    f"{self.repair_rounds}/{cfg.max_repair_rounds}: the worst-off device "
                    f"needs {missing} more independent fragment(s), sending {count}"
                )
            else:
                # Silence is not completion (TS004 §3.2 only tells the server about the
                # devices that answered), so a round of spare coded fragments goes out for
                # the devices nothing is known about.
                logger.info(
                    f"{sim.current_time():.2f}s  CAMPAIGN  repair round "
                    f"{self.repair_rounds}/{cfg.max_repair_rounds}: "
                    f"{len(unknown)} device(s) never reported their state, sending "
                    f"{count} spare coded fragment(s)"
                )

            self._transition(FuotaCampaignState.SESSION_SETUP)
            session_time = await self._phase_session_setup(lead, count)
            if session_time is None:
                logger.warning(
                    f"{sim.current_time():.2f}s  CAMPAIGN  no device accepted the repair "
                    f"session, giving up on the repair rounds"
                )
                return

            self._transition(FuotaCampaignState.BROADCAST)
            await self._phase_broadcast(
                session_time,
                start_n=session.highest_n_sent + 1,
                count=count,
                repair=True,
            )

            self._transition(FuotaCampaignState.STATUS)
            await self._phase_status()

    # ---- Phase 7: cleanup (TS004 §3.4, TS005 §4.4) ----

    async def _phase_cleanup(self) -> None:
        cfg = self.config
        targets = sorted(self.participants)
        if not targets:
            return

        if cfg.delete_session:
            self.fragmentation.delete_session(targets, cfg.frag_index)
        if cfg.delete_group:
            self.multicast_setup.delete_group(targets, cfg.group_id)

        session = self.fragmentation.sessions.get(cfg.frag_index)

        def answered() -> bool:
            if session is None:
                return True
            if cfg.delete_session and set(session.delete_answers) < self.participants:
                return False
            return True

        await self._wait_until(answered, cfg.cleanup_timeout)

    # ---- Answer hooks ----

    def _on_setup_answer(self, dev_addr: int, command: MulticastSetupCommandType) -> None:
        return None

    def _on_frag_answer(self, dev_addr: int, command: FragCommandType) -> None:
        complete = False
        match command:
            case FragDataBlockReceivedReq():
                complete = command.frag_index == self.config.frag_index
            case FragSessionStatusAns():
                complete = (
                    command.frag_index == self.config.frag_index
                    and not command.session_does_not_exist
                    and command.missing_frag == 0
                )
            case _:
                return None
        if complete and dev_addr not in self._completion_time:
            self._completion_time[dev_addr] = sim.current_time()
            logger.info(
                f"{sim.current_time():.2f}s  CAMPAIGN  0x{dev_addr:08X} holds the "
                f"complete data block "
                f"({len(self._completion_time)}/{len(self.participants)})"
            )
        return None

    # ---- Timing helpers ----

    async def _sleep(self, duration: float) -> None:
        if duration > 0:
            await sim.sleep(duration)

    def answers_possible_at(self) -> float:
        """Earliest time a participant could answer a command queued now.

        While a multicast session window is open a Class C participant does not transmit at
        all (TS005 §2.7), and a device stack hands the radio back over one further slot, so
        the first uplink that can *carry* an answer is a couple of uplink intervals after the
        window closed — and the command itself is only delivered in the RX1 window of one of
        those uplinks. A timeout that starts counting before that point is not a timeout on
        the device, it is a timeout on the session window, and with the TS005 ``TimeOut``
        being a power of two the window can overrun the broadcast by almost as much again.

        :returns: Now, when no session window is pending.
        """
        now = sim.current_time()
        if self._session_end is None:
            return now
        return max(now, self._session_end + 2.0 * self.config.device_uplink_interval)

    async def _wait_until(
        self, predicate: Callable[[], bool], timeout: float, not_before: float = 0.0
    ) -> bool:
        """Poll *predicate* until it holds or *timeout* seconds elapsed.

        Polling rather than waiting on an event keeps a phase from deadlocking when the
        condition can only be satisfied by traffic that never arrives.

        :param not_before: Simulation time from which the timeout starts counting. Used by
            the phases that wait for device answers; see :meth:`answers_possible_at`.
        """
        deadline = max(sim.current_time(), not_before) + max(timeout, 0.0)
        while True:
            if predicate():
                return True
            if sim.current_time() >= deadline:
                logger.info(
                    f"{sim.current_time():.2f}s  CAMPAIGN  {self.state.value} timed out "
                    f"after {timeout:.1f}s, carrying on"
                )
                return False
            await sim.sleep(
                min(self.config.poll_interval, max(deadline - sim.current_time(), 1e-6))
            )

    def _keep(self, survivors: set[int], phase: FuotaCampaignState) -> bool:
        """Narrow the participant set to *survivors*, recording who was dropped."""
        dropped = self.participants - survivors
        for addr in dropped:
            self.excluded[addr] = phase
            logger.warning(
                f"{sim.current_time():.2f}s  CAMPAIGN  dropping 0x{addr:08X}: no usable "
                f"answer in {phase.value}"
            )
        self.participants = set(survivors)
        return len(self.participants) >= max(self.config.min_participants, 1)

    # ---- Result ----

    def _finalise(self) -> None:
        cfg = self.config
        now = sim.current_time()
        self.result.phase_durations[self.state] = (
            self.result.phase_durations.get(self.state, 0.0) + now - self._state_entered_at
        )
        self._state_entered_at = now

        session = self.fragmentation.sessions.get(cfg.frag_index)
        complete = self.completed_devices

        result = self.result
        result.state = self.state
        result.success = self.state is FuotaCampaignState.DONE
        result.started_at = self._started_at
        result.finished_at = now
        result.total_time = now - self._started_at
        result.devices = len(self.devices)
        result.participants = len(self.participants)
        result.completed = len(complete)
        result.failed = len(self.devices - complete)
        result.excluded = set(self.excluded)
        result.nb_frag = session.nb_frag if session is not None else 0
        result.frag_size = self.frag_size
        result.repair_rounds = self.repair_rounds
        result.fragments_uncoded = self._fragments_uncoded
        result.fragments_coded = self._fragments_coded
        result.fragments_repair = self._fragments_repair
        result.fragments_scheduled = (
            self._fragments_uncoded + self._fragments_coded + self._fragments_repair
        )
        result.unicast_downlinks = self._unicast_downlinks
        result.uplinks_received = self._uplinks_received
        result.completion_time = dict(self._completion_time)
        result.fragments_received = {
            addr: report.nb_frag_received
            for (index, addr), report in self.fragmentation.latest_status.items()
            if index == cfg.frag_index
        }

        if self.gateway is not None:
            frames = [
                entry
                for entry in self.gateway.multicast_log
                if entry[1] == cfg.mc_addr
            ]
            result.multicast_frames = len(frames)
            dr = EU868_DATA_RATES[cfg.data_rate]
            airtime = sum(
                estimate_airtime(
                    payload_len=entry[3],
                    bandwidth=dr.bandwidth.to_khz(),
                    spreading_factor=dr.spreading_factor.value,
                    code_rate=5,
                )
                for entry in frames
            )
            result.multicast_airtime = airtime
            if self.gateway.duty_cycle is not None:
                result.duty_cycle_quiet_time = self.gateway.duty_cycle.quiet_time(airtime)

        self._done = True
        logger.info(
            f"{now:.2f}s  CAMPAIGN  finished in state {self.state.value} after "
            f"{result.total_time:.1f}s: {result.completed}/{result.devices} device(s) "
            f"complete, {result.fragments_scheduled} fragment(s) sent in "
            f"{result.repair_rounds} repair round(s)"
        )
