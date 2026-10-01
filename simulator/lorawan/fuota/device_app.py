"""
Shared base for device-side FUOTA application-layer packages.

LoRa Alliance application packages (TS003/TS004/TS005) answer downlink commands with uplink
commands on the same FPort. Unlike MAC commands those answers do not piggy-back on the next
frame automatically: the device firmware has to send an uplink carrying them, usually after a
package-specific delay (e.g. TS004 ``BlockAckDelay``) so that a whole multicast group does
not answer in the same instant.

``FuotaDeviceApplication`` keeps a queue of pending uplinks and, when a ``LoRaWanDevice`` is
attached, sends them itself from a child task. Without a device the queue can be drained with
:meth:`pop_pending_uplink`, which is what the codec-level unit tests and
:class:`~simulator.lorawan.fuota.device_stack.FuotaDeviceStack` do.

Two properties of the queue matter for spec conformance:

- **Each entry carries its own payload and its own ``ready_at``.** A package that queues an
  answer with a 10 s spreading delay and then an immediate one must not have the two swapped,
  which is exactly what popping "the queue head" after sleeping would do.
- **A payload may be late-bound.** ``queue_uplink`` accepts either ``bytes`` or a zero-argument
  callable returning ``bytes``, evaluated the instant before the uplink is handed to the
  radio. TS005 ``TimeToStart`` is specified relative to the *uplink*, so the only correct
  moment to encode it is at transmission time (TS005-2.0.0 §4.5).
"""

from __future__ import annotations

import logging
import random
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Union

from simulator.environment import simulation_env as sim
from simulator.exceptions import SimulatorException
from simulator.lorawan.application import Application

if TYPE_CHECKING:
    from simulator.lorawan.device import LoRaWanDevice

logger = logging.getLogger(__name__)


#: What may be queued as an uplink: finished octets, or a builder evaluated at send time.
UplinkPayload = Union[bytes, Callable[[], bytes]]


@dataclass
class PendingUplink:
    """One queued uplink: when it may go out, and what it carries.

    :ivar ready_at: Simulation time from which the payload may be transmitted. Entries are
        drained in ``ready_at`` order, ties broken by insertion order (:ivar:`seq`).
    :ivar payload: Either the octets themselves or a zero-argument builder, resolved by
        :meth:`resolve` immediately before the uplink is sent.
    :ivar seq: Monotonic insertion counter, so equal delays keep FIFO ordering.
    :ivar cancelled: Set by :meth:`FuotaDeviceApplication.cancel_pending`; a cancelled entry
        is never transmitted.
    """

    ready_at: float
    payload: UplinkPayload
    seq: int = 0
    cancelled: bool = field(default=False, compare=False)

    def resolve(self) -> bytes:
        """The octets to transmit, building them now if the payload is late-bound."""
        if callable(self.payload):
            return self.payload()
        return self.payload


class FuotaDeviceApplication(Application):
    """
        Device-side application package with an outgoing uplink queue.

        Subclasses implement :meth:`port` and :meth:`on_downlink` and call
        :meth:`queue_uplink` for every answer they want to send.
    """

    def __init__(self, device: LoRaWanDevice | None = None, rng: random.Random | None = None):
        self.device = device
        self.rng = rng if rng is not None else random.Random()
        self._pending_uplinks: list[PendingUplink] = []
        self._uplink_seq = 0
        #: Number of uplinks actually transmitted through the attached device.
        self.uplinks_sent = 0

    async def on_uplink(self, dev_addr: int, payload: bytes) -> None:
        """Device-side packages never receive uplinks."""
        return None

    # ---- Outgoing uplink queue ----

    @property
    def pending_uplinks(self) -> list[bytes]:
        """Payloads queued but not yet sent (or popped), in the order they will go out.

        Late-bound payloads are built to answer this, so reading the property is only a
        snapshot of what *would* be sent at the current simulation time.
        """
        return [entry.resolve() for entry in self._ordered_pending()]

    @property
    def pending_entries(self) -> list[PendingUplink]:
        """The queued entries themselves, in the order they will go out."""
        return self._ordered_pending()

    def next_ready_at(self) -> float | None:
        """``ready_at`` of the entry that will go out next, or None when the queue is empty."""
        ordered = self._ordered_pending()
        return ordered[0].ready_at if ordered else None

    def _ordered_pending(self) -> list[PendingUplink]:
        return sorted(
            (e for e in self._pending_uplinks if not e.cancelled),
            key=lambda e: (e.ready_at, e.seq),
        )

    def pop_pending_uplink(self, now: float | None = None) -> bytes | None:
        """Take the next pending uplink payload, or None when there is nothing to send.

        :param now: When given, only entries whose ``ready_at`` has been reached are
            eligible — this is how :class:`~simulator.lorawan.fuota.device_stack.FuotaDeviceStack`
            honours TS004 ``BlockAckDelay`` for a package that has no device of its own.
            With None (the default) the oldest-ready entry is taken regardless of its delay,
            which is what the codec-level tests want.
        """
        for entry in self._ordered_pending():
            if now is not None and entry.ready_at > now:
                continue
            self._remove(entry)
            return entry.resolve()
        return None

    def cancel_pending(self, entry: PendingUplink) -> bool:
        """Drop a queued entry before it is transmitted; True when it was still pending."""
        if entry.cancelled or entry not in self._pending_uplinks:
            return False
        entry.cancelled = True
        self._remove(entry)
        return True

    def _remove(self, entry: PendingUplink) -> None:
        try:
            self._pending_uplinks.remove(entry)
        except ValueError:  # pragma: no cover - already drained
            pass

    async def queue_uplink(
        self, payload: UplinkPayload, delay: float = 0.0,
    ) -> PendingUplink:
        """
            Queue an uplink payload for this package's FPort.

            With a device attached the payload is transmitted from a child task after
            ``delay`` seconds of simulation time; otherwise it waits in the queue for
            :meth:`pop_pending_uplink`, which honours the same delay when the caller passes
            it the current time.

            :param payload: The octets, or a zero-argument callable building them at send
                time (see :data:`UplinkPayload`).
            :param delay: Seconds of simulation time to wait before this payload may be
                transmitted. Delays are per payload: a later entry with a shorter delay goes
                out first, and entries with the same ``ready_at`` keep FIFO order.
            :returns: The queue entry, so a caller driving a retransmission loop can cancel
                a copy that was never sent.
        """
        # Outside a running simulation there is no clock to measure a delay against, so the
        # queue degenerates to plain FIFO — which is what the codec-level tests drive.
        ready_at = sim.current_time() + max(0.0, delay) if sim.is_running() else 0.0
        entry = PendingUplink(
            ready_at=ready_at, payload=payload, seq=self._uplink_seq,
        )
        self._uplink_seq += 1
        self._pending_uplinks.append(entry)
        if self.device is not None and sim.is_running():
            await sim.start_child_task(self._send_later(entry, delay))
        return entry

    async def _send_later(self, entry: PendingUplink, delay: float) -> None:
        try:
            if delay > 0:
                await sim.sleep(delay)
            if entry.cancelled or self.device is None:
                return
            if entry not in self._pending_uplinks:
                # Something else (the device stack, a test) already drained this entry.
                return
            self._remove(entry)
            payload = entry.resolve()
            logger.debug(
                f"{sim.current_time():.2f}s  FUOTA-DEV  uplink on FPort {self.port()} "
                f"({len(payload)} bytes)"
            )
            await self.device.send_uplink(self.port(), payload)
            self.uplinks_sent += 1
        except SimulatorException:
            pass
