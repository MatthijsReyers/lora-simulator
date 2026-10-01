"""Unit tests for the FUOTA campaign state machine (``simulator.lorawan.fuota.campaign``).

The campaign is driven here without a gateway and without radios: a handful of *virtual*
devices hand encrypted uplinks straight to :meth:`NetworkServer.handle_uplink` and feed the
downlink that comes back into the TS004/TS005 device packages, exactly the way
``tests/test_lorawan_clock_sync.py`` drives TS003. A second helper task plays the gateway's
multicast scheduler: it drains the network server's multicast queue and hands every
``DataFragment`` to the devices that are "in the session".

That keeps every phase transition, timeout and repair-round calculation observable without a
PHY layer deciding the outcome. The real-radio runs live in
``tests/test_lorawan_fuota_end_to_end.py``.

Covered:

- the phase sequence IDLE -> ... -> DONE and the timings recorded in ``state_history``;
- a device that never uplinks being dropped after ``setup_timeout``;
- a device answering ``McGroupSetupReq`` with ``IDerror`` being dropped;
- the repair-round arithmetic: ``max_missing + repair_extra_fragments`` more coded fragments
  from ``highest_n_sent + 1``;
- configuration validation;
- the metrics in :class:`FuotaCampaignResult`.
"""

from __future__ import annotations

import logging

import pytest

from simulator.environment import simulation_env as sim
from simulator.exceptions import SimulatorException
from simulator.lorawan.crypto import compute_data_mic, encrypt_frm_payload
from simulator.lorawan.enums.frame_types import MType
from simulator.lorawan.frame import FCtrl, FHDR, MACPayload, MHDR, PHYPayload
from simulator.lorawan.fuota.campaign import (
    FuotaCampaign,
    FuotaCampaignConfig,
    FuotaCampaignState,
)
from simulator.lorawan.fuota.frag_transport import (
    FRAGMENTATION_FPORT,
    DataFragment,
    FragmentationDeviceApplication,
    parse_downlink_commands,
)
from simulator.lorawan.fuota.multicast_setup import (
    MULTICAST_SETUP_FPORT,
    McGroupSetupAns,
    MulticastSetupDeviceApplication,
    encode_commands,
)
from simulator.lorawan.network_server import NetworkServer

# ── Test credentials ────────────────────────────────────────────────────────
NWK_S_KEY = bytes.fromhex("2B7E151628AED2A6ABF7158809CF4F3C")
APP_S_KEY = bytes.fromhex("3C4F9C098815F7ABA6D2AE281615E72B")

DEV_ADDRS = [0x26020001, 0x26020002, 0x26020003]
GEN_APP_KEYS = {addr: bytes([index + 0x40]) * 16 for index, addr in enumerate(DEV_ADDRS)}

MC_ADDR = 0xFF000002
DATA_RATE = 5
FRAG_SIZE = 40
#: 800 octets -> NbFrag = 20, the smallest block the TS004 FEC is still efficient on (§A.3).
FIRMWARE = bytes((index * 29 + 7) & 0xFF for index in range(800))


def _build_uplink(dev_addr: int, fcnt: int, fport: int, payload: bytes) -> bytes:
    """A real encrypted, MIC'd uplink PHYPayload — the clock-sync test's helper."""
    encrypted = encrypt_frm_payload(
        APP_S_KEY, dev_addr=dev_addr, fcnt=fcnt, uplink=True, payload=payload,
    )
    fhdr = FHDR(dev_addr=dev_addr, fctrl=FCtrl(), fcnt=fcnt)
    mac_payload = MACPayload(fhdr=fhdr, fport=fport, frm_payload=encrypted)
    mhdr = MHDR(mtype=MType.UNCONFIRMED_DATA_UP)
    mhdr_and_payload = bytes([mhdr.encode()]) + mac_payload.encode(uplink=True)
    mic = compute_data_mic(
        NWK_S_KEY,
        dev_addr=dev_addr,
        fcnt=fcnt,
        uplink=True,
        mhdr_and_payload=mhdr_and_payload,
    )
    return PHYPayload(mhdr=mhdr, mac_payload=mac_payload, mic=mic).encode()


# ═══════════════════════════════════════════════════════════════════════════
# A fleet of virtual devices
# ═══════════════════════════════════════════════════════════════════════════

class VirtualDevice:
    """A device reduced to its two FUOTA packages and a frame counter.

    It has no radio and no :class:`~simulator.lorawan.device.LoRaWanDevice`, so the TS005
    package keeps its multicast contexts internally and never schedules a real session; the
    answer it produces is the same either way, which is all the campaign reacts to.
    """

    def __init__(self, dev_addr: int) -> None:
        self.dev_addr = dev_addr
        self.fcnt_up = 0
        self.fcnt_down = 0
        self.gen_app_key = GEN_APP_KEYS[dev_addr]
        self.multicast_setup = MulticastSetupDeviceApplication(
            None, gen_app_key=self.gen_app_key
        )
        self.fragmentation = FragmentationDeviceApplication(
            None, gen_app_key=self.gen_app_key
        )
        #: When False the device's uplinks never reach the network server.
        self.reachable = True
        #: Drop every n-th fragment to model a weak receiver; 0 disables it.
        self.drop_every = 0
        #: Refuse every ``McGroupSetupReq`` with ``IDerror`` (TS005 §4.3).
        self.refuse_group = False
        self.uplinks = 0
        self.downlinks = 0
        self._fragments_seen = 0

    # -- uplink path --

    def _next_payload(self) -> tuple[int, bytes]:
        pending = self.multicast_setup.pop_pending_uplink()
        if pending is not None:
            return MULTICAST_SETUP_FPORT, pending
        pending = self.fragmentation.pop_pending_uplink()
        if pending is not None:
            return FRAGMENTATION_FPORT, pending
        return FRAGMENTATION_FPORT, b""

    async def uplink(self, ns: NetworkServer) -> None:
        """Send one uplink and deliver whatever downlink comes back in 'RX1'."""
        fport, payload = self._next_payload()
        if self.refuse_group and fport == MULTICAST_SETUP_FPORT:
            payload = self._refuse(payload)
        if not self.reachable:
            return

        raw = _build_uplink(self.dev_addr, self.fcnt_up, fport, payload)
        self.fcnt_up += 1
        self.uplinks += 1

        downlink = await ns.handle_uplink(raw)
        if downlink is None:
            return

        phy = PHYPayload.decode_data(downlink)
        assert phy.mac_payload is not None
        if phy.mac_payload.fport is None or phy.mac_payload.fport == 0:
            return
        plaintext = encrypt_frm_payload(
            APP_S_KEY,
            dev_addr=self.dev_addr,
            fcnt=phy.mac_payload.fhdr.fcnt,
            uplink=False,
            payload=phy.mac_payload.frm_payload,
        )
        self.downlinks += 1
        await self._dispatch(phy.mac_payload.fport, plaintext)

    def _refuse(self, payload: bytes) -> bytes:
        """Replace an ``McGroupSetupAns`` with one carrying ``IDerror`` (TS005 §4.3)."""
        return encode_commands(
            [McGroupSetupAns(group_id=0, id_error=True)]
        ) if payload else payload

    async def _dispatch(self, fport: int, payload: bytes) -> None:
        if fport == MULTICAST_SETUP_FPORT:
            await self.multicast_setup.on_downlink(payload)
        elif fport == FRAGMENTATION_FPORT:
            await self.fragmentation.on_downlink(payload)

    # -- multicast path --

    async def receive_multicast(self, payload: bytes) -> None:
        """Deliver a multicast FPort 201 payload, honouring :attr:`drop_every`."""
        if self.drop_every:
            commands = parse_downlink_commands(payload)
            if any(isinstance(command, DataFragment) for command in commands):
                self._fragments_seen += 1
                if self._fragments_seen % self.drop_every == 0:
                    return
        await self.fragmentation.on_downlink(payload)


class Fleet:
    """Network server, campaign and virtual devices, plus the two driver tasks."""

    def __init__(
        self,
        *,
        config: FuotaCampaignConfig | None = None,
        firmware: bytes = FIRMWARE,
        device_count: int = 3,
        uplink_interval: float = 1.0,
    ) -> None:
        sim.logger.setLevel(logging.WARNING)
        self.ns = NetworkServer(default_data_rate=DATA_RATE)
        self.addrs = DEV_ADDRS[:device_count]
        for addr in self.addrs:
            self.ns.register_device(addr, NWK_S_KEY, APP_S_KEY)

        self.devices = {addr: VirtualDevice(addr) for addr in self.addrs}
        self.uplink_interval = uplink_interval
        self.campaign = FuotaCampaign(
            self.ns,
            key_provider={addr: GEN_APP_KEYS[addr] for addr in self.addrs},
            firmware=firmware,
            config=config if config is not None else _config(),
        )
        #: Multicast payloads the virtual gateway put "on the air".
        self.multicast_sent: list[tuple[float, bytes]] = []

    def start(self) -> None:
        self.campaign.start()
        sim.create_task(self._uplink_loop())
        sim.create_task(self._multicast_loop())

    async def _uplink_loop(self) -> None:
        """Give every device an uplink opportunity, round robin."""
        try:
            while sim.is_running():
                for device in self.devices.values():
                    await sim.sleep(self.uplink_interval)
                    await device.uplink(self.ns)
        except SimulatorException:
            return

    async def _multicast_loop(self) -> None:
        """Stand in for the gateway: transmit due multicast frames to the whole group."""
        try:
            while sim.is_running():
                await sim.sleep(0.05)
                due = self.ns.pop_due_multicast_downlinks(sim.current_time())
                for entry in due:
                    self.multicast_sent.append((sim.current_time(), entry.payload))
                    for device in self.devices.values():
                        await device.receive_multicast(entry.payload)
        except SimulatorException:
            return


def _config(**overrides: object) -> FuotaCampaignConfig:
    """A campaign configuration tuned for a fast, radio-less run."""
    defaults: dict[str, object] = dict(
        mc_addr=MC_ADDR,
        mc_key=bytes(range(16)),
        frag_size=FRAG_SIZE,
        data_rate=DATA_RATE,
        redundancy_ratio=None,
        redundancy_fragments=3,
        session_lead_time=12.0,
        session_timeout=8,
        fragment_interval=0.1,
        broadcast_settle=1.0,
        setup_timeout=20.0,
        status_timeout=20.0,
        cleanup_timeout=10.0,
        poll_interval=0.2,
        max_repair_rounds=0,
    )
    defaults.update(overrides)
    return FuotaCampaignConfig(**defaults)  # type: ignore[arg-type]


# ═══════════════════════════════════════════════════════════════════════════
# Configuration validation
# ═══════════════════════════════════════════════════════════════════════════

class TestConfigValidation:
    def _campaign(self, **overrides: object) -> FuotaCampaign:
        ns = NetworkServer(default_data_rate=DATA_RATE)
        ns.register_device(DEV_ADDRS[0], NWK_S_KEY, APP_S_KEY)
        return FuotaCampaign(
            ns,
            key_provider={DEV_ADDRS[0]: GEN_APP_KEYS[DEV_ADDRS[0]]},
            firmware=FIRMWARE,
            config=_config(**overrides),
        )

    def test_frag_size_beyond_the_frame_is_refused(self):
        with pytest.raises(ValueError, match="does not fit one frame"):
            self._campaign(frag_size=220, data_rate=5)

    def test_frag_size_at_the_limit_is_accepted(self):
        # DR5: MaxAppPl 222 minus the 3-octet DataFragment header.
        campaign = self._campaign(frag_size=219, data_rate=5)
        assert campaign.frag_size == 219

    def test_frag_size_defaults_to_the_region_maximum(self):
        campaign = self._campaign(frag_size=None, data_rate=3)
        assert campaign.frag_size == 112

    def test_both_redundancy_knobs_is_refused(self):
        with pytest.raises(ValueError, match="not both"):
            self._campaign(redundancy_ratio=0.2, redundancy_fragments=3)

    def test_group_id_out_of_range(self):
        with pytest.raises(ValueError, match="McGroupID"):
            self._campaign(group_id=4)

    def test_frag_index_out_of_range(self):
        with pytest.raises(ValueError, match="FragIndex"):
            self._campaign(frag_index=7)

    def test_short_mc_key(self):
        with pytest.raises(ValueError, match="McKey"):
            self._campaign(mc_key=b"\x00" * 8)

    def test_empty_firmware(self):
        ns = NetworkServer()
        with pytest.raises(ValueError, match="non-empty firmware"):
            FuotaCampaign(ns, key_provider={1: b"\x00" * 16}, firmware=b"")

    def test_no_devices(self):
        ns = NetworkServer()
        with pytest.raises(ValueError, match="at least one device"):
            FuotaCampaign(ns, key_provider={}, firmware=FIRMWARE)

    def test_session_timeout_exponent_covers_the_broadcast(self):
        campaign = self._campaign(session_timeout=None, fragment_interval=1.0)
        # 23 fragments one second apart need more than 16 s and at most 32 s.
        assert campaign.session_timeout_exponent(23) == 5

    def test_fragment_interval_follows_the_airtime_without_a_gateway(self):
        campaign = self._campaign(fragment_interval=None)
        assert campaign.fragment_interval() == pytest.approx(
            campaign.fragment_frame_airtime() + 0.1
        )


# ═══════════════════════════════════════════════════════════════════════════
# The happy path
# ═══════════════════════════════════════════════════════════════════════════

class TestCampaignStateMachine:
    def test_every_phase_runs_in_order_and_reaches_done(self):
        fleet = Fleet()
        fleet.start()
        sim.run(simulation_length=90)

        campaign = fleet.campaign
        assert campaign.state is FuotaCampaignState.DONE
        assert campaign.done is True

        states = [state for (_time, state) in campaign.state_history]
        assert states == [
            FuotaCampaignState.GROUP_SETUP,
            FuotaCampaignState.FRAG_SETUP,
            FuotaCampaignState.SESSION_SETUP,
            FuotaCampaignState.BROADCAST,
            FuotaCampaignState.STATUS,
            FuotaCampaignState.CLEANUP,
            FuotaCampaignState.DONE,
        ]
        # Transitions are recorded with the time they happened, in order.
        times = [time for (time, _state) in campaign.state_history]
        assert times == sorted(times)

    def test_all_devices_complete_and_hold_the_firmware(self):
        fleet = Fleet()
        fleet.start()
        sim.run(simulation_length=90)

        assert fleet.campaign.completed_devices == set(fleet.addrs)
        assert fleet.campaign.failed_devices == set()
        for device in fleet.devices.values():
            # The session itself is gone: CLEANUP deleted it (TS004 §3.4).
            assert device.fragmentation.sessions == {}
            assert device.fragmentation.completed_blocks[0] == FIRMWARE

    def test_result_metrics_are_sane(self):
        fleet = Fleet()
        fleet.start()
        sim.run(simulation_length=90)

        result = fleet.campaign.result
        assert result.success is True
        assert result.state is FuotaCampaignState.DONE
        assert result.devices == 3
        assert result.participants == 3
        assert result.completed == 3
        assert result.failed == 0
        assert result.nb_frag == 20
        assert result.frag_size == FRAG_SIZE
        assert result.fragments_uncoded == 20
        assert result.fragments_coded == 3
        assert result.fragments_repair == 0
        assert result.fragments_scheduled == 23
        assert result.repair_rounds == 0
        # The virtual gateway is not a LoRaWanGateway, so no airtime is reported.
        assert result.multicast_frames == 0
        assert len(fleet.multicast_sent) == 23
        # Setup commands went out as unicast downlinks and every answer came back.
        assert result.unicast_downlinks >= 3 * 4
        assert result.uplinks_received >= 3 * 4
        assert set(result.completion_time) == set(fleet.addrs)
        assert all(time > 0 for time in result.completion_time.values())
        assert result.total_time > 0
        assert sum(result.phase_durations.values()) == pytest.approx(
            result.total_time, abs=1e-6
        )

    def test_wait_done_returns_the_success_flag(self):
        fleet = Fleet()
        observed: list[bool] = []

        async def watcher() -> None:
            observed.append(await fleet.campaign.wait_done())

        fleet.start()
        sim.create_task(watcher())
        sim.run(simulation_length=90)

        assert observed == [True]

    def test_the_group_and_session_are_torn_down(self):
        fleet = Fleet()
        fleet.start()
        sim.run(simulation_length=90)

        session = fleet.campaign.fragmentation.sessions[0]
        assert set(session.delete_answers) == set(fleet.addrs)
        for device in fleet.devices.values():
            assert device.fragmentation.sessions == {}
            assert device.multicast_setup.groups == {}


# ═══════════════════════════════════════════════════════════════════════════
# Partial fleets: timeouts and error answers
# ═══════════════════════════════════════════════════════════════════════════

class TestPartialFleet:
    def test_a_silent_device_is_dropped_after_the_setup_timeout(self):
        fleet = Fleet(config=_config(setup_timeout=8.0))
        silent = fleet.devices[DEV_ADDRS[2]]
        silent.reachable = False
        fleet.start()
        sim.run(simulation_length=90)

        campaign = fleet.campaign
        assert campaign.state is FuotaCampaignState.DONE
        assert campaign.participants == {DEV_ADDRS[0], DEV_ADDRS[1]}
        assert campaign.excluded == {DEV_ADDRS[2]: FuotaCampaignState.GROUP_SETUP}
        assert campaign.result.excluded == {DEV_ADDRS[2]}
        # The campaign still finished for the rest, but the fleet is not complete.
        assert campaign.completed_devices == {DEV_ADDRS[0], DEV_ADDRS[1]}
        assert campaign.failed_devices == {DEV_ADDRS[2]}
        assert campaign.result.devices == 3
        assert campaign.result.participants == 2

    def test_an_error_answer_drops_the_device_in_group_setup(self):
        fleet = Fleet(config=_config(setup_timeout=8.0))
        refusing = fleet.devices[DEV_ADDRS[1]]
        refusing.refuse_group = True
        fleet.start()
        sim.run(simulation_length=90)

        campaign = fleet.campaign
        # The device reported IDerror for McGroupID 0, so the group never came up on it.
        assert campaign.multicast_setup.state(DEV_ADDRS[1]).setup_errors == {0}
        assert campaign.multicast_setup.state(DEV_ADDRS[1]).groups == {}
        assert campaign.excluded == {DEV_ADDRS[1]: FuotaCampaignState.GROUP_SETUP}
        assert campaign.participants == {DEV_ADDRS[0], DEV_ADDRS[2]}
        assert campaign.state is FuotaCampaignState.DONE

    def test_a_fleet_that_never_answers_fails_rather_than_hanging(self):
        fleet = Fleet(config=_config(setup_timeout=5.0))
        for device in fleet.devices.values():
            device.reachable = False
        fleet.start()
        sim.run(simulation_length=40)

        campaign = fleet.campaign
        assert campaign.state is FuotaCampaignState.FAILED
        assert campaign.result.success is False
        assert campaign.failure_reason is not None
        assert "multicast group" in campaign.failure_reason
        assert campaign.done is True


# ═══════════════════════════════════════════════════════════════════════════
# Repair rounds (TS004 §3.2, §A.3)
# ═══════════════════════════════════════════════════════════════════════════

class TestRepairRounds:
    def test_a_repair_round_sends_max_missing_plus_the_extra_fragments(self):
        fleet = Fleet(
            config=_config(
                redundancy_fragments=0,
                max_repair_rounds=1,
                repair_extra_fragments=2,
                session_lead_time=10.0,
            )
        )
        # One device loses every fifth fragment: 20 sent, 16 received, 4 missing.
        fleet.devices[DEV_ADDRS[2]].drop_every = 5
        fleet.start()
        sim.run(simulation_length=140)

        campaign = fleet.campaign
        assert campaign.repair_rounds == 1
        assert FuotaCampaignState.REPAIR in [
            state for (_time, state) in campaign.state_history
        ]

        result = campaign.result
        assert result.fragments_uncoded == 20
        assert result.fragments_coded == 0
        # max_missing was 4, plus the two spare coded fragments of §A.3.
        assert result.fragments_repair == 6
        assert result.fragments_scheduled == 26
        assert len(fleet.multicast_sent) == 26

        assert campaign.state is FuotaCampaignState.DONE
        for device in fleet.devices.values():
            assert device.fragmentation.completed_blocks[0] == FIRMWARE

    def test_the_repair_round_continues_the_fragment_index(self):
        fleet = Fleet(
            config=_config(
                redundancy_fragments=0, max_repair_rounds=1, session_lead_time=10.0,
            )
        )
        fleet.devices[DEV_ADDRS[2]].drop_every = 5
        fleet.start()
        sim.run(simulation_length=140)

        # N runs 1..20 in the first broadcast and 21..26 in the repair round, never
        # repeating a coded fragment: a repeat would be linearly dependent and useless.
        indices = []
        for (_time, payload) in fleet.multicast_sent:
            for command in parse_downlink_commands(payload):
                if isinstance(command, DataFragment):
                    indices.append(command.index_n)
        assert indices == list(range(1, 27))
        assert fleet.campaign.fragmentation.sessions[0].highest_n_sent == 26

    def test_no_repair_round_when_nothing_is_missing(self):
        fleet = Fleet(config=_config(max_repair_rounds=3))
        fleet.start()
        sim.run(simulation_length=90)

        assert fleet.campaign.repair_rounds == 0
        assert FuotaCampaignState.REPAIR not in [
            state for (_time, state) in fleet.campaign.state_history
        ]

    def test_the_round_budget_is_respected(self):
        """With no repair budget the campaign ends FAILED instead of looping forever."""
        fleet = Fleet(
            config=_config(redundancy_fragments=0, max_repair_rounds=0)
        )
        fleet.devices[DEV_ADDRS[2]].drop_every = 5
        fleet.start()
        sim.run(simulation_length=90)

        campaign = fleet.campaign
        assert campaign.repair_rounds == 0
        assert campaign.state is FuotaCampaignState.FAILED
        assert campaign.completed_devices == {DEV_ADDRS[0], DEV_ADDRS[1]}
        assert campaign.failed_devices == {DEV_ADDRS[2]}
        report = campaign.fragmentation.latest_status[(0, DEV_ADDRS[2])]
        assert report.missing_frag == 4
        assert campaign.result.fragments_received[DEV_ADDRS[2]] == 16


# ═══════════════════════════════════════════════════════════════════════════
# Session parameters handed to TS005
# ═══════════════════════════════════════════════════════════════════════════

class TestSessionParameters:
    def test_session_time_is_one_lead_time_ahead(self):
        fleet = Fleet(config=_config(session_lead_time=15.0))
        fleet.start()
        sim.run(simulation_length=90)

        request = fleet.campaign.multicast_setup._last_session_request(0)
        assert request is not None
        entered = next(
            time
            for (time, state) in fleet.campaign.state_history
            if state is FuotaCampaignState.SESSION_SETUP
        )
        assert request.session_time == pytest.approx(int(entered + 15.0), abs=1)
        assert request.data_rate == DATA_RATE
        assert request.timeout_seconds == 1 << 8

    def test_every_device_reports_a_positive_time_to_start(self):
        fleet = Fleet()
        fleet.start()
        sim.run(simulation_length=90)

        for addr in fleet.addrs:
            time_to_start = fleet.campaign.multicast_setup.device_state[addr].time_to_start
            assert 0 < time_to_start[0] <= 15
