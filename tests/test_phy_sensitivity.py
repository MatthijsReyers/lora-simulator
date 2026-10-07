"""
Receiver sensitivity: by default the noise floor plus the SNR requirement of the spreading
factor, optionally overridden per spreading factor (or by any function of the receive
configuration) so that datasheet or paper values can be used verbatim.
"""
import math

import pytest

from simulator.environment import simulation_env as sim
from simulator.lora.client_radio import LoraClientRadio
from simulator.lora.enums.spreading_factor import SpreadingFactor
from simulator.lora.phy_layer import LoraPhyLayer
from simulator.lora.radio_config import LoraConfig
from simulator.path_loss.constant_loss import constant_path_loss

# Table I of Georgiou & Raza, "Low power wide area network analysis: Can LoRa scale?" (2017).
GEORGIOU_RAZA = {7: -123.0, 8: -126.0, 9: -129.0, 10: -132.0, 11: -134.5, 12: -137.0}


def test_default_sensitivity_is_noise_floor_plus_snr_requirement():
    phy = LoraPhyLayer(noise_floor=-117.0)
    for sf in SpreadingFactor:
        config = LoraConfig(spreading_factor=sf)
        assert phy.sensitivity(config) == pytest.approx(-117.0 + sf.minimum_snr())


def test_table_override_accepts_int_and_enum_keys():
    phy = LoraPhyLayer(sensitivity=GEORGIOU_RAZA)
    assert phy.sensitivity(LoraConfig(spreading_factor=7)) == -123.0
    assert phy.sensitivity(LoraConfig(spreading_factor=SpreadingFactor.SF11)) == -134.5
    # The table does not know SF6, which must be an error rather than a silent fallback.
    with pytest.raises(KeyError):
        phy.sensitivity(LoraConfig(spreading_factor=SpreadingFactor.SF6))


def test_callable_override_sees_the_whole_receive_configuration():
    phy = LoraPhyLayer()
    phy.set_sensitivity(lambda config: -130.0 + 10 * math.log10(config.bandwidth.to_hz() / 125e3))
    assert phy.sensitivity(LoraConfig(bandwidth=125)) == pytest.approx(-130.0)
    assert phy.sensitivity(LoraConfig(bandwidth=500)) == pytest.approx(-130.0 + 10 * math.log10(4))


def test_override_can_be_removed_again():
    phy = LoraPhyLayer(noise_floor=-120.0, sensitivity=GEORGIOU_RAZA)
    phy.set_sensitivity(None)
    config = LoraConfig(spreading_factor=7)
    assert phy.sensitivity(config) == pytest.approx(-120.0 + SpreadingFactor.SF7.minimum_snr())


def test_reconfiguring_the_singleton_keeps_the_override():
    LoraPhyLayer(sensitivity=GEORGIOU_RAZA)
    phy = LoraPhyLayer(noise_floor=-110.0)  # e.g. a radio or example touching other settings
    assert phy.sensitivity(LoraConfig(spreading_factor=12)) == -137.0


def _run_link(path_loss: float, tx_power: int = 14, sf: int = 7) -> bool:
    """ Sends one packet over a link with a fixed loss and reports whether it was received. """
    LoraPhyLayer(path_loss=constant_path_loss(path_loss))
    sender = LoraClientRadio(position=(0.0, 0.0))
    receiver = LoraClientRadio(position=(100.0, 0.0))
    sender.set_tx_config(power=tx_power, spreading_factor=sf)
    receiver.set_rx_config(spreading_factor=sf)
    received = []

    async def listen():
        await receiver.receive()
        try:
            await receiver.receive_data_within(10.0)
            received.append(True)
        except TimeoutError:
            pass

    async def send():
        await sim.sleep(0.1)
        await sender.transmit_data_blocking(b"hello")

    sim.create_task(listen())
    sim.create_task(send())
    sim.run(simulation_length=20_000_000)
    return bool(received)


def test_packet_between_default_and_overridden_sensitivity_flips_reception():
    # With the default -120dBm noise floor SF7 needs -127.5dBm, with the paper table -123dBm.
    # A packet arriving at 14 - 139 = -125dBm is lost with the table and received without it.
    LoraPhyLayer(noise_floor=-120.0, sensitivity=GEORGIOU_RAZA)
    assert _run_link(path_loss=139.0) is False


def test_packet_between_default_and_overridden_sensitivity_without_override():
    LoraPhyLayer(noise_floor=-120.0)
    assert _run_link(path_loss=139.0) is True


def test_sensitivity_boundary_is_inclusive():
    LoraPhyLayer(sensitivity=GEORGIOU_RAZA)
    assert _run_link(path_loss=137.0) is True   # exactly -123dBm
