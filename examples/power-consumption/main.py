import asyncio, random
import sys, logging
sys.path.append('.') # To allow importing the simulator package while running from root folder.
from simulator.lora.client_radio import LoraClientRadio
from simulator.environment import simulation_env as sim

class NodePing:
    """ This nodes wakes up after a random delay to send a "Ping" messages and wait for a Pong """
    def __init__(self, node_id: int):
        self.radio = LoraClientRadio()
        self.node_id = node_id
        sim.create_task(self.run())

    async def run(self):
        self.radio.set_rx_config(spreading_factor=7, bandwidth=125)
        self.radio.set_tx_config(power=16, spreading_factor=7, bandwidth=125)
        while sim.is_running():
            try:
                await self.radio.off()
                await sim.sleep( random.uniform(1, 5) )
                await self.radio.transmit_data_blocking(b"Ping")
                print(f'{sim.current_time():.4f} PING (node {self.node_id})')
                packet = await self.radio.receive_data_within(timeout=0.4)
                if packet.payload != b"Pong":
                    print(f'{sim.current_time():.4f} Node {self.node_id} Unexpected response: {packet.payload}')
            except asyncio.TimeoutError:
                print(f'{sim.current_time():.4f} Node {self.node_id} Receive timed out')

class NodePong:
    """ This nodes continuously listens for "Ping" messages and responds with "Pong" """
    def __init__(self):
        self.radio = LoraClientRadio()
        sim.create_task(self.run())

    async def run(self):
        self.radio.set_rx_config(spreading_factor=7, bandwidth=125)
        while sim.is_running():
            try:
                packet = await self.radio.receive_data_wait()
                if packet.payload == b"Ping":
                    await self.radio.transmit_data_blocking(b"Pong")
                    print(f'{sim.current_time():.4f} PONG')
            except asyncio.TimeoutError:
                print(f'{sim.current_time():.4f} Pong node received timed out')


if __name__ == "__main__":
    ping_nodes = [ NodePing(i) for i in range(5) ]
    pong_node = NodePong()

    sim.run(simulation_length=15)

    for pn in ping_nodes:
        pn.radio.power_consumer.events.to_csv(
            f'examples/power-consumption/ping_node_{pn.node_id}_power_events.csv', 
            index=False
        )