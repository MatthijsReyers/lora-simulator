"""
The PHY layer can be given a frame loss: a share of the frames that a receiver does not hear at
all, whatever the link budget. It is what gives scenarios with different protocols the same
packet loss ratio.
"""
import pytest

from simulator.environment import simulation_env as sim
from simulator.lora.client_radio import LoraClientRadio
from simulator.lora.phy_layer import LoraPhyLayer
from simulator.path_loss.constant_loss import constant_path_loss

FRAMES = 400


def received_frames(frame_loss: float | None, seed: int = 1, listeners: int = 1) -> list[list[int]]:
    """ Sends `FRAMES` numbered frames and returns the numbers every listener received. """
    LoraPhyLayer(path_loss=constant_path_loss(60.0), frame_loss=frame_loss, frame_loss_seed=seed)
    sender = LoraClientRadio(position=(0.0, 0.0))
    sender.set_tx_config(power=14)
    radios = [LoraClientRadio(position=(float(i + 1), 0.0)) for i in range(listeners)]
    received: list[list[int]] = [[] for _ in radios]

    async def listen(radio: LoraClientRadio, numbers: list[int]):
        radio.set_rx_config()
        await radio.receive()
        while True:
            try:
                packet = await radio.receive_data_within(5.0)
            except TimeoutError:
                return  # the sender is done
            numbers.append(int.from_bytes(packet.payload, "big"))

    async def send():
        await sim.sleep(0.1)
        for number in range(FRAMES):
            await sender.transmit_data_blocking(number.to_bytes(2, "big"))
            await sim.sleep(0.01)

    for (radio, numbers) in zip(radios, received):
        sim.create_task(listen(radio, numbers))
    sim.create_task(send())
    sim.run(simulation_length=60)
    return received


def test_nothing_is_lost_by_default():
    assert received_frames(None) == [list(range(FRAMES))]


def test_the_configured_share_is_lost():
    (received,) = received_frames(0.25)
    assert len(received) == pytest.approx(0.75 * FRAMES, abs=0.08 * FRAMES)


def test_everything_is_lost_at_one():
    assert received_frames(1.0) == [[]]


def test_receivers_lose_frames_independently():
    (first, second) = received_frames(0.25, listeners=2)
    assert first != second
    # Were they to lose the same frames, a quarter would be missing at both.
    assert len(set(first) | set(second)) > 0.88 * FRAMES


def test_lost_frame_is_logged_as_not_demodulated():
    LoraPhyLayer(path_loss=constant_path_loss(60.0), frame_loss=1.0)
    sender = LoraClientRadio(position=(0.0, 0.0))
    sender.set_tx_config(power=14)
    listener = LoraClientRadio(position=(1.0, 0.0))

    async def run():
        listener.set_rx_config()
        await listener.receive()
        await sim.sleep(0.1)
        await sender.transmit_data_blocking(b"frame")

    sim.create_task(run())
    sim.run(simulation_length=5)
    log = listener.packets_log
    assert len(log) == 1
    assert bool(log.iloc[0].demodulate_failure)
