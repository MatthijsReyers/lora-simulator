"""
Device-side FUOTA stack: the three LoRa Alliance packages a FUOTA-capable end device runs,
plus the firmware-style behaviour loop that carries their answers.

A real FUOTA-capable node runs three application-layer packages side by side:

================  ======  =============================================================
FPort             Spec    Package
================  ======  =============================================================
202               TS003   Application clock synchronisation (``AppTimeReq``/``Ans``)
200               TS005   Remote Multicast Setup
201               TS004   Fragmented Data Block Transport
================  ======  =============================================================

:class:`FuotaDeviceStack` wires all three onto one
:class:`~simulator.lorawan.device.LoRaWanDevice`, derives their key material from the
device's root key, and runs the periodic application uplink every battery-powered Class A
node sends anyway. That loop is what makes the whole campaign work: the network server can
only deliver a unicast command in the RX1 window that follows an uplink, so the device's own
traffic is what paces :class:`~simulator.lorawan.fuota.campaign.FuotaCampaign`'s setup phases.

Three deliberate decisions, all visible in the constructor:

- **The TS005 package is attached to the device, the TS004 package is not.**
  :class:`~simulator.lorawan.fuota.multicast_setup.MulticastSetupDeviceApplication` needs the
  device to create multicast contexts and to schedule the Class B/C session, so it gets one
  and transmits its own answers. The TS004 package only ever needs the device to *send*, and
  its answers (``FragSessionStatusAns``, ``FragDataBlockReceivedReq``) are produced while a
  multicast session may still be open — a moment at which a Class C device cannot transmit.
  It is therefore driven without a device and its queue is drained by this loop, which only
  ever transmits outside a session window. Set ``self_transmit_answers=True`` to attach it
  anyway. The queue is drained **by readiness**, not blindly: an answer the package held
  back for its TS004 ``BlockAckDelay`` spreading, or a ``FragDataBlockReceivedReq``
  retransmission that is still inside its retry interval, stays queued until it is due, so
  ``block_ack_delay`` is an effective knob on this path too.
- **Uplinks pause while a multicast session is running** (``device.active_session()``), which
  is both what a Class C device has to do and what TS005 §2.7 describes. Override with
  ``uplink_during_session=True``.
- **One uplink per slot**, carrying the oldest pending package answer and otherwise a small
  application payload. The network server returns at most one downlink per uplink, so there
  is nothing to gain from sending more.

Reference: LoRa Alliance TS003-2.0.0 §2, TS004-2.0.0 §3, TS005-2.0.0 §4.
"""

from __future__ import annotations

import logging
import random
from collections.abc import Callable
from dataclasses import dataclass, field

from simulator.environment import simulation_env as sim
from simulator.exceptions import SimulatorException
from simulator.lorawan.application import Application
from simulator.lorawan.applications.clock_sync import (
    CLOCK_SYNC_FPORT,
    ClockSyncApplication,
)
from simulator.lorawan.device import LoRaWanDevice
from simulator.lorawan.enums.operating_mode import OperatingMode
from simulator.lorawan.fuota.frag_transport import (
    FRAGMENTATION_FPORT,
    FragmentationDeviceApplication,
)
from simulator.lorawan.fuota.multicast_setup import (
    MULTICAST_SETUP_FPORT,
    MulticastSetupDeviceApplication,
)

logger = logging.getLogger(__name__)


#: FPort the stack's own periodic application payload travels on. 1–223 are free for
#: applications; the FUOTA packages occupy 200–202.
DEFAULT_APPLICATION_FPORT = 10

#: Seconds the loop waits after a multicast session closed before it transmits again. The
#: Class C receive loop polls the radio in one-second steps, so it still owns the receiver
#: for up to a second after the device reverted to Class A.
DEFAULT_POST_SESSION_DELAY = 1.5


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


@dataclass
class FuotaDeviceMetrics:
    """What one device did during a campaign.

    :ivar uplinks_by_fport: Uplinks this loop transmitted, per FPort. Answers the TS005
        package sent on its own are **not** counted here — see :attr:`package_uplinks`.
    :ivar downlinks_by_fport: Downlinks delivered to an application, per FPort. Multicast
        fragments land on FPort 201 like unicast TS004 commands do.
    :ivar images_received: Images the device reassembled **and** verified. One that failed
        its data-block MIC is discarded (TS004 §3.3) and counted in :attr:`mic_failures`
        instead.
    :ivar mic_failures: ``FragIndex`` count whose reassembled block failed the MIC check.
    :ivar energy_joules: Total energy the device's radio consumed, or None when the radio
        exposes no power consumer.
    """

    dev_addr: int
    uplinks_sent: int = 0
    package_uplinks: int = 0
    uplinks_by_fport: dict[int, int] = field(default_factory=dict)
    downlinks_by_fport: dict[int, int] = field(default_factory=dict)
    fragments_received: int = 0
    fragments_dropped: int = 0
    images_received: int = 0
    mic_failures: int = 0
    image_bytes: int = 0
    completion_time: float | None = None
    clock_offset_seconds: int | None = None
    multicast_groups: int = 0
    energy_joules: float | None = None


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


class _CountingApplication(Application):
    """Per-FPort downlink counter that forwards to the real package.

    Registered on the device in place of the package itself, so every downlink the device
    dispatches is counted before the package sees it. With no inner application it is a
    plain sink, which is what the stack uses for its own application FPort.
    """

    def __init__(
        self,
        fport: int,
        inner: Application | None,
        counts: dict[int, int],
        log: list[tuple[float, int, bytes]],
    ) -> None:
        self._fport = fport
        self._inner = inner
        self._counts = counts
        self._log = log

    def port(self) -> int:
        return self._fport

    async def on_uplink(self, dev_addr: int, payload: bytes) -> None:
        return None

    async def on_downlink(self, payload: bytes) -> None:
        self._counts[self._fport] = self._counts.get(self._fport, 0) + 1
        self._log.append((sim.current_time(), self._fport, payload))
        if self._inner is not None:
            await self._inner.on_downlink(payload)


# ---------------------------------------------------------------------------
# The stack
# ---------------------------------------------------------------------------


class FuotaDeviceStack:
    """The three FUOTA packages plus a firmware-style behaviour loop, on one device.

    Typical use, before ``sim.run()``::

        device = LoRaWanDevice(session=DeviceSession(addr, nwk_s_key, app_s_key))
        stack = FuotaDeviceStack(device, gen_app_key=GEN_APP_KEY, uplink_interval=5.0)
        stack.start()

    After the run, :attr:`received_images` holds the reconstructed firmware per
    ``FragIndex`` — only the images whose TS004 §3.3 data-block MIC verified, since a block
    that fails it "SHALL NOT be used" and is discarded — and :meth:`metrics` summarises the
    device's share of the campaign.

    Reference: LoRa Alliance TS003/TS004/TS005.
    """

    def __init__(
        self,
        device: LoRaWanDevice,
        *,
        gen_app_key: bytes,
        lorawan_1_1: bool = False,
        uplink_fport: int = DEFAULT_APPLICATION_FPORT,
        uplink_interval: float = 30.0,
        uplink_jitter: float = 0.0,
        uplink_payload: bytes = b"\x00",
        first_uplink: float = 1.0,
        stop_after: float | None = None,
        clock_sync_interval: float | None = None,
        clock_sync_at_start: bool = True,
        run_uplink_loop: bool = True,
        uplink_during_session: bool = False,
        self_transmit_answers: bool = False,
        post_session_delay: float = DEFAULT_POST_SESSION_DELAY,
        class_b_from: float | None = None,
        class_b_until: float | None = None,
        rng: random.Random | None = None,
        on_firmware_received: Callable[[int, bytes, int], None] | None = None,
        fragmentation_factory: Callable[..., FragmentationDeviceApplication] | None = None,
    ) -> None:
        """
        :param device: The end device the packages run on. It must already be activated.
        :param gen_app_key: ``GenAppKey`` (LoRaWAN 1.0.x) or ``AppKey`` (1.1+). Both the
            TS005 ``McKEKey`` and the TS004 ``DataBlockIntKey`` are derived from it.
        :param lorawan_1_1: Use the LoRaWAN 1.1 key derivations.
        :param uplink_fport: FPort the periodic application payload goes out on.
        :param uplink_interval: Seconds between application uplinks. The slot grid is
            absolute, so the RX windows of one uplink never push the next one late.
        :param uplink_jitter: Maximum uniform jitter, in seconds, added to each slot. Keeps
            a fleet started at the same instant from colliding at the gateway.
        :param uplink_payload: The payload itself; keep it small, it is only there to open
            an RX1 window.
        :param first_uplink: Simulation time of the first slot.
        :param stop_after: Stop transmitting at this simulation time. An uplink needs its
            RX1/RX2 windows afterwards, so leave a few seconds before the end of the run.
        :param clock_sync_interval: Seconds between TS003 ``AppTimeReq``s, or None for a
            single request at startup.
        :param clock_sync_at_start: Send the first ``AppTimeReq`` on the first slot.
        :param run_uplink_loop: False builds the packages but starts no task, which is what
            a test driving the device by hand wants.
        :param uplink_during_session: Keep transmitting while a multicast session is open.
            A Class C device cannot do that, so the default is False.
        :param self_transmit_answers: Attach the device to the TS004 package as well, so it
            transmits its answers from its own child tasks instead of through this loop.
        :param post_session_delay: Seconds to wait after a session closed before the first
            uplink, while the Class C receive loop lets go of the radio.
        :param class_b_from: Switch the device to Class B at this time, so it holds beacon
            lock before a TS005 Class B multicast session opens (§4.6 assumes it does).
        :param class_b_until: Switch back to Class A at this time. A Class B device spends
            most of its time waiting for beacons and ping slots, which makes the RX1 window
            after an uplink a far less reliable place to deliver a unicast command; dropping
            back to Class A once the multicast session is over restores it.
        :param rng: Random source for the slot jitter.
        :param on_firmware_received: Called ``(frag_index, data, descriptor)`` when a data
            block is reassembled *and* its data-block MIC verified. A block that fails the
            check is discarded without calling it (TS004 §3.3).
        :param fragmentation_factory: Builds the TS004 device package. Used by tests that
            need a lossy variant; it is called with the same keyword arguments the default
            :class:`FragmentationDeviceApplication` gets.
        """
        assert device.session is not None, "The device must be activated before the stack"
        assert 1 <= uplink_fport <= 223, f"FPort must be 1-223, got {uplink_fport}"
        assert uplink_interval > 0, "uplink_interval must be positive"

        self.device = device
        self.dev_addr: int = device.session.dev_addr
        self.gen_app_key = gen_app_key
        self.lorawan_1_1 = lorawan_1_1
        self.uplink_fport = uplink_fport
        self.uplink_interval = uplink_interval
        self.uplink_jitter = uplink_jitter
        self.uplink_payload = uplink_payload
        self.first_uplink = first_uplink
        self.stop_after = stop_after
        self.clock_sync_interval = clock_sync_interval
        self.clock_sync_at_start = clock_sync_at_start
        self.run_uplink_loop = run_uplink_loop
        self.uplink_during_session = uplink_during_session
        self.post_session_delay = post_session_delay
        self.class_b_from = class_b_from
        self.class_b_until = class_b_until
        self.rng = rng if rng is not None else random.Random()
        self.on_firmware_received = on_firmware_received

        # ---- The three packages ----
        self.clock_sync = ClockSyncApplication()
        self.multicast_setup = MulticastSetupDeviceApplication(
            device, gen_app_key=gen_app_key, lorawan_1_1=lorawan_1_1, rng=self.rng,
        )
        factory = (
            fragmentation_factory
            if fragmentation_factory is not None
            else FragmentationDeviceApplication
        )
        self.fragmentation: FragmentationDeviceApplication = factory(
            device if self_transmit_answers else None,
            gen_app_key=gen_app_key,
            lorawan_1_1=lorawan_1_1,
            on_block_received=self._on_block_received,
            rng=self.rng,
        )

        # ---- Metrics ----
        #: Downlinks delivered per FPort.
        self.downlinks_received: dict[int, int] = {}
        #: ``(time, fport, payload)`` for every downlink delivered to an application.
        self.downlink_log: list[tuple[float, int, bytes]] = []
        #: Uplinks this loop transmitted, per FPort.
        self.uplinks_by_fport: dict[int, int] = {}
        #: Uplinks this loop transmitted in total.
        self.uplinks_sent = 0
        #: Reconstructed, MIC-verified firmware images by ``FragIndex``.
        self.received_images: dict[int, bytes] = {}
        #: ``FragIndex`` values whose reassembled block failed its data-block MIC.
        self.mic_failures: set[int] = set()
        #: Simulation time the first image was reassembled and verified.
        self.completion_time: float | None = None

        self._next_clock_sync: float | None = (
            first_uplink if clock_sync_at_start else None
        )
        self._was_in_session = False
        self._started = False

        for fport, inner in (
            (MULTICAST_SETUP_FPORT, self.multicast_setup),
            (FRAGMENTATION_FPORT, self.fragmentation),
            (CLOCK_SYNC_FPORT, self.clock_sync),
            (uplink_fport, None),
        ):
            device.register_application(
                _CountingApplication(
                    fport, inner, self.downlinks_received, self.downlink_log
                )
            )

    # ---- Lifecycle ----

    def start(self) -> None:
        """Register the behaviour loop (and the Class B switch) as simulation tasks.

        Must be called before ``sim.run()``, like every other ``sim.create_task`` caller.
        """
        assert not self._started, "FuotaDeviceStack.start() was already called"
        self._started = True
        if self.run_uplink_loop:
            sim.create_task(self.run(), name=f"fuota-device-{self.dev_addr:08X}")
        if self.class_b_from is not None:
            sim.create_task(self._enter_class_b(self.class_b_from))

    async def run(self) -> None:
        """The behaviour loop itself, for a caller that starts its own task."""
        try:
            await self._uplink_loop()
        except SimulatorException:
            return

    async def _enter_class_b(self, at_time: float) -> None:
        try:
            await sim.sleep_until(max(at_time, sim.next_tick()))
            await self.device.switch_mode(OperatingMode.CLASS_B)
            logger.info(
                f"{sim.current_time():.2f}s  FUOTA-DEV  0x{self.dev_addr:08X} acquired "
                f"Class B"
            )
            if self.class_b_until is None:
                return
            await sim.sleep_until(max(self.class_b_until, sim.next_tick()))
            if self.device.operating_mode is OperatingMode.CLASS_B:
                await self.device.switch_mode(OperatingMode.CLASS_A)
                logger.info(
                    f"{sim.current_time():.2f}s  FUOTA-DEV  0x{self.dev_addr:08X} left "
                    f"Class B"
                )
        except SimulatorException:
            return

    # ---- The behaviour loop ----

    async def _uplink_loop(self) -> None:
        slot = self.first_uplink
        while sim.is_running():
            now = sim.current_time()
            while slot <= now:
                slot += self.uplink_interval
            target = slot
            if self.uplink_jitter > 0:
                target += self.rng.uniform(0.0, self.uplink_jitter)
            if self.stop_after is not None and target > self.stop_after:
                return
            await sim.sleep_until(max(target, sim.next_tick()))
            if not sim.is_running():
                return
            await self._slot()

    async def _slot(self) -> None:
        """One behaviour-loop slot: at most one uplink."""
        session = self.device.active_session()
        if session is not None and not self.uplink_during_session:
            # Only a Class C session leaves the receiver running; a Class B one opens short
            # ping slots like any other window and needs no recovery afterwards.
            self._was_in_session = session.mode is OperatingMode.CLASS_C
            return

        if self._was_in_session:
            # The session just closed. Give the Class C receive loop time to notice and
            # empty whatever it queued for other members of the group, then skip this slot.
            self._was_in_session = False
            await self._recover_from_session()
            return

        fport, payload = self._next_uplink()
        await self.device.send_uplink(fport, payload)
        self.uplinks_sent += 1
        self.uplinks_by_fport[fport] = self.uplinks_by_fport.get(fport, 0) + 1

    async def _recover_from_session(self) -> None:
        """Hand the radio back after a multicast session window closed.

        A device leaving Class C keeps its receiver on until the continuous-RX loop next
        polls it, and the frames it queued in the meantime would be handed to the next RX1
        window instead of the reply the device is actually waiting for. Draining and
        powering the receiver down is what a Class A device does between windows anyway, so
        this stays correct once the device layer does it by itself.
        """
        if self.post_session_delay > 0:
            await sim.sleep(self.post_session_delay)
        try:
            while await self.device.radio.receive_data_nowait() is not None:
                pass
            await self.device.radio.off()
        except (RuntimeError, AssertionError):  # pragma: no cover - radio bookkeeping
            return

    def _next_uplink(self) -> tuple[int, bytes]:
        """Pick what this slot's uplink carries, oldest *ready* package answer first.

        Both queues are drained with the current simulation time, so an answer a package
        deliberately held back — TS004 ``BlockAckDelay`` spreading (§3.2/§3.3) and the
        ``FragDataBlockReceivedReq`` retransmission interval (§3.5) — is skipped until its
        delay has elapsed instead of going out on the first slot after it was produced.
        """
        now = sim.current_time()

        pending = self.multicast_setup.pop_pending_uplink(now)
        if pending is not None:
            return MULTICAST_SETUP_FPORT, pending

        pending = self.fragmentation.pop_pending_uplink(now)
        if pending is not None:
            return FRAGMENTATION_FPORT, pending

        if self._next_clock_sync is not None and now >= self._next_clock_sync:
            self._next_clock_sync = (
                now + self.clock_sync_interval
                if self.clock_sync_interval is not None
                else None
            )
            return CLOCK_SYNC_FPORT, self.clock_sync.build_time_request(int(now))

        return self.uplink_fport, self.uplink_payload

    # ---- Completion ----

    def _on_block_received(self, frag_index: int, data: bytes, descriptor: int) -> None:
        """Take delivery of a reassembled block, unless its MIC says not to (§3.3).

        TS004 §3.3: a data block whose ``DataBlockIntKey`` MIC does not verify "SHALL NOT
        be used". The bytes are of the right length but not the firmware that was sent, so
        the image is dropped instead of stored — the device reports the ``MICError`` bit to
        the server from the TS004 package, and the campaign counts it as a failure.

        ``mic_ok`` is None when the device holds no ``DataBlockIntKey`` and could not check;
        there is nothing to act on then, so the block is kept.
        """
        session = self.fragmentation.sessions.get(frag_index)
        if session is not None and session.mic_ok is False:
            self.mic_failures.add(frag_index)
            logger.warning(
                f"{sim.current_time():.2f}s  FUOTA-DEV  0x{self.dev_addr:08X} DISCARDED "
                f"firmware image {frag_index} ({len(data)} octets, "
                f"Descriptor=0x{descriptor:08X}): data block MIC check FAILED"
            )
            return

        self.received_images[frag_index] = data
        if self.completion_time is None:
            self.completion_time = sim.current_time()
        logger.info(
            f"{sim.current_time():.2f}s  FUOTA-DEV  0x{self.dev_addr:08X} holds firmware "
            f"image {frag_index} ({len(data)} octets, Descriptor=0x{descriptor:08X})"
        )
        if self.on_firmware_received is not None:
            self.on_firmware_received(frag_index, data, descriptor)

    def is_complete(self, frag_index: int = 0) -> bool:
        """Whether the device holds a reassembled, MIC-verified image of a ``FragIndex``."""
        return frag_index in self.received_images

    @property
    def energy_consumed(self) -> float | None:
        """Total energy the device's radio consumed, in joules, or None if unavailable."""
        consumer = getattr(self.device.radio, "power_consumer", None)
        getter = getattr(consumer, "get_total_energy_consumed", None)
        if getter is None:
            return None
        try:
            return float(getter())
        except (AssertionError, AttributeError):  # pragma: no cover - sim torn down
            return None

    def metrics(self) -> FuotaDeviceMetrics:
        """A snapshot of this device's contribution to the campaign."""
        image = self.received_images.get(min(self.received_images), b"") if self.received_images else b""
        return FuotaDeviceMetrics(
            dev_addr=self.dev_addr,
            uplinks_sent=self.uplinks_sent,
            package_uplinks=self.multicast_setup.uplinks_sent
            + self.fragmentation.uplinks_sent,
            uplinks_by_fport=dict(self.uplinks_by_fport),
            downlinks_by_fport=dict(self.downlinks_received),
            fragments_received=self.fragmentation.fragments_received,
            fragments_dropped=self.fragmentation.fragments_dropped,
            images_received=len(self.received_images),
            mic_failures=len(self.mic_failures),
            image_bytes=len(image),
            completion_time=self.completion_time,
            clock_offset_seconds=self.clock_sync.offset_seconds,
            multicast_groups=len(self.device.multicast_groups),
            energy_joules=self.energy_consumed,
        )
