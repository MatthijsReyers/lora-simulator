"""Full-simulation FUOTA campaigns: TS003 + TS005 + TS004 over a real PHY layer.

Every test here runs a real :func:`sim.run`: a :class:`NetworkServer`, a
:class:`LoRaWanGateway` with its multicast scheduler and duty-cycle limiter, and N
:class:`~simulator.lorawan.device.LoRaWanDevice` instances each carrying a
:class:`~simulator.lorawan.fuota.device_stack.FuotaDeviceStack`. Frames really go on the
air, so a fragment that arrives while a device is transmitting is genuinely lost, and the
setup commands only reach a device in the RX1 window that follows one of its own uplinks.

The orchestration is entirely :class:`~simulator.lorawan.fuota.campaign.FuotaCampaign`'s:
the tests wire up the network and then only inspect what came out.

Covered:

(a) three devices and a 2 kB image with no losses: everyone completes, ``DONE``, no repair;
(b) five devices, two of them losing fragments: the redundancy and a repair round rescue them;
(c) a 10% duty-cycle gateway: the fragments are spaced accordingly and the campaign still
    completes;
(d) the Class B variant of the same flow (TS005 §4.6), with beacons and ping slots;
(e) a device the network server does not know: it is dropped after ``setup_timeout`` and
    the rest of the fleet finishes anyway.
"""

from __future__ import annotations

import logging
import random

import pytest

from simulator.environment import simulation_env as sim
from simulator.lora.airtime import estimate_airtime
from simulator.lora.phy_layer import LoraPhyLayer
from simulator.lorawan.device import DeviceSession, LoRaWanDevice
from simulator.lorawan.fuota.campaign import (
    FuotaCampaign,
    FuotaCampaignConfig,
    FuotaCampaignState,
)
from simulator.lorawan.fuota.device_stack import FuotaDeviceStack
from simulator.lorawan.fuota.frag_transport import (
    DataFragment,
    FragmentationDeviceApplication,
    parse_downlink_commands,
)
from simulator.lorawan.gateway import LoRaWanGateway
from simulator.lorawan.network_server import NetworkServer
from simulator.lorawan.region import BEACON_INTERVAL, EU868_DATA_RATES

# ── Test credentials and radio parameters ───────────────────────────────────
NWK_S_KEY = bytes.fromhex("2B7E151628AED2A6ABF7158809CF4F3C")
APP_S_KEY = bytes.fromhex("3C4F9C098815F7ABA6D2AE281615E72B")

DEV_ADDRS = [0x26030001 + index for index in range(5)]
GEN_APP_KEYS = {addr: bytes([0x80 + index]) * 16 for index, addr in enumerate(DEV_ADDRS)}

MC_ADDR = 0xFF000003
MC_KEY = bytes.fromhex("A0A1A2A3A4A5A6A7A8A9AAABACADAEAF")

DATA_RATE = 5
#: Everything stays on the gateway's own channel: the LoRaWAN layer is single-channel, so a
#: session on another DLFreq would simply never be heard (see the README's caveat).
DL_FREQUENCY = 868_100_000

#: 2 kB of firmware, 43 fragments of 48 octets with 16 octets of padding.
FIRMWARE_2KB = bytes((index * 31 + 5) & 0xFF for index in range(2048))
FRAG_SIZE = 48


# ═══════════════════════════════════════════════════════════════════════════
# Fixtures and helpers
# ═══════════════════════════════════════════════════════════════════════════

class LossyFragmentationDeviceApplication(FragmentationDeviceApplication):
    """A receiver that throws away fragments with a fixed, seeded probability.

    Models a device at the edge of coverage without having to arrange the radio geometry
    for it, which keeps the loss pattern identical from run to run.
    """

    def __init__(self, *args, loss: float = 0.0, loss_seed: int = 0, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.loss = loss
        self.loss_rng = random.Random(loss_seed)
        self.fragments_lost_on_air = 0

    async def _handle_data_fragment(self, fragment: DataFragment) -> None:
        if self.loss_rng.random() < self.loss:
            self.fragments_lost_on_air += 1
            return
        await super()._handle_data_fragment(fragment)


def _lossy_factory(loss: float, seed: int):
    """A ``fragmentation_factory`` for :class:`FuotaDeviceStack` with a seeded loss model."""

    def factory(device, **kwargs) -> FragmentationDeviceApplication:
        return LossyFragmentationDeviceApplication(
            device, loss=loss, loss_seed=seed, **kwargs
        )

    return factory


def _quiet_logging() -> None:
    sim.logger.setLevel(logging.WARNING)
    LoraPhyLayer().logger.setLevel(logging.WARNING)


def _build_network(
    *,
    config: FuotaCampaignConfig,
    firmware: bytes,
    device_count: int = 3,
    unregistered: set[int] | None = None,
    duty_cycle: float | None = None,
    class_b_enabled: bool = False,
    first_uplink: float = 1.0,
    stagger: float = 2.5,
    uplink_interval: float = 6.0,
    stop_after: float | None = None,
    class_b_from: float | None = None,
    class_b_until: float | None = None,
    lossy: dict[int, tuple[float, int]] | None = None,
) -> tuple[NetworkServer, LoRaWanGateway, FuotaCampaign, list[FuotaDeviceStack]]:
    """Network server, gateway, campaign and N devices, all started but not yet run.

    :param unregistered: Devices the network server is deliberately not told about, so
        their uplinks are dropped and the campaign never hears from them.
    :param lossy: ``index -> (loss probability, seed)`` for devices that should drop
        fragments.
    """
    _quiet_logging()
    unregistered = unregistered or set()
    lossy = lossy or {}

    addrs = DEV_ADDRS[:device_count]
    ns = NetworkServer(default_data_rate=DATA_RATE)
    for addr in addrs:
        if addr not in unregistered:
            ns.register_device(addr, NWK_S_KEY, APP_S_KEY)

    gateway = LoRaWanGateway(
        network_server=ns,
        data_rate=DATA_RATE,
        duty_cycle=duty_cycle,
        class_b_enabled=class_b_enabled,
    )
    gateway.radio.logger.setLevel(logging.WARNING)

    campaign = FuotaCampaign(
        ns,
        key_provider={addr: GEN_APP_KEYS[addr] for addr in addrs},
        firmware=firmware,
        config=config,
        gateway=gateway,
    )

    stacks: list[FuotaDeviceStack] = []
    for index, addr in enumerate(addrs):
        device = LoRaWanDevice(
            session=DeviceSession(
                dev_addr=addr, nwk_s_key=NWK_S_KEY, app_s_key=APP_S_KEY,
            ),
            data_rate=DATA_RATE,
        )
        device.radio.logger.setLevel(logging.WARNING)
        stack = FuotaDeviceStack(
            device,
            gen_app_key=GEN_APP_KEYS[addr],
            uplink_interval=uplink_interval,
            first_uplink=first_uplink + index * stagger,
            stop_after=stop_after,
            class_b_from=class_b_from,
            class_b_until=class_b_until,
            rng=random.Random(1000 + index),
            fragmentation_factory=(
                _lossy_factory(*lossy[index]) if index in lossy else None
            ),
        )
        stack.start()
        stacks.append(stack)

    campaign.start()
    return ns, gateway, campaign, stacks


def _frame_airtime(raw_len: int) -> float:
    dr = EU868_DATA_RATES[DATA_RATE]
    return estimate_airtime(
        payload_len=raw_len,
        bandwidth=dr.bandwidth.to_khz(),
        spreading_factor=dr.spreading_factor.value,
        code_rate=5,
    )


def _class_c_config(**overrides: object) -> FuotaCampaignConfig:
    defaults: dict[str, object] = dict(
        mc_addr=MC_ADDR,
        mc_key=MC_KEY,
        frag_size=FRAG_SIZE,
        data_rate=DATA_RATE,
        dl_frequency=DL_FREQUENCY,
        redundancy_ratio=None,
        redundancy_fragments=6,
        descriptor=0x00010002,
        fragment_interval=0.4,
        session_lead_time=22.0,
        setup_timeout=40.0,
        status_timeout=45.0,
        cleanup_timeout=40.0,
        poll_interval=0.5,
        broadcast_settle=1.0,
        max_repair_rounds=0,
    )
    defaults.update(overrides)
    return FuotaCampaignConfig(**defaults)  # type: ignore[arg-type]


# ═══════════════════════════════════════════════════════════════════════════
# (a) A clean Class C campaign
# ═══════════════════════════════════════════════════════════════════════════

SIM_LENGTH_CLEAN = 170


class TestCleanCampaign:
    def test_three_devices_receive_a_two_kilobyte_image(self):
        ns, gateway, campaign, stacks = _build_network(
            config=_class_c_config(),
            firmware=FIRMWARE_2KB,
            device_count=3,
            stop_after=SIM_LENGTH_CLEAN - 6,
        )
        sim.run(simulation_length=SIM_LENGTH_CLEAN)

        assert campaign.state is FuotaCampaignState.DONE, campaign.failure_reason
        assert campaign.completed_devices == set(DEV_ADDRS[:3])
        assert campaign.failed_devices == set()
        assert campaign.repair_rounds == 0

        for stack in stacks:
            assert stack.received_images[0] == FIRMWARE_2KB
            assert stack.completion_time is not None
            assert stack.is_complete(0)

    def test_the_result_describes_the_campaign(self):
        ns, gateway, campaign, stacks = _build_network(
            config=_class_c_config(),
            firmware=FIRMWARE_2KB,
            device_count=3,
            stop_after=SIM_LENGTH_CLEAN - 6,
        )
        sim.run(simulation_length=SIM_LENGTH_CLEAN)

        result = campaign.result
        assert result.success is True
        assert result.devices == 3 and result.participants == 3 and result.completed == 3
        assert result.nb_frag == 43  # ceil(2048 / 48)
        assert result.frag_size == FRAG_SIZE
        assert result.fragments_uncoded == 43
        assert result.fragments_coded == 6
        assert result.fragments_repair == 0
        # Every scheduled fragment really went on the air through the gateway.
        assert result.multicast_frames == 49
        assert gateway.multicast_frames_sent == 49
        assert result.multicast_airtime > 0
        assert result.duty_cycle_quiet_time == 0.0  # no duty cycle on this gateway
        assert result.unicast_downlinks >= 3 * 4
        assert result.uplinks_received >= 3 * 4
        assert set(result.completion_time) == set(DEV_ADDRS[:3])
        assert result.last_completion_time is not None
        assert result.total_time > 0
        assert result.phase_durations[FuotaCampaignState.BROADCAST] > 0

    def test_per_device_metrics(self):
        ns, gateway, campaign, stacks = _build_network(
            config=_class_c_config(),
            firmware=FIRMWARE_2KB,
            device_count=3,
            stop_after=SIM_LENGTH_CLEAN - 6,
        )
        sim.run(simulation_length=SIM_LENGTH_CLEAN)

        for stack in stacks:
            metrics = stack.metrics()
            assert metrics.images_received == 1
            assert metrics.image_bytes == 2048
            assert metrics.completion_time is not None
            assert metrics.uplinks_sent > 0
            # The periodic application uplink is what opened the RX1 windows.
            assert metrics.uplinks_by_fport[10] > 0
            # Fragments arrive on FPort 201, the TS005 commands on 200.
            assert metrics.downlinks_by_fport[201] >= 43
            assert metrics.downlinks_by_fport[200] >= 2
            assert metrics.fragments_received >= 43
            assert metrics.multicast_groups == 0  # McGroupDeleteReq tore it down again
            # The device answered a TS003 AppTimeReq at startup.
            assert metrics.clock_offset_seconds is not None
            assert metrics.energy_joules is not None and metrics.energy_joules > 0


# ═══════════════════════════════════════════════════════════════════════════
# (b) Losses, redundancy and a repair round
# ═══════════════════════════════════════════════════════════════════════════

SIM_LENGTH_LOSSY = 420


class TestLossyCampaign:
    def test_five_devices_with_a_weak_receiver_still_all_complete(self):
        ns, gateway, campaign, stacks = _build_network(
            config=_class_c_config(
                redundancy_fragments=4,
                max_repair_rounds=2,
                repair_extra_fragments=2,
                session_lead_time=30.0,
                setup_timeout=90.0,
            ),
            firmware=FIRMWARE_2KB,
            device_count=5,
            uplink_interval=12.0,
            stagger=2.4,
            stop_after=SIM_LENGTH_LOSSY - 6,
            # Two devices at the edge of coverage: 25% and 15% fragment loss.
            lossy={3: (0.25, 7), 4: (0.15, 11)},
        )
        sim.run(simulation_length=SIM_LENGTH_LOSSY)

        assert campaign.state is FuotaCampaignState.DONE, campaign.failure_reason
        assert campaign.completed_devices == set(DEV_ADDRS[:5])
        for stack in stacks:
            assert stack.received_images[0] == FIRMWARE_2KB

        # The lossy devices really did lose fragments, so this was not a free pass.
        lossy_apps = [stacks[3].fragmentation, stacks[4].fragmentation]
        for app in lossy_apps:
            assert isinstance(app, LossyFragmentationDeviceApplication)
            assert app.fragments_lost_on_air > 0

        # And the redundancy or a repair round is what rescued them.
        result = campaign.result
        assert result.fragments_coded + result.fragments_repair > 0
        assert result.fragments_scheduled == gateway.multicast_frames_sent

    def test_a_repair_round_is_driven_by_the_worst_off_device(self):
        ns, gateway, campaign, stacks = _build_network(
            config=_class_c_config(
                redundancy_fragments=0,
                max_repair_rounds=2,
                repair_extra_fragments=2,
                session_lead_time=22.0,
            ),
            firmware=FIRMWARE_2KB,
            device_count=3,
            stop_after=SIM_LENGTH_LOSSY - 6,
            lossy={2: (0.2, 3)},
        )
        sim.run(simulation_length=SIM_LENGTH_LOSSY)

        assert campaign.repair_rounds >= 1
        assert FuotaCampaignState.REPAIR in [
            state for (_time, state) in campaign.state_history
        ]
        assert campaign.result.fragments_repair > 0
        # The repair fragments continue the coded sequence rather than repeating it.
        session = campaign.fragmentation.sessions[0]
        assert session.highest_n_sent == campaign.result.fragments_scheduled
        assert campaign.state is FuotaCampaignState.DONE, campaign.failure_reason


# ═══════════════════════════════════════════════════════════════════════════
# (c) A duty-cycle-limited gateway
# ═══════════════════════════════════════════════════════════════════════════

SIM_LENGTH_DUTY = 220
#: 960 octets: 20 fragments of 48, enough to measure the spacing without a long run.
FIRMWARE_SMALL = FIRMWARE_2KB[:960]


class TestDutyCycle:
    def test_fragments_respect_the_duty_cycle_and_the_campaign_completes(self):
        duty_cycle = 0.10
        ns, gateway, campaign, stacks = _build_network(
            config=_class_c_config(
                redundancy_fragments=3,
                # None: the campaign derives the spacing from the gateway's duty cycle.
                fragment_interval=None,
                session_lead_time=22.0,
            ),
            firmware=FIRMWARE_SMALL,
            device_count=3,
            duty_cycle=duty_cycle,
            stop_after=SIM_LENGTH_DUTY - 6,
        )
        assert campaign.fragment_interval() == pytest.approx(
            campaign.fragment_frame_airtime() / duty_cycle + 0.1
        )

        sim.run(simulation_length=SIM_LENGTH_DUTY)

        assert campaign.state is FuotaCampaignState.DONE, campaign.failure_reason
        assert campaign.completed_devices == set(DEV_ADDRS[:3])

        fragments = [
            entry for entry in gateway.multicast_log if entry[1] == MC_ADDR
        ]
        assert len(fragments) == 23  # NbFrag 20 + 3 redundancy
        lengths = {entry[3] for entry in fragments}
        assert len(lengths) == 1, "every fragment frame is the same size"

        required = _frame_airtime(next(iter(lengths))) / duty_cycle
        times = [entry[0] for entry in fragments]
        gaps = [later - earlier for earlier, later in zip(times, times[1:])]
        assert all(gap >= required * 0.99 for gap in gaps), (gaps, required)

        assert campaign.result.duty_cycle_quiet_time > 0


# ═══════════════════════════════════════════════════════════════════════════
# (d) The Class B variant (TS005 §4.6)
# ═══════════════════════════════════════════════════════════════════════════

#: 400 octets: 8 fragments of 50, which fit in one beacon period of ping slots.
FIRMWARE_TINY = FIRMWARE_2KB[:400]
CLASS_B_SESSION_TIME = 2 * BEACON_INTERVAL
CLASS_B_SESSION_END = CLASS_B_SESSION_TIME + BEACON_INTERVAL
SIM_LENGTH_CLASS_B = 520


class TestClassBCampaign:
    def test_a_class_b_campaign_delivers_through_ping_slots(self):
        """The whole flow over Class B: unicast setup, ping-slot fragments, unicast status.

        The devices acquire Class B only once the unicast setup is behind them and drop back
        to Class A when the session window closes. A Class B device spends its time waiting
        for beacons and ping slots, which makes the RX1 window after an uplink an unreliable
        place to deliver a command — the same reason a real deployment does the TS005/TS004
        handshake in Class A and only switches for the session itself.
        """
        config = _class_c_config(
            class_b=True,
            class_b_periodicity=2,  # 128 >> 2 = 32 ping slots per beacon period
            frag_size=50,
            redundancy_fragments=4,
            session_timeout=0,  # 128 * 2**0 = one beacon period
            session_lead_time=215.0,  # rounds up to the beacon at t=256
            broadcast_offset=0.0,  # the frames are due exactly at SessionTime
            fragment_interval=0.0,
            setup_timeout=90.0,
            status_timeout=90.0,
            cleanup_timeout=90.0,
            max_repair_rounds=0,
        )
        ns, gateway, campaign, stacks = _build_network(
            config=config,
            firmware=FIRMWARE_TINY,
            device_count=2,
            class_b_enabled=True,
            class_b_from=60.0,
            class_b_until=CLASS_B_SESSION_END + 6.0,
            uplink_interval=16.0,
            stagger=5.0,
            stop_after=SIM_LENGTH_CLASS_B - 10,
        )
        sim.run(simulation_length=SIM_LENGTH_CLASS_B)

        assert campaign.state is FuotaCampaignState.DONE, campaign.failure_reason
        assert campaign.completed_devices == set(DEV_ADDRS[:2])

        request = campaign.multicast_setup._last_session_request(0)
        assert request is not None
        # TS005 §4.6: SessionTime is a multiple of one beacon period and TimeOut counts
        # beacon periods, not seconds.
        assert request.session_time == CLASS_B_SESSION_TIME
        assert request.session_time % BEACON_INTERVAL == 0
        assert request.timeout_seconds == BEACON_INTERVAL

        for stack in stacks:
            assert stack.received_images[0] == FIRMWARE_TINY
            record = stack.multicast_setup.session_history[-1]
            assert record.started is True
            assert record.ping_periodicity == 2

        # NbFrag 8 + 4 redundancy were scheduled, and every one of them was taken out of
        # the queue by the Class B ping slot scheduler.
        assert campaign.result.fragments_scheduled == 12
        assert ns.pending_multicast_downlinks() == []
        # A Class B multicast frame goes out through the gateway's ping slot path, which
        # does not pass through the free-running multicast scheduler, so it is absent from
        # ``multicast_log`` by design. What the devices saw is the real evidence.
        assert gateway.multicast_frames_sent == 0
        for stack in stacks:
            assert stack.fragmentation.fragments_received >= 8
            slots = [
                time
                for (time, fport, payload) in stack.downlink_log
                if fport == 201
                and any(
                    isinstance(command, DataFragment)
                    for command in parse_downlink_commands(payload)
                )
            ]
            assert slots, "no fragment arrived during the session"
            assert all(
                CLASS_B_SESSION_TIME <= time <= CLASS_B_SESSION_END for time in slots
            ), slots


# ═══════════════════════════════════════════════════════════════════════════
# (e) A device the network never hears from
# ═══════════════════════════════════════════════════════════════════════════

class TestUnreachableDevice:
    def test_a_silent_device_is_excluded_and_the_rest_complete(self):
        ns, gateway, campaign, stacks = _build_network(
            config=_class_c_config(setup_timeout=35.0),
            firmware=FIRMWARE_SMALL,
            device_count=3,
            # The third device is never registered: the network server drops its uplinks
            # and it therefore never answers a single command.
            unregistered={DEV_ADDRS[2]},
            stop_after=SIM_LENGTH_CLEAN - 6,
        )
        sim.run(simulation_length=SIM_LENGTH_CLEAN)

        assert campaign.state is FuotaCampaignState.DONE, campaign.failure_reason
        assert campaign.participants == {DEV_ADDRS[0], DEV_ADDRS[1]}
        assert campaign.excluded == {DEV_ADDRS[2]: FuotaCampaignState.GROUP_SETUP}
        assert campaign.completed_devices == {DEV_ADDRS[0], DEV_ADDRS[1]}
        assert campaign.failed_devices == {DEV_ADDRS[2]}

        assert stacks[0].received_images[0] == FIRMWARE_SMALL
        assert stacks[1].received_images[0] == FIRMWARE_SMALL
        # The excluded device never even got a fragmentation session.
        assert stacks[2].received_images == {}
        assert stacks[2].fragmentation.sessions == {}

        result = campaign.result
        assert result.devices == 3
        assert result.participants == 2
        assert result.completed == 2
        assert result.failed == 1
        assert result.excluded == {DEV_ADDRS[2]}
