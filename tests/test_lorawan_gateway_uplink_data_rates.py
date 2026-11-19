"""A gateway that listens for uplinks with several data rates at once.

By default the gateway demodulates one data rate and answers with it. With
``uplink_data_rates`` it gets a receive chain per data rate, like a concentrator, and an RX1
reply goes out with the data rate of the uplink it answers, which is what the device that
sent it listens for.
"""

from __future__ import annotations

import pytest

from simulator.environment import simulation_env as sim
from simulator.lorawan.application import Application
from simulator.lorawan.device import DeviceSession, LoRaWanDevice
from simulator.lorawan.gateway import LoRaWanGateway
from simulator.lorawan.network_server import NetworkServer

NWK_S_KEY = bytes.fromhex("2B7E151628AED2A6ABF7158809CF4F3C")
APP_S_KEY = bytes.fromhex("3C4F9C098815F7ABA6D2AE281615E72B")
FPORT = 10
DATA_RATES = {0x26015000: 5, 0x26015001: 3, 0x26015002: 0}


class _EchoServerApp(Application):
    """Answers every uplink with a downlink carrying the same payload."""

    def __init__(self) -> None:
        self.uplinks: dict[int, int] = {}
        self._replies: dict[int, bytes] = {}

    def port(self) -> int:
        return FPORT

    async def on_uplink(self, dev_addr: int, payload: bytes) -> None:
        self.uplinks[dev_addr] = self.uplinks.get(dev_addr, 0) + 1
        self._replies[dev_addr] = payload

    async def get_downlink(self, dev_addr: int) -> bytes | None:
        return self._replies.pop(dev_addr, None)


class _DeviceApp(Application):
    def __init__(self) -> None:
        self.downlinks: list[bytes] = []

    def port(self) -> int:
        return FPORT

    async def on_uplink(self, dev_addr: int, payload: bytes) -> None:
        return None

    async def on_downlink(self, payload: bytes) -> None:
        self.downlinks.append(payload)


def _run(uplink_data_rates: list[int] | None) -> tuple[_EchoServerApp, dict[int, _DeviceApp]]:
    ns = NetworkServer()
    server_app = _EchoServerApp()
    ns.register_application(server_app)
    LoRaWanGateway(network_server=ns, data_rate=5, uplink_data_rates=uplink_data_rates)

    apps: dict[int, _DeviceApp] = {}
    for (index, (dev_addr, data_rate)) in enumerate(DATA_RATES.items()):
        ns.register_device(dev_addr, NWK_S_KEY, APP_S_KEY)
        device = LoRaWanDevice(
            session=DeviceSession(dev_addr=dev_addr, nwk_s_key=NWK_S_KEY, app_s_key=APP_S_KEY),
            data_rate=data_rate,
        )
        device.radio.position = (10.0 * (index + 1), 0.0)
        apps[dev_addr] = _DeviceApp()
        device.register_application(apps[dev_addr])

        async def uplinks(device: LoRaWanDevice = device, start: float = 1.0 + 10.0 * index) -> None:
            await sim.sleep_until(start)
            await device.send_uplink(FPORT, b"ping")

        sim.create_task(uplinks())

    sim.run(simulation_length=40)
    return server_app, apps


def test_uplinks_and_rx1_replies_at_every_configured_data_rate() -> None:
    server_app, apps = _run(uplink_data_rates=[0, 3, 5])

    for dev_addr in DATA_RATES:
        assert server_app.uplinks.get(dev_addr) == 1
        assert apps[dev_addr].downlinks == [b"ping"]


def test_default_gateway_hears_its_own_data_rate_only() -> None:
    server_app, apps = _run(uplink_data_rates=None)

    assert server_app.uplinks == {0x26015000: 1}
    assert apps[0x26015000].downlinks == [b"ping"]
    assert apps[0x26015001].downlinks == []
    assert apps[0x26015002].downlinks == []


@pytest.mark.parametrize("extra", [[0], [0, 5]])
def test_own_data_rate_is_always_listened_to(extra: list[int]) -> None:
    server_app, _ = _run(uplink_data_rates=extra)

    assert set(server_app.uplinks) == {0x26015000, 0x26015002}
