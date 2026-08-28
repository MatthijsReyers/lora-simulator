"""Tests for carrier frequencies and multi-chain (gateway) radios."""

import asyncio
import pytest

from simulator.environment import simulation_env as sim
from simulator.lora.client_radio import LoraClientRadio
from simulator.lora.gateway_radio import LoraGatewayRadio
from simulator.lora.radio import LoraRadio, TooManyRxChainsException
from simulator.lora.radio_config import DEFAULT_FREQUENCY, LoraConfig
from simulator.lora.enums.bandwidth import Bandwidth
from simulator.lora.enums.code_rate import CodeRate
from simulator.lora.enums.spreading_factor import SpreadingFactor
from simulator.lorawan.region import EU868_DEFAULT_UPLINK_CHANNELS


# ── LoraConfig spectrum helpers ───────────────────────────────────────

class TestLoraConfigSpectrum:

    def test_same_channel(self):
        a = LoraConfig(frequency=868_100_000)
        b = LoraConfig(frequency=868_100_000)
        assert a.same_channel(b)

    def test_different_frequency_is_not_same_channel(self):
        a = LoraConfig(frequency=868_100_000)
        b = LoraConfig(frequency=868_300_000)
        assert not a.same_channel(b)

    def test_different_bandwidth_is_not_same_channel(self):
        a = LoraConfig(frequency=868_100_000, bandwidth=Bandwidth.KHz125)
        b = LoraConfig(frequency=868_100_000, bandwidth=Bandwidth.KHz250)
        assert not a.same_channel(b)

    def test_adjacent_125khz_channels_do_not_overlap(self):
        """The EU868 default channels are 200kHz apart, 125kHz signals on them never touch."""
        a = LoraConfig(frequency=868_100_000, bandwidth=Bandwidth.KHz125)
        b = LoraConfig(frequency=868_300_000, bandwidth=Bandwidth.KHz125)
        assert not a.overlaps(b)
        assert not b.overlaps(a)

    def test_wideband_signal_overlaps_neighbouring_channel(self):
        """A 500kHz signal on 868.1 covers 867.85-868.35 and hits a 125kHz signal on 868.3."""
        wide = LoraConfig(frequency=868_100_000, bandwidth=Bandwidth.KHz500)
        narrow = LoraConfig(frequency=868_300_000, bandwidth=Bandwidth.KHz125)
        assert wide.overlaps(narrow)
        assert narrow.overlaps(wide)

    def test_overlap_is_reflexive(self):
        a = LoraConfig(frequency=868_100_000)
        assert a.overlaps(a.copy())

    def test_copy_preserves_frequency(self):
        a = LoraConfig(frequency=869_525_000)
        assert a.copy().frequency == 869_525_000


class TestLoraConfigConstructor:
    """ The LoraConfig constructor accepts plain integers as enum shorthands and validates. """

    def test_int_shorthands(self):
        config = LoraConfig(bandwidth=250, spreading_factor=9, code_rate=6)
        assert config.bandwidth == Bandwidth.KHz250
        assert config.spreading_factor == SpreadingFactor.SF9
        assert config.code_rate == CodeRate.CR4_6

    def test_enums_pass_through(self):
        config = LoraConfig(
            bandwidth=Bandwidth.KHz500,
            spreading_factor=SpreadingFactor.SF12,
            code_rate=CodeRate.CR4_8,
        )
        assert config.bandwidth == Bandwidth.KHz500
        assert config.spreading_factor == SpreadingFactor.SF12
        assert config.code_rate == CodeRate.CR4_8

    def test_invalid_bandwidth_rejected(self):
        with pytest.raises(ValueError):
            LoraConfig(bandwidth=333)

    def test_invalid_frequency_rejected(self):
        with pytest.raises(AssertionError):
            LoraConfig(frequency=-1)
        with pytest.raises(AssertionError):
            LoraConfig(frequency=868.1e6)  # type: ignore[arg-type]

    def test_invalid_preamble_rejected(self):
        with pytest.raises(AssertionError):
            LoraConfig(preamble_len=0)


# ── Radio class hierarchy ─────────────────────────────────────────────

class TestRadioHierarchy:

    def test_lora_radio_is_abstract(self):
        with pytest.raises(TypeError):
            LoraRadio()  # type: ignore[abstract]

    def test_client_radio_has_one_chain(self):
        radio = LoraClientRadio()
        assert radio.max_rx_chains == 1
        assert len(radio.rx_chains) == 1

    def test_gateway_radio_defaults(self):
        radio = LoraGatewayRadio()
        assert radio.max_rx_chains == 8
        # Chains are only allocated when configured, a fresh radio has just the primary.
        assert len(radio.rx_chains) == 1

    def test_gateway_radio_single_chain_mode(self):
        """Multi-chain support can be turned off by limiting the radio to one chain."""
        radio = LoraGatewayRadio(max_chains=1)
        with pytest.raises(TooManyRxChainsException):
            radio.add_rx_chain(frequency=868_300_000)


# ── Frequency configuration semantics ─────────────────────────────────

class TestFrequencyConfiguration:

    def test_default_frequency(self):
        radio = LoraClientRadio()
        assert radio.rx_frequency == DEFAULT_FREQUENCY
        assert radio.tx_frequency == DEFAULT_FREQUENCY

    def test_set_channel_retunes_rx_and_tx(self):
        radio = LoraClientRadio()
        radio.set_channel(869_525_000)
        assert radio.rx_frequency == 869_525_000
        assert radio.tx_frequency == 869_525_000

    def test_rx_config_keeps_frequency(self):
        """Like the real HAL, set_rx_config without a frequency must not retune the radio."""
        radio = LoraClientRadio()
        radio.set_channel(868_500_000)
        radio.set_rx_config(spreading_factor=9, bandwidth=125)
        assert radio.rx_frequency == 868_500_000

    def test_tx_config_keeps_frequency(self):
        radio = LoraClientRadio()
        radio.set_channel(868_500_000)
        radio.set_tx_config(power=14, spreading_factor=9)
        assert radio.tx_frequency == 868_500_000

    def test_explicit_frequency_in_config(self):
        radio = LoraClientRadio()
        radio.set_rx_config(frequency=868_300_000)
        radio.set_tx_config(power=14, frequency=868_300_000)
        assert radio.rx_frequency == 868_300_000
        assert radio.tx_frequency == 868_300_000


# ── Gateway chain management ──────────────────────────────────────────

class TestGatewayChains:

    def test_channel_plan_via_constructor(self):
        radio = LoraGatewayRadio(frequencies=EU868_DEFAULT_UPLINK_CHANNELS)
        freqs = [c.config.frequency for c in radio.rx_chains if c.enabled]
        assert freqs == EU868_DEFAULT_UPLINK_CHANNELS

    def test_add_rx_chain_returns_index(self):
        radio = LoraGatewayRadio(max_chains=2)
        chain = radio.add_rx_chain(frequency=868_300_000)
        assert chain == 1
        assert radio.rx_chains[chain].config.frequency == 868_300_000

    def test_too_many_chains(self):
        radio = LoraGatewayRadio(max_chains=2)
        radio.add_rx_chain(frequency=868_300_000)
        with pytest.raises(TooManyRxChainsException):
            radio.add_rx_chain(frequency=868_500_000)

    def test_channel_plan_too_large(self):
        radio = LoraGatewayRadio(max_chains=2)
        with pytest.raises(TooManyRxChainsException):
            radio.set_channel_plan(EU868_DEFAULT_UPLINK_CHANNELS)

    def test_disable_chain(self):
        radio = LoraGatewayRadio(frequencies=EU868_DEFAULT_UPLINK_CHANNELS)
        radio.enable_rx_chain(1, enabled=False)
        assert not radio.rx_chains[1].enabled
        assert radio.rx_chain_for(868_300_000) is None
        radio.enable_rx_chain(1, enabled=True)
        assert radio.rx_chain_for(868_300_000) == 1

    def test_smaller_channel_plan_disables_leftover_chains(self):
        radio = LoraGatewayRadio(frequencies=EU868_DEFAULT_UPLINK_CHANNELS)
        radio.set_channel_plan([868_100_000])
        enabled = [c for c in radio.rx_chains if c.enabled]
        assert len(enabled) == 1
        assert enabled[0].config.frequency == 868_100_000


# ── End-to-end reception across frequencies ───────────────────────────

class TestMultiFrequencyReception:

    def test_gateway_receives_parallel_channels_client_does_not(self):
        """
            Three simultaneous transmissions on the three EU868 default channels: a gateway
            radio listening to all of them receives every packet (on the right chain), while a
            client radio tuned to just one channel only hears that channel.
        """
        received: list[tuple[str, bytes, int | None]] = []

        gateway = LoraGatewayRadio(frequencies=EU868_DEFAULT_UPLINK_CHANNELS)
        client = LoraClientRadio()
        client.set_channel(868_300_000)

        senders = []
        for (i, freq) in enumerate(EU868_DEFAULT_UPLINK_CHANNELS):
            sender = LoraClientRadio()
            sender.set_tx_config(power=14, frequency=freq)
            senders.append(sender)

        async def tx_task(sender: LoraClientRadio, delay: float, payload: bytes):
            await sim.sleep(delay)
            await sender.transmit_data_blocking(payload)

        async def gw_rx_task():
            await gateway.receive(continuous=True)
            while sim.is_running():
                try:
                    (packet, meta) = await gateway.receive_data_wait(metadata=True)
                    received.append(("gw", packet.payload, meta.chain_id))
                except asyncio.TimeoutError:
                    return

        async def client_rx_task():
            await client.receive(continuous=True)
            while sim.is_running():
                try:
                    packet = await client.receive_data_wait()
                    received.append(("client", packet.payload, None))
                except asyncio.TimeoutError:
                    return

        sim.create_task(gw_rx_task())
        sim.create_task(client_rx_task())
        for (i, sender) in enumerate(senders):
            sim.create_task(tx_task(sender, 0.1 + i * 0.001, b"ch%d" % i))

        sim.run(simulation_length=2)

        gw_payloads = sorted(p for (who, p, _) in received if who == "gw")
        assert gw_payloads == [b"ch0", b"ch1", b"ch2"]

        # Each packet must have been demodulated by the chain tuned to its channel.
        gw_chains = {p: c for (who, p, c) in received if who == "gw"}
        assert gw_chains == {b"ch0": 0, b"ch1": 1, b"ch2": 2}

        # The single-channel client radio only hears the packet on its own channel.
        client_payloads = [p for (who, p, _) in received if who == "client"]
        assert client_payloads == [b"ch1"]
