"""
Shared base for device-side FUOTA application-layer packages.

LoRa Alliance application packages (TS003/TS004/TS005) answer downlink commands with uplink
commands on the same FPort. Unlike MAC commands those answers do not piggy-back on the next
frame automatically: the device firmware has to send an uplink carrying them, usually after a
package-specific delay (e.g. TS004 ``BlockAckDelay``) so that a whole multicast group does
not answer in the same instant.

``FuotaDeviceApplication`` keeps a queue of pending uplink payloads and, when a
``LoRaWanDevice`` is attached, sends them itself from a child task. Without a device the
queue can be drained with :meth:`pop_pending_uplink`, which is what the codec-level unit
tests do.
"""

from __future__ import annotations

import logging
import random
from collections import deque
from typing import TYPE_CHECKING

from simulator.environment import simulation_env as sim
from simulator.exceptions import SimulatorException
from simulator.lorawan.application import Application

if TYPE_CHECKING:
    from simulator.lorawan.device import LoRaWanDevice

logger = logging.getLogger(__name__)


class FuotaDeviceApplication(Application):
    """
        Device-side application package with an outgoing uplink queue.

        Subclasses implement :meth:`port` and :meth:`on_downlink` and call
        :meth:`queue_uplink` for every answer they want to send.
    """

    def __init__(self, device: LoRaWanDevice | None = None, rng: random.Random | None = None):
        self.device = device
        self.rng = rng if rng is not None else random.Random()
        self._pending_uplinks: deque[bytes] = deque()
        #: Number of uplinks actually transmitted through the attached device.
        self.uplinks_sent = 0

    async def on_uplink(self, dev_addr: int, payload: bytes) -> None:
        """Device-side packages never receive uplinks."""
        return None

    # ---- Outgoing uplink queue ----

    @property
    def pending_uplinks(self) -> list[bytes]:
        """Payloads queued but not yet sent (or popped), oldest first."""
        return list(self._pending_uplinks)

    def pop_pending_uplink(self) -> bytes | None:
        """Take the oldest pending uplink payload, or None if the queue is empty."""
        if not self._pending_uplinks:
            return None
        return self._pending_uplinks.popleft()

    async def queue_uplink(self, payload: bytes, delay: float = 0.0) -> None:
        """
            Queue an uplink payload for this package's FPort.

            With a device attached the payload is transmitted from a child task after
            ``delay`` seconds of simulation time; otherwise it waits in the queue for
            :meth:`pop_pending_uplink`.
        """
        self._pending_uplinks.append(payload)
        if self.device is not None and sim.is_running():
            await sim.start_child_task(self._send_later(delay))

    async def _send_later(self, delay: float) -> None:
        try:
            if delay > 0:
                await sim.sleep(delay)
            payload = self.pop_pending_uplink()
            if payload is None or self.device is None:
                return
            logger.debug(
                f"{sim.current_time():.2f}s  FUOTA-DEV  uplink on FPort {self.port()} "
                f"({len(payload)} bytes)"
            )
            await self.device.send_uplink(self.port(), payload)
            self.uplinks_sent += 1
        except SimulatorException:
            pass
