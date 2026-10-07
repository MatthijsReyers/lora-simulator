"""Receive windows close after a few symbols when nothing arrives.

A Class A or Class B device spends nearly all of its receive time in *empty* RX1/RX2 windows,
so their length sets the device's energy cost almost by itself. The window used to be held
open for a fixed half second at every data rate; hardware closes it as soon as the
demodulator has seen enough symbols to know no preamble is coming (6 ms at SF7, 200 ms at
SF12). A downlink that starts inside the window is still received in full.
"""

from __future__ import annotations

import pytest

from simulator.environment import simulation_env as sim
from simulator.lora.enums.bandwidth import Bandwidth
from simulator.lora.enums.radio_state import RadioState
from simulator.lora.enums.spreading_factor import SpreadingFactor
from simulator.lora.phy_layer import LoraPhyLayer
from simulator.lorawan.device import DeviceSession, LoRaWanDevice
from simulator.lorawan.enums.operating_mode import OperatingMode
from simulator.lorawan.gateway import LoRaWanGateway
from simulator.lorawan.network_server import NetworkServer
from simulator.lorawan.region import (
    EU868_DATA_RATES, RX_WINDOW_GUARD, RX_WINDOW_MIN_SYMBOLS,
    rx_window_duration, rx_window_timeout_symbols,
)

DEV_ADDR = 0x26019101
NWK_S_KEY = bytes.fromhex("2B7E151628AED2A6ABF7158809CF4F3C")
APP_S_KEY = bytes.fromhex("3C4F9C098815F7ABA6D2AE281615E72B")


class TestRxWindowDuration:
    def test_sf7_needs_extra_symbols_to_absorb_the_timing_error(self):
        # (2*6-8) symbols of 1.024 ms plus 2 x 5 ms of error is 13.8 symbols -> 14.
        assert rx_window_timeout_symbols(SpreadingFactor.SF7, Bandwidth.KHz125) == 14
        assert rx_window_duration(SpreadingFactor.SF7, Bandwidth.KHz125) == pytest.approx(
            14 * 128 / 125_000
        )

    def test_sf12_is_bounded_by_the_minimum_symbol_count(self):
        # A single SF12 symbol is longer than the whole timing error, so the minimum wins.
        assert rx_window_timeout_symbols(SpreadingFactor.SF12, Bandwidth.KHz125) == (
            RX_WINDOW_MIN_SYMBOLS
        )
        assert rx_window_duration(SpreadingFactor.SF12, Bandwidth.KHz125) == pytest.approx(
            RX_WINDOW_MIN_SYMBOLS * 4096 / 125_000
        )

    def test_without_timing_error_every_data_rate_waits_the_minimum(self):
        for dr in EU868_DATA_RATES.values():
            assert rx_window_timeout_symbols(
                dr.spreading_factor, dr.bandwidth, rx_error=0.0
            ) == RX_WINDOW_MIN_SYMBOLS

    def test_slower_data_rates_keep_the_window_open_longer(self):
        durations = [
            rx_window_duration(dr.spreading_factor, dr.bandwidth)
            for _, dr in sorted(EU868_DATA_RATES.items(), reverse=True)  # DR5 .. DR0
        ]
        assert durations == sorted(durations)
        assert durations[0] < 0.02   # DR5: ~14 ms
        assert durations[-1] < 0.25  # DR0: ~197 ms

    def test_accepts_plain_integers(self):
        assert rx_window_duration(7, 125) == rx_window_duration(
            SpreadingFactor.SF7, Bandwidth.KHz125
        )


def _scenario() -> tuple[NetworkServer, LoRaWanDevice]:
    LoraPhyLayer()
    ns = NetworkServer()
    ns.register_device(DEV_ADDR, NWK_S_KEY, APP_S_KEY)
    LoRaWanGateway(network_server=ns)
    device = LoRaWanDevice(
        session=DeviceSession(dev_addr=DEV_ADDR, nwk_s_key=NWK_S_KEY, app_s_key=APP_S_KEY),
        data_rate=5,
    )
    return ns, device


def _rx_time(device: LoRaWanDevice) -> float:
    """Seconds the device's radio spent in RX, from its power trace."""
    chain = next(c for c in device.radio.rx_chains if c.enabled)
    rx_power = device.radio.power_profile.rx_power(chain.config)
    events = device.radio.power_consumer.events
    assert events is not None
    durations = events["time"].shift(-1) - events["time"]
    return float(durations[events["power"] == rx_power].sum())


class TestClassAWindows:
    def test_empty_windows_cost_milliseconds_not_seconds(self):
        _ns, device = _scenario()
        outcome: list[bool] = []

        async def behaviour() -> None:
            await sim.sleep(1.0)
            outcome.append(await device.send_uplink(1, b"ping"))

        sim.create_task(behaviour())
        sim.run(simulation_length=6)

        assert outcome == [False]
        window = rx_window_duration(SpreadingFactor.SF7, Bandwidth.KHz125)
        rx_time = _rx_time(device)
        # Two windows, each the detection timeout plus the guard on either side. The old
        # fixed half-second window put this at a full second.
        assert rx_time >= 2 * window
        assert rx_time <= 2 * (window + 2 * RX_WINDOW_GUARD) + 0.005
        assert device.radio.get_state() in (RadioState.OFF, RadioState.STANDBY)

    def test_a_downlink_longer_than_the_window_is_still_received(self):
        ns, device = _scenario()
        outcome: list[bool] = []
        # ~110 ms on air at DR5: the frame starts at the window's opening instant and is
        # still going when the ~15 ms detection timeout expires.
        ns.queue_downlink(DEV_ADDR, 1, bytes(range(60)))

        async def behaviour() -> None:
            await sim.sleep(1.0)
            outcome.append(await device.send_uplink(1, b"ping"))

        sim.create_task(behaviour())
        sim.run(simulation_length=6)

        assert outcome == [True]
        assert _rx_time(device) > rx_window_duration(SpreadingFactor.SF7, Bandwidth.KHz125)

    def test_window_follows_the_device_data_rate(self):
        _ns, device = _scenario()
        device.data_rate = 0
        device._configure_radio()
        outcome: list[bool] = []

        async def behaviour() -> None:
            await sim.sleep(1.0)
            outcome.append(await device.send_uplink(1, b"ping"))

        sim.create_task(behaviour())
        sim.run(simulation_length=12)

        assert outcome == [False]
        window = rx_window_duration(SpreadingFactor.SF12, Bandwidth.KHz125)
        rx_time = _rx_time(device)
        assert rx_time >= 2 * window
        assert rx_time <= 2 * (window + 2 * RX_WINDOW_GUARD) + 0.005


class TestClassCRx1Window:
    def test_rx1_reply_is_received_and_continuous_rx_resumes(self):
        ns, device = _scenario()
        outcome: list[bool] = []
        ns.queue_downlink(DEV_ADDR, 1, bytes(range(60)))

        async def behaviour() -> None:
            await sim.sleep(0.5)
            await device.switch_mode(OperatingMode.CLASS_C)
            await sim.sleep(0.5)
            outcome.append(await device.send_uplink(1, b"ping"))

        sim.create_task(behaviour())
        sim.run(simulation_length=6)

        assert outcome == [True]
        assert device.radio.get_state() == RadioState.RX
