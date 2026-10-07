"""IQ inversion separates uplinks from downlinks; beacon windows keep their deadline.

LoRaWAN transmits downlinks with inverted IQ, so an end device listening for downlinks never
even detects the preamble of another device's uplink on the same channel. Until the LoRaWAN
layer set the flag, every device on the single simulated channel received every other
device's uplinks in its windows, which both handed foreign frames to the MAC layer and kept
Class B beacon windows restarting. The beacon window itself now keeps its deadline when a
non-beacon frame lands in it instead of opening a fresh window.
"""

from __future__ import annotations

from simulator.environment import simulation_env as sim
from simulator.lora.enums.radio_state import RadioState
from simulator.lora.phy_layer import LoraPhyLayer
from simulator.lorawan.device import DeviceSession, LoRaWanDevice
from simulator.lorawan.enums.operating_mode import OperatingMode
from simulator.lorawan.gateway import LoRaWanGateway
from simulator.lorawan.network_server import NetworkServer
from simulator.lorawan.region import BEACON_INTERVAL, BEACON_RESERVED, BEACON_LATE_TOLERANCE

DEV_A = 0x26019201
DEV_B = 0x26019202
NWK_S_KEY = bytes.fromhex("2B7E151628AED2A6ABF7158809CF4F3C")
APP_S_KEY = bytes.fromhex("3C4F9C098815F7ABA6D2AE281615E72B")


def _scenario(*, class_b: bool = False) -> tuple[NetworkServer, LoRaWanGateway, LoRaWanDevice, LoRaWanDevice]:
    LoraPhyLayer()
    ns = NetworkServer()
    ns.register_device(DEV_A, NWK_S_KEY, APP_S_KEY)
    ns.register_device(DEV_B, NWK_S_KEY, APP_S_KEY)
    gateway = LoRaWanGateway(network_server=ns, class_b_enabled=class_b)
    a = LoRaWanDevice(session=DeviceSession(dev_addr=DEV_A, nwk_s_key=NWK_S_KEY, app_s_key=APP_S_KEY))
    b = LoRaWanDevice(session=DeviceSession(dev_addr=DEV_B, nwk_s_key=NWK_S_KEY, app_s_key=APP_S_KEY))
    return ns, gateway, a, b


def _rx_segments(device: LoRaWanDevice) -> list[tuple[float, float]]:
    """(start, duration) of every stretch the device's radio spent in RX."""
    log = device.radio._LoraRadio__state_log  # type: ignore[attr-defined]
    return [
        (t, t_next - t)
        for (t, state), (t_next, _) in zip(log, log[1:])
        if state == RadioState.RX
    ]


class TestIqInversion:
    def test_a_listening_device_does_not_hear_another_devices_uplink(self):
        _ns, gateway, a, b = _scenario()
        foreign: list[bytes] = []

        async def spy(raw: bytes) -> None:
            foreign.append(raw)

        b._process_downlink = spy  # type: ignore[method-assign]

        async def behaviour() -> None:
            await sim.sleep(0.5)
            await b.switch_mode(OperatingMode.CLASS_C)
            await sim.sleep(0.5)
            await a.send_uplink(1, b"from a")

        sim.create_task(behaviour())
        sim.run(simulation_length=5)

        assert gateway.frames_forwarded == 1          # the gateway did hear it
        assert foreign == []                           # the other device did not
        assert b.radio.get_state() == RadioState.RX    # and is still listening

    def test_the_device_still_hears_its_own_downlink(self):
        ns, _gateway, a, _b = _scenario()
        ns.queue_downlink(DEV_A, 1, b"reply")
        outcome: list[bool] = []

        async def behaviour() -> None:
            await sim.sleep(1.0)
            outcome.append(await a.send_uplink(1, b"ping"))

        sim.create_task(behaviour())
        sim.run(simulation_length=5)
        assert outcome == [True]


class TestBeaconWindow:
    def test_beacon_is_acquired_when_another_downlink_lands_in_the_window_first(self):
        ns, gateway, a, b = _scenario(class_b=True)
        # A's RX1 reply goes out ~4 s before the beacon, inside B's acquisition window. B
        # receives it (downlinks are what B listens for), finds it is not a beacon, and has
        # to keep listening to the same deadline instead of giving up or starting over.
        ns.queue_downlink(DEV_A, 1, bytes(range(40)))
        a_received: list[bool] = []

        async def behaviour() -> None:
            await sim.sleep(BEACON_INTERVAL - 8.0)
            await b.switch_mode(OperatingMode.CLASS_B)
            await sim.sleep_until(BEACON_INTERVAL - 5.0)
            a_received.append(await a.send_uplink(1, b"ping"))

        sim.create_task(behaviour())
        sim.run(simulation_length=BEACON_INTERVAL + 20)

        assert a_received == [True]
        assert gateway.downlinks_skipped == 0
        assert b._beacon_locked
        assert b._beacon_time == BEACON_INTERVAL

    def test_an_rx1_reply_that_would_overlap_the_beacon_slot_is_skipped(self):
        ns, gateway, a, b = _scenario(class_b=True)
        # A's reply would be on the air at the instant the beacon is due. The gateway keeps
        # the beacon guard and reserved slot clear, so the reply is dropped (A's package
        # layer would retry it on the next uplink) and the beacon goes out on time.
        ns.queue_downlink(DEV_A, 1, bytes(range(40)))

        async def behaviour() -> None:
            await sim.sleep(BEACON_INTERVAL - 2.0)
            await b.switch_mode(OperatingMode.CLASS_B)
            await sim.sleep_until(BEACON_INTERVAL - 1.0 - 0.06)
            # (A's own RX1 window catches the beacon instead, on this single channel, and
            # discards it as undecodable; what matters here is what the gateway did.)
            await a.send_uplink(1, b"ping")

        sim.create_task(behaviour())
        sim.run(simulation_length=BEACON_INTERVAL + 20)

        assert gateway.downlinks_skipped == 1
        assert b._beacon_locked
        assert b._beacon_time == BEACON_INTERVAL

    def test_beacon_search_survives_the_devices_own_uplinks(self):
        _ns, _gateway, _a, b = _scenario(class_b=True)
        # Every uplink puts the radio into TX and the Class A windows after it power the
        # receiver down. The beacon search has to put it back on, or the device sits deaf
        # for the rest of its (up to 130 s) search window and never locks.

        async def behaviour() -> None:
            await sim.sleep(BEACON_INTERVAL - 60.0)
            await b.switch_mode(OperatingMode.CLASS_B)
            for offset in (50.0, 34.0, 18.0, 6.0):  # the last one just outside BEACON_GUARD
                await sim.sleep_until(BEACON_INTERVAL - offset)
                await b.send_uplink(1, b"ping")

        sim.create_task(behaviour())
        sim.run(simulation_length=BEACON_INTERVAL + 20)

        assert b._beacon_locked
        assert b._beacon_time == BEACON_INTERVAL

    def test_tracking_a_beacon_costs_the_beacon_not_a_second_of_listening(self):
        _ns, _gateway, _a, b = _scenario(class_b=True)
        first_beacon = BEACON_INTERVAL

        async def behaviour() -> None:
            await sim.sleep(first_beacon - 1.0)
            await b.switch_mode(OperatingMode.CLASS_B)

        sim.create_task(behaviour())
        sim.run(simulation_length=3 * BEACON_INTERVAL + 10)

        assert b._beacon_locked
        assert b._beacon_time == 3 * BEACON_INTERVAL
        after_lock = [seg for seg in _rx_segments(b) if seg[0] > first_beacon + 1.0]
        assert after_lock, "expected ping slots and two more beacon windows"
        # A tracked beacon window closes as soon as the beacon has been received (~60 ms at
        # DR5); ping slots are 30 ms. The old scheduler woke a full second early and sat in
        # RX until the beacon arrived.
        assert max(d for _, d in after_lock) < 0.5
        # And an empty tracked window would not stay open longer than the reserved slot.
        assert BEACON_RESERVED + BEACON_LATE_TOLERANCE < 3.2
