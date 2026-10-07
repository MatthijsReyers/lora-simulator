"""
The PHY layer only presents a transmission to the radios that are in receive mode when it
starts. Radios that are off, in standby or transmitting never learn about the packet, which
both mirrors real receivers and keeps the cost of a transmission proportional to the number
of listening radios instead of the number of radios in the simulation.
"""
import pytest

from simulator.environment import simulation_env as sim
from simulator.lora.client_radio import LoraClientRadio
from simulator.lora.enums.radio_state import RadioState
from simulator.lora.phy_layer import LoraPhyLayer
from simulator.path_loss.constant_loss import constant_path_loss

SIMULATION_LENGTH = 20_000_000  # 20 s of 1 us ticks


def make_radios(count: int) -> list[LoraClientRadio]:
    LoraPhyLayer(path_loss=constant_path_loss(60.0))
    radios = [LoraClientRadio(position=(float(i), 0.0)) for i in range(count)]
    for radio in radios:
        radio.set_tx_config(power=14)
        radio.set_rx_config()
    return radios


def count_notifications(radio: LoraClientRadio) -> list[int]:
    """ Replaces the radio's receive-start callback with one that also counts its calls. """
    calls = [0]
    original = radio._on_receive_start

    def counting(packet):
        calls[0] += 1
        return original(packet)

    radio._on_receive_start = counting  # type: ignore[method-assign]
    return calls


def test_only_listening_radios_are_notified():
    sender, listener, *idle = make_radios(6)
    listener_calls = count_notifications(listener)
    idle_calls = [count_notifications(radio) for radio in idle]
    received = []

    async def listen():
        await listener.receive()
        received.append(await listener.receive_data_within(5.0))

    async def send():
        await sim.sleep(0.1)
        await sender.transmit_data_blocking(b"hello")

    sim.create_task(listen())
    sim.create_task(send())
    sim.run(simulation_length=SIMULATION_LENGTH)

    assert received and received[0].payload == b"hello"
    assert listener_calls == [1]
    assert all(calls == [0] for calls in idle_calls)
    for radio in idle:
        assert radio.get_state() == RadioState.OFF
        assert radio.rx_chains[0].packets_in_transit == {}


def test_radio_turned_on_mid_packet_does_not_see_it():
    sender, late = make_radios(2)
    seen_busy = []

    async def send():
        await sim.sleep(0.1)
        await sender.transmit_data_blocking(b"x" * 50)  # ~100 ms at SF7

    async def wake_up_late():
        await sim.sleep(0.15)  # the packet is in the air by now
        await late.receive()
        seen_busy.append(late.carrier_sense_instant())
        with pytest.raises(TimeoutError):
            await late.receive_data_within(1.0)

    sim.create_task(send())
    sim.create_task(wake_up_late())
    sim.run(simulation_length=SIMULATION_LENGTH)

    # The receiver was off when the packet started, so it neither receives it nor regards the
    # channel as busy while the tail of the packet is still in the air.
    assert seen_busy == [False]
    assert late.rx_chains[0].packets_in_transit == {}


def test_unseen_packet_does_not_collide_with_a_later_one():
    sender_a, sender_b, late = make_radios(3)
    LoraPhyLayer().enable_capture_effect = False  # any overlap of known packets is a collision
    received = []

    async def send_a():
        await sim.sleep(0.1)
        await sender_a.transmit_data_blocking(b"a" * 200)  # ~370 ms at SF7

    async def send_b():
        await sim.sleep(0.3)  # overlaps with the tail of A
        await sender_b.transmit_data_blocking(b"b")

    async def wake_up_late():
        await sim.sleep(0.2)  # A is on the air, the receiver missed its start
        await late.receive()
        received.append(await late.receive_data_within(2.0))

    for task in (send_a(), send_b(), wake_up_late()):
        sim.create_task(task)
    sim.run(simulation_length=SIMULATION_LENGTH)

    # Packet A was invisible to the late receiver, so it cannot spoil the reception of B.
    assert [packet.payload for packet in received] == [b"b"]


def test_listening_radio_that_turns_off_mid_packet_still_misses_the_end():
    sender, receiver = make_radios(2)
    outcome = []

    async def listen_briefly():
        await receiver.receive()
        await sim.sleep(0.15)  # the packet started at 0.1 s
        await receiver.off()
        await sim.sleep(1.0)
        outcome.append(receiver.rx_chains[0].packets_in_transit == {})
        outcome.append(await receiver.receive_data_nowait())

    async def send():
        await sim.sleep(0.1)
        await sender.transmit_data_blocking(b"x" * 50)

    sim.create_task(listen_briefly())
    sim.create_task(send())
    sim.run(simulation_length=SIMULATION_LENGTH)

    # The radio was listening at the start so it tracked the packet, but turning off before the
    # end loses it (`missed_end`): bookkeeping is cleaned up and nothing is delivered.
    assert outcome == [True, None]


def test_transmitting_radio_does_not_track_concurrent_packets():
    sender, busy = make_radios(2)
    seen_busy = []

    async def transmit_then_check():
        await sim.sleep(0.05)
        await busy.transmit_data_blocking(b"y" * 100)  # ~190 ms, so ends after sender's start
        await busy.receive()
        seen_busy.append(busy.carrier_sense_instant())

    async def send():
        await sim.sleep(0.1)
        await sender.transmit_data_blocking(b"x" * 200)  # still in the air when `busy` is done

    sim.create_task(transmit_then_check())
    sim.create_task(send())
    sim.run(simulation_length=SIMULATION_LENGTH)

    assert seen_busy == [False]
