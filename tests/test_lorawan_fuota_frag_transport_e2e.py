"""End-to-end simulation runs of the TS004-2.0.0 fragmentation package.

Unlike ``tests/test_lorawan_fuota_frag_transport.py``, which drives the codecs and the
application classes directly, every test here runs a real simulation: a
:class:`~simulator.lorawan.network_server.NetworkServer`, a
:class:`~simulator.lorawan.gateway.LoRaWanGateway` with its multicast scheduler, and three
three :class:`~simulator.lorawan.device.LoRaWanDevice` instances that are already members of
a multicast group. Frames really go over the air through the PHY layer, so a fragment that
collides or arrives while a device is transmitting is genuinely lost.

The devices follow the TS005 session lifecycle the fragment broadcast assumes: Class C
while a multicast session window is open (listening only), Class A the rest of the time,
where their uplinks carry the package's answers and open the RX1 windows the network server
delivers unicast commands in.

The multicast group is constructed directly rather than set up over TS005: this module is
about the fragment transport, not about ``McGroupSetupReq``.

Covered:

- a clean broadcast where all three devices reconstruct the block and the per-device
  ``DataBlockIntKey`` MIC verifies (§3.3, §3.6);
- a device that loses every fifth fragment, rescued by the redundancy fragments (§A.1);
- a status round (§3.2) followed by a repair round of extra coded fragments;
- the gateway's duty cycle spacing fragments at least ``airtime / duty_cycle`` apart.
"""

from __future__ import annotations

from simulator.environment import simulation_env as sim
from simulator.exceptions import SimulatorException
from simulator.lora.airtime import estimate_airtime
from simulator.lorawan.device import DeviceSession, LoRaWanDevice, MulticastGroup
from simulator.lorawan.enums.operating_mode import OperatingMode
from simulator.lorawan.fuota.crypto import derive_data_block_int_key
from simulator.lorawan.fuota.frag_transport import (
    FRAGMENTATION_FPORT,
    DataFragment,
    FragmentationDeviceApplication,
    FragmentationServerApplication,
)
from simulator.lorawan.gateway import LoRaWanGateway
from simulator.lorawan.network_server import NetworkServer
from simulator.lorawan.region import EU868_DATA_RATES

# ── Test credentials ────────────────────────────────────────────────────────
DEV_ADDRS = [0x26010001, 0x26010002, 0x26010003]
NWK_S_KEY = bytes.fromhex("2B7E151628AED2A6ABF7158809CF4F3C")
APP_S_KEY = bytes.fromhex("3C4F9C098815F7ABA6D2AE281615E72B")

#: One GenAppKey per device, so each gets a different DataBlockIntKey and therefore a
#: different FragSessionSetupReq MIC — exactly the point of the per-device MIC in §3.3.
GEN_APP_KEYS = {
    addr: bytes([index + 1]) * 16 for index, addr in enumerate(DEV_ADDRS)
}
DATA_BLOCK_INT_KEYS = {
    addr: derive_data_block_int_key(key=key) for addr, key in GEN_APP_KEYS.items()
}

MC_ADDR = 0xFF000001
MC_NWK_KEY = bytes.fromhex("AABBCCDD11223344AABBCCDD11223344")
MC_APP_KEY = bytes.fromhex("11223344AABBCCDD11223344AABBCCDD")

DATA_RATE = 5
FRAG_SIZE = 40
#: 800 octets split into 20 fragments of 40. §A.3 warns the FEC is inefficient below ~20
#: fragments, so this is the smallest block that is still representative.
DATA_BLOCK = bytes((i * 37 + 11) & 0xFF for i in range(800))


# ═══════════════════════════════════════════════════════════════════════════
# Fixtures and helpers
# ═══════════════════════════════════════════════════════════════════════════

class LossyFragmentationDeviceApplication(FragmentationDeviceApplication):
    """Device application that throws away every *drop_every*-th ``DataFragment``.

    Models a receiver that is simply worse off than the rest of the fleet — a weak link,
    an interferer, or a device busy doing something else — without having to arrange the
    radio geometry for it.
    """

    def __init__(self, *args, drop_every: int = 5, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.drop_every = drop_every
        self.fragments_lost_on_air = 0
        self._fragments_seen = 0

    async def _handle_data_fragment(self, fragment: DataFragment) -> None:
        self._fragments_seen += 1
        if self._fragments_seen % self.drop_every == 0:
            self.fragments_lost_on_air += 1
            return
        await super()._handle_data_fragment(fragment)


def _build_fleet(
    lossy_index: int | None = None, drop_every: int = 5
) -> tuple[NetworkServer, LoRaWanGateway, list[LoRaWanDevice], list[FragmentationDeviceApplication], FragmentationServerApplication]:
    """A network server, a gateway and three Class C devices in one multicast group.

    The device applications are created without an attached device, so this module's own
    polling loop is the only thing that transmits uplinks. That keeps the three devices
    from colliding with each other in ways that have nothing to do with TS004.
    """
    ns = NetworkServer()
    for addr in DEV_ADDRS:
        ns.register_device(addr, NWK_S_KEY, APP_S_KEY)
    ns.create_multicast_group(
        MC_ADDR, MC_NWK_KEY, MC_APP_KEY, group_id=0, data_rate=DATA_RATE
    )

    server = FragmentationServerApplication(ns, key_provider=DATA_BLOCK_INT_KEYS)
    ns.register_application(server)

    gateway = LoRaWanGateway(network_server=ns, data_rate=DATA_RATE)

    devices: list[LoRaWanDevice] = []
    apps: list[FragmentationDeviceApplication] = []
    for index, addr in enumerate(DEV_ADDRS):
        device = LoRaWanDevice(
            session=DeviceSession(
                dev_addr=addr, nwk_s_key=NWK_S_KEY, app_s_key=APP_S_KEY
            ),
            data_rate=DATA_RATE,
        )
        if index == lossy_index:
            app: FragmentationDeviceApplication = LossyFragmentationDeviceApplication(
                gen_app_key=GEN_APP_KEYS[addr], drop_every=drop_every
            )
        else:
            app = FragmentationDeviceApplication(gen_app_key=GEN_APP_KEYS[addr])
        device.register_application(app)
        # McGroupID 0, which a FragSessionSetupReq enables with McGroupBitMask bit 0.
        device.join_multicast_group(
            MulticastGroup(MC_ADDR, MC_NWK_KEY, MC_APP_KEY, group_id=0)
        )
        devices.append(device)
        apps.append(app)

    return ns, gateway, devices, apps, server


async def _device_loop(
    device: LoRaWanDevice,
    app: FragmentationDeviceApplication,
    *,
    start: float,
    period: float,
    class_c_windows: list[tuple[float, float]],
    stop_after: float,
) -> None:
    """Run one device: Class C while a multicast session is open, Class A otherwise.

    Outside a session window the device transmits on a fixed slot grid (``start`` plus
    multiples of ``period``). The uplink both carries whatever answer the package has
    queued and opens the RX1 window the network server delivers the next unicast command
    in. Inside a window the device only listens, which is what a device in a TS005 Class C
    multicast session does.

    The slot grid is absolute rather than "sleep *period* after the last uplink", because
    ``send_uplink`` blocks for the whole RX1/RX2 pair: a relative delay lets the three
    devices drift into each other and collide at the gateway.

    Mode switching and transmitting happen in this one task, so the device never tries to
    receive continuously and transmit at the same time. After leaving Class C it waits out
    the continuous-RX loop's one-second poll and skips a slot before transmitting again.

    The loop stops a few seconds before the simulation ends (*stop_after*): an uplink needs
    its RX1 and RX2 windows afterwards, and a radio that is still switching state when the
    environment tears down would fail on bookkeeping that no longer exists.
    """
    try:
        slot = start
        while sim.is_running():
            now = sim.current_time()
            while slot <= now:
                slot += period
            if slot > stop_after:
                return
            await sim.sleep_until(slot)

            in_session = any(
                begin <= sim.current_time() < end for begin, end in class_c_windows
            )
            wanted = OperatingMode.CLASS_C if in_session else OperatingMode.CLASS_A
            if device.operating_mode != wanted:
                await device.switch_mode(wanted)
                if wanted == OperatingMode.CLASS_A:
                    # Let the Class C receive loop notice the stop flag and let go of the
                    # radio; transmitting resumes on the next slot.
                    await sim.sleep(1.2)
                    # Leaving Class C does not power the receiver down, so the radio would
                    # keep queueing frames meant for other devices and the next RX1 window
                    # would hand one of those to _process_downlink and close before this
                    # device's own reply ever lands. Empty the queue and switch off, which
                    # is what a Class A device does between windows anyway.
                    while await device.radio.receive_data_nowait() is not None:
                        pass
                    await device.radio.off()
                    continue
            if not in_session:
                payload = app.pop_pending_uplink()
                await device.send_uplink(FRAGMENTATION_FPORT, payload or b"")
    except SimulatorException:
        return


def _start_fleet(
    devices: list[LoRaWanDevice],
    apps: list[FragmentationDeviceApplication],
    class_c_windows: list[tuple[float, float]],
    *,
    simulation_length: float,
    period: float = 5.0,
    stagger: float = 1.5,
) -> None:
    """Start one :func:`_device_loop` per device, staggered so uplinks never overlap."""
    for index, (device, app) in enumerate(zip(devices, apps)):
        sim.create_task(
            _device_loop(
                device,
                app,
                start=1.0 + index * stagger,
                period=period,
                class_c_windows=class_c_windows,
                stop_after=simulation_length - 5.0,
            )
        )


def _multicast_airtime(raw_len: int) -> float:
    dr = EU868_DATA_RATES[DATA_RATE]
    return estimate_airtime(
        payload_len=raw_len,
        bandwidth=dr.bandwidth.to_khz(),
        spreading_factor=dr.spreading_factor.value,
        code_rate=5,
    )


# ═══════════════════════════════════════════════════════════════════════════
# A clean multicast transfer
# ═══════════════════════════════════════════════════════════════════════════

class TestMulticastTransfer:
    def test_three_devices_reconstruct_the_block(self):
        ns, gateway, devices, apps, server = _build_fleet()
        # The multicast session runs from t=18; the fragments go out from t=26.
        _start_fleet(devices, apps, [(18.0, 45.0)], simulation_length=45)

        async def server_driver() -> None:
            try:
                await sim.sleep(1.0)
                server.create_session(
                    DEV_ADDRS,
                    frag_index=0,
                    data=DATA_BLOCK,
                    frag_size=FRAG_SIZE,
                    session_cnt=1,
                    mc_group_bit_mask=0b0001,
                    redundancy_fragments=3,
                    descriptor=0x00010002,
                )
                server.broadcast_fragments(
                    0, group_addr=MC_ADDR, start_time=26.0, interval=0.5,
                )
            except SimulatorException:
                return

        sim.create_task(server_driver())
        sim.run(simulation_length=45)

        assert server.devices_acked_setup(0) == set(DEV_ADDRS)
        assert gateway.multicast_frames_sent == 23  # NbFrag 20 + 3 redundancy

        for addr, app in zip(DEV_ADDRS, apps):
            assert app.is_complete(0), f"0x{addr:08X} did not finish"
            assert app.completed_blocks[0] == DATA_BLOCK
            assert app.sessions[0].mic_ok is True, f"0x{addr:08X} MIC mismatch"
            assert app.sessions[0].nb_frag == 20
            assert app.fragments_dropped <= app.sessions[0].frames_received

    def test_the_block_is_padded_and_stripped_again(self):
        """A block that is not a multiple of FragSize round-trips through Padding (§3.3)."""
        block = DATA_BLOCK[:785]  # 20 fragments of 40 with 15 octets of padding
        ns, gateway, devices, apps, server = _build_fleet()
        _start_fleet(devices[:1], apps[:1], [(13.0, 40.0)], simulation_length=40)

        async def server_driver() -> None:
            try:
                await sim.sleep(1.0)
                session = server.create_session(
                    DEV_ADDRS[:1],
                    frag_index=1,
                    data=block,
                    frag_size=FRAG_SIZE,
                    session_cnt=1,
                    mc_group_bit_mask=0b0001,
                    redundancy_fragments=3,
                )
                assert session.padding == 15
                server.broadcast_fragments(
                    1, group_addr=MC_ADDR, start_time=18.0, interval=0.5,
                )
            except SimulatorException:
                return

        sim.create_task(server_driver())
        sim.run(simulation_length=40)

        assert apps[0].completed_blocks[1] == block
        assert len(apps[0].completed_blocks[1]) == 785
        assert apps[0].sessions[1].mic_ok is True


# ═══════════════════════════════════════════════════════════════════════════
# Redundancy against losses (§A.1)
# ═══════════════════════════════════════════════════════════════════════════

class TestRedundancy:
    def test_redundancy_rescues_a_device_losing_every_fifth_fragment(self):
        ns, gateway, devices, apps, server = _build_fleet(lossy_index=2, drop_every=5)
        lossy = apps[2]
        assert isinstance(lossy, LossyFragmentationDeviceApplication)
        _start_fleet(devices, apps, [(18.0, 50.0)], simulation_length=50)

        async def server_driver() -> None:
            try:
                await sim.sleep(1.0)
                server.create_session(
                    DEV_ADDRS,
                    frag_index=0,
                    data=DATA_BLOCK,
                    frag_size=FRAG_SIZE,
                    session_cnt=1,
                    mc_group_bit_mask=0b0001,
                    redundancy_fragments=6,
                )
                server.broadcast_fragments(
                    0, group_addr=MC_ADDR, start_time=26.0, interval=0.5,
                )
            except SimulatorException:
                return

        sim.create_task(server_driver())
        sim.run(simulation_length=50)

        assert gateway.multicast_frames_sent == 26  # 20 + 6 redundancy
        assert lossy.fragments_lost_on_air >= 4

        for addr, app in zip(DEV_ADDRS, apps):
            assert app.is_complete(0), f"0x{addr:08X} did not finish"
            assert app.completed_blocks[0] == DATA_BLOCK
            assert app.sessions[0].mic_ok is True

        # The lossy device needed the coded fragments: it never saw all 20 uncoded ones.
        assert len(lossy.sessions[0].decoder.missing_uncoded()) > 0

    def test_without_redundancy_the_lossy_device_falls_short(self):
        """The control case: exactly NbFrag fragments leave one device incomplete."""
        ns, gateway, devices, apps, server = _build_fleet(lossy_index=2, drop_every=5)
        _start_fleet(devices, apps, [(18.0, 45.0)], simulation_length=45)

        async def server_driver() -> None:
            try:
                await sim.sleep(1.0)
                server.create_session(
                    DEV_ADDRS,
                    frag_index=0,
                    data=DATA_BLOCK,
                    frag_size=FRAG_SIZE,
                    session_cnt=1,
                    mc_group_bit_mask=0b0001,
                )
                server.broadcast_fragments(
                    0, group_addr=MC_ADDR, start_time=26.0, interval=0.5,
                )
            except SimulatorException:
                return

        sim.create_task(server_driver())
        sim.run(simulation_length=45)

        assert gateway.multicast_frames_sent == 20
        assert apps[0].is_complete(0)
        assert apps[1].is_complete(0)
        assert not apps[2].is_complete(0)
        assert apps[2].progress(0) == (16, 20)


# ═══════════════════════════════════════════════════════════════════════════
# Status round and repair round (§3.2, §3.6)
# ═══════════════════════════════════════════════════════════════════════════

class TestStatusAndRepair:
    def test_status_round_drives_a_repair_round(self):
        ns, gateway, devices, apps, server = _build_fleet(lossy_index=2, drop_every=5)
        # Two multicast sessions: the initial broadcast, then the repair round.
        _start_fleet(devices, apps, [(18.0, 38.0), (58.0, 78.0)], simulation_length=80)
        lossy_addr = DEV_ADDRS[2]

        async def server_driver() -> None:
            try:
                await sim.sleep(1.0)
                session = server.create_session(
                    DEV_ADDRS,
                    frag_index=0,
                    data=DATA_BLOCK,
                    frag_size=FRAG_SIZE,
                    session_cnt=1,
                    mc_group_bit_mask=0b0001,
                )
                # Exactly NbFrag fragments: no redundancy, so the lossy device falls short.
                server.broadcast_fragments(
                    0, group_addr=MC_ADDR, start_time=26.0, interval=0.5,
                )

                # The session ends at t=38; back in Class A, poll the fleet over unicast.
                await sim.sleep(39.0)
                server.request_status(DEV_ADDRS, 0, participants=False)

                # The answers ride the devices' uplinks over the next few seconds.
                await sim.sleep(15.0)
                missing = server.max_missing(0)
                assert missing > 0
                server.broadcast_fragments(
                    0,
                    group_addr=MC_ADDR,
                    start_time=62.0,
                    interval=0.5,
                    count=missing + 2,
                    start_n=session.highest_n_sent + 1,
                )
            except SimulatorException:
                return

        sim.create_task(server_driver())
        sim.run(simulation_length=80)

        # §3.2 Participants = 0: only the device still missing fragments answered.
        answering = {report.dev_addr for report in server.status_reports}
        assert answering == {lossy_addr}
        report = server.latest_status[(0, lossy_addr)]
        assert report.missing_frag == 4
        assert report.nb_frag_received == 16
        assert report.memory_error is False
        assert report.session_does_not_exist is False

        for addr, app in zip(DEV_ADDRS, apps):
            assert app.is_complete(0), f"0x{addr:08X} did not finish"
            assert app.completed_blocks[0] == DATA_BLOCK
            assert app.sessions[0].mic_ok is True

    def test_completion_acknowledgement_round_trip(self):
        """AckReception = 1: the device uplinks CID 0x04 and the server answers it (§3.5)."""
        ns, gateway, devices, apps, server = _build_fleet()
        _start_fleet(devices, apps, [(18.0, 45.0)], simulation_length=65)

        async def server_driver() -> None:
            try:
                await sim.sleep(1.0)
                server.create_session(
                    DEV_ADDRS,
                    frag_index=0,
                    data=DATA_BLOCK,
                    frag_size=FRAG_SIZE,
                    session_cnt=1,
                    mc_group_bit_mask=0b0001,
                    redundancy_fragments=3,
                    ack_reception=True,
                    block_ack_delay=0,
                )
                server.broadcast_fragments(
                    0, group_addr=MC_ADDR, start_time=26.0, interval=0.5,
                )
            except SimulatorException:
                return

        sim.create_task(server_driver())
        sim.run(simulation_length=65)

        assert server.devices_complete(0) == set(DEV_ADDRS)
        assert server.sessions[0].mic_errors == set()
        for app in apps:
            assert app.is_complete(0)
            # The FragDataBlockReceivedAns came back, so nothing is outstanding.
            assert not app.sessions[0].ack_pending
            assert app.sessions[0].ack_attempts >= 1


# ═══════════════════════════════════════════════════════════════════════════
# Duty cycle (ETSI EN 300 220-2 / RP002-1.0.4 §2.4.3)
# ═══════════════════════════════════════════════════════════════════════════

class TestDutyCycle:
    def test_fragments_are_spaced_by_at_least_airtime_over_duty_cycle(self):
        ns = NetworkServer()
        ns.create_multicast_group(MC_ADDR, MC_NWK_KEY, MC_APP_KEY, data_rate=DATA_RATE)
        for addr in DEV_ADDRS:
            ns.register_device(addr, NWK_S_KEY, APP_S_KEY)
        server = FragmentationServerApplication(ns, key_provider=DATA_BLOCK_INT_KEYS)
        ns.register_application(server)

        duty_cycle = 0.10
        gateway = LoRaWanGateway(
            network_server=ns, data_rate=DATA_RATE, duty_cycle=duty_cycle
        )

        server.create_session(
            DEV_ADDRS,
            frag_index=0,
            data=DATA_BLOCK[:240],  # 6 fragments, enough to measure the spacing
            frag_size=FRAG_SIZE,
            session_cnt=1,
            mc_group_bit_mask=0b0001,
        )
        # Ask for a far tighter spacing than the duty cycle permits: the gateway's
        # limiter, not the schedule, must decide when the frames actually go out.
        server.broadcast_fragments(
            0, group_addr=MC_ADDR, start_time=1.0, interval=0.05
        )

        sim.run(simulation_length=40)

        assert gateway.multicast_frames_sent == 6
        times = [entry[0] for entry in gateway.multicast_log]
        lengths = [entry[3] for entry in gateway.multicast_log]
        assert len(set(lengths)) == 1, "every fragment frame is the same size"

        required = _multicast_airtime(lengths[0]) / duty_cycle
        gaps = [later - earlier for earlier, later in zip(times, times[1:])]
        assert all(gap >= required * 0.99 for gap in gaps), (gaps, required)
        # And the limiter is the binding constraint, not some unrelated delay.
        assert all(gap < required * 1.5 for gap in gaps), (gaps, required)

    def test_no_duty_cycle_follows_the_requested_schedule(self):
        ns = NetworkServer()
        ns.create_multicast_group(MC_ADDR, MC_NWK_KEY, MC_APP_KEY, data_rate=DATA_RATE)
        for addr in DEV_ADDRS:
            ns.register_device(addr, NWK_S_KEY, APP_S_KEY)
        server = FragmentationServerApplication(ns, key_provider=DATA_BLOCK_INT_KEYS)
        ns.register_application(server)
        gateway = LoRaWanGateway(network_server=ns, data_rate=DATA_RATE)

        server.create_session(
            DEV_ADDRS, frag_index=0, data=DATA_BLOCK[:240], frag_size=FRAG_SIZE,
            session_cnt=1, mc_group_bit_mask=0b0001,
        )
        server.broadcast_fragments(
            0, group_addr=MC_ADDR, start_time=1.0, interval=0.5
        )

        sim.run(simulation_length=20)

        assert gateway.multicast_frames_sent == 6
        times = [entry[0] for entry in gateway.multicast_log]
        assert times[0] >= 1.0
        assert times[-1] - times[0] < 3.5


def test_attached_device_sends_its_own_answers():
    """With a device attached the package transmits its answers itself (§3.3).

    One device, one manual uplink: everything after it — the FragSessionSetupAns — is
    sent by ``FuotaDeviceApplication``'s own child task.
    """
    ns = NetworkServer()
    addr = DEV_ADDRS[0]
    ns.register_device(addr, NWK_S_KEY, APP_S_KEY)
    ns.create_multicast_group(MC_ADDR, MC_NWK_KEY, MC_APP_KEY, data_rate=DATA_RATE)
    server = FragmentationServerApplication(ns, key_provider=DATA_BLOCK_INT_KEYS)
    ns.register_application(server)
    LoRaWanGateway(network_server=ns, data_rate=DATA_RATE)

    device = LoRaWanDevice(
        session=DeviceSession(dev_addr=addr, nwk_s_key=NWK_S_KEY, app_s_key=APP_S_KEY),
        data_rate=DATA_RATE,
    )
    app = FragmentationDeviceApplication(device, gen_app_key=GEN_APP_KEYS[addr])
    device.register_application(app)
    device.join_multicast_group(
        MulticastGroup(MC_ADDR, MC_NWK_KEY, MC_APP_KEY, group_id=0)
    )

    async def driver() -> None:
        try:
            await sim.sleep(1.0)
            server.create_session(
                [addr], frag_index=0, data=DATA_BLOCK, frag_size=FRAG_SIZE,
                session_cnt=1, mc_group_bit_mask=0b0001,
            )
            await sim.sleep(1.0)
            # The single uplink that fetches the setup request in RX1.
            await device.send_uplink(FRAGMENTATION_FPORT, b"")
        except SimulatorException:
            return

    sim.create_task(driver())
    sim.run(simulation_length=20)

    assert app.sessions[0].nb_frag == 20
    assert app.uplinks_sent == 1  # the FragSessionSetupAns, sent unprompted
    assert server.devices_acked_setup(0) == {addr}
