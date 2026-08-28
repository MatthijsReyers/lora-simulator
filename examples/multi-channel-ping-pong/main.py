#!/usr/bin/env python3
"""
    Multi-channel ping pong example.

    Three nodes each ping on a different EU868 default uplink channel at (almost) the same time.
    The gateway radio has a receive chain listening on each of the three channels, so unlike a
    single channel radio it demodulates all three overlapping pings in parallel. It then answers
    each ping with a pong on the channel the ping arrived on, which the node in question is
    listening to.

    Try replacing the LoraGatewayRadio with a LoraClientRadio (or constructing it with
    max_chains=1 and a single frequency) to see two of the three pings go unanswered.
"""
import sys, asyncio

sys.path.append('.')

PING_INTERVAL = 15  # seconds between ping rounds
PING_ROUNDS = 3

from simulator.environment import simulation_env as sim
from simulator.lora.client_radio import LoraClientRadio
from simulator.lora.gateway_radio import LoraGatewayRadio
from simulator.lorawan.region import EU868_DEFAULT_UPLINK_CHANNELS


class Node:
    """ A simple end device that pings on a single fixed channel and waits for the pong. """

    def __init__(self, name: str, frequency: int):
        self.name = name
        self.frequency = frequency
        self.radio = LoraClientRadio()
        # A client radio has a single receive chain, so this node can only ever talk and listen
        # on the one channel it is tuned to.
        self.radio.set_channel(frequency)
        self.radio.set_tx_config(power=14)
        sim.create_task(self.run())

    async def run(self):
        for i in range(PING_ROUNDS):
            # Sleep until the start of the next ping round. Note that we deliberately keep all
            # three nodes in sync so that their pings always overlap in time, if the pings were
            # staggered some would arrive while the (half-duplex!) gateway is busy transmitting
            # a pong and go unanswered.
            round_start = i * PING_INTERVAL
            if round_start > sim.current_time():
                await sim.sleep_until(round_start)

            print(f'{sim.current_time():8.4f} {self.name} pinging on {self.frequency/1e6:.1f}MHz')
            await self.radio.transmit_data_blocking(b'Ping ' + self.name.encode())
            await self.radio.receive(continuous=True)

            try:
                packet = await self.radio.receive_data_within(5.0)
                print(f'{sim.current_time():8.4f} {self.name} got reply: {packet.payload}')
            except (asyncio.TimeoutError, TimeoutError):
                print(f'{sim.current_time():8.4f} {self.name} no reply within 5s!')

            await self.radio.off()


class Gateway:
    """ A gateway that listens on all three EU868 default channels simultaneously. """

    def __init__(self):
        self.radio = LoraGatewayRadio(frequencies=EU868_DEFAULT_UPLINK_CHANNELS)
        sim.create_task(self.run())

    async def run(self):
        await self.radio.receive(continuous=True)
        while sim.is_running():
            try:
                (packet, meta) = await self.radio.receive_data_wait(metadata=True)
            except (asyncio.TimeoutError, TimeoutError):
                return

            freq = packet.config.frequency
            print(
                f'{sim.current_time():8.4f} GW received {packet.payload} on chain '
                f'{meta.chain_id} ({freq/1e6:.1f}MHz), ponging back'
            )

            # Answer on the channel the ping came in on. The gateway has a single transmitter,
            # so replies to overlapping pings go out one after another.
            self.radio.set_tx_config(power=14, frequency=freq)
            await self.radio.transmit_data_blocking(b'Pong ' + packet.payload[5:])
            await self.radio.receive(continuous=True)


if __name__ == "__main__":
    gateway = Gateway()
    nodes = [
        Node(name=f'Node{i+1}', frequency=freq)
        for (i, freq) in enumerate(EU868_DEFAULT_UPLINK_CHANNELS)
    ]

    sim.run(simulation_length=PING_ROUNDS * PING_INTERVAL)
