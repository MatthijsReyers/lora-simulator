"""
State shared by the gateway and the recipients of one FUOTA session: the transmission schedule
of the proposed and FSF schemes, the group bookkeeping of the GB schemes and the "all done"
flag. The schedule is the only thing recipients know about the gateway (the paper assumes the
SF sequence and the channel are known a priori and the devices are synchronised).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from scenario import Scenario

GATEWAY_POSITION = (0.0, 0.0)


@dataclass
class Session:
    scenario: Scenario
    start_time: float
    # GB schemes: the SF assigned to every recipient and, per SF group, who still has to decode.
    assigned_sf: dict[int, int] = field(default_factory=dict)
    pending_per_group: dict[int, set[int]] = field(default_factory=dict)
    decoded: set[int] = field(default_factory=set)
    total_recipients: int = 0
    done: bool = False
    frames_sent: dict[int, int] = field(default_factory=dict)   # per SF
    end_time: float | None = None

    # ── Proposed / FSF schedule ────────────────────────────────────────────────────────────

    def sf_of_frame(self, index: int) -> int:
        """ The SF of the gateway's `index`-th frame (0-based), Section III. """
        s = self.scenario
        if s.scheme == "fsf":
            return s.fixed_sf
        return min(s.first_sf + index // s.transmissions_per_sf, s.last_sf)

    def round_switches(self) -> list[tuple[float, int]]:
        """
            (time, sf) for every change of SF in the proposed scheme: the start time of the
            first frame of each round after the first. Frames are paced at the maximum duty
            cycle, so the schedule is fixed by the parameters alone.
        """
        s = self.scenario
        if s.scheme != "proposed":
            return []
        switches = []
        time = self.start_time
        for sf in range(s.first_sf, s.last_sf):
            time += s.transmissions_per_sf * s.frame_period(sf)
            switches.append((time, sf + 1))
        return switches

    # ── Group based schemes ────────────────────────────────────────────────────────────────

    def groups(self) -> list[int]:
        """ The SFs that have at least one recipient, lowest first (the gateway's group order). """
        return sorted(self.pending_per_group)

    def group_done(self, sf: int) -> bool:
        return not self.pending_per_group.get(sf)

    # ── Progress ───────────────────────────────────────────────────────────────────────────

    def report_decoded(self, node: int, time: float) -> None:
        self.decoded.add(node)
        sf = self.assigned_sf.get(node)
        if sf is not None:
            self.pending_per_group[sf].discard(node)
        if len(self.decoded) >= self.total_recipients and not self.done:
            self.done = True
            self.end_time = time

    def stop(self, time: float) -> None:
        """ Ends the session early (the gateway hit its cap). """
        if not self.done:
            self.done = True
            self.end_time = time
