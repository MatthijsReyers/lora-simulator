import random, logging
from simulator.exceptions import SimulatorException
from simulator.lora.radio import LoraRadio
from simulator.environment import simulation_env as sim
from packets import *


class Node:
    logger = logging.getLogger(__name__)

    def __init__(self):
        self.node_id = None
        self.hardware_id = random.randint(0, 10000)
        self.radio = LoraRadio()
        sim.create_task(self.run())

    async def run(self):
        self.radio.set_rx_config(spreading_factor=7, bandwidth=125)
        self.radio.set_tx_config(power=4, spreading_factor=7, bandwidth=125)
        await sim.sleep(random.random()* 10)
        await self.__enroll()
        if self.node_id is None:
            self.logger.error(
                f'{sim.current_time():.4f} Node {self.hardware_id} failed to enroll'
            )
            return
        try:
            while sim.is_running():
                await sim.sleep(30)
                data_pkt = DataPacket(node_id=self.node_id, payload=random.randbytes(10))
                await self.radio.transmit_data_blocking(data_pkt.to_bytes())
        except SimulatorException:
            return

    async def __enroll(self):
        for retry in range(5):
            await sim.sleep(random.random() * retry) # Randomized backoff to desync colliding nodes
            enroll_req = EnrollmentRequestPacket(hardware_id=self.hardware_id)
            await self.radio.transmit_data_blocking(enroll_req.to_bytes())
            try:
                start = sim.current_time()
                data = await self.radio.receive_data_within(0.5)
                enroll_res = EnrollmentResponsePacket.from_bytes(data.payload)
                if enroll_res.hardware_id == self.hardware_id:
                    self.node_id = enroll_res.node_id
                    print(f'{sim.current_time():.4f} Node {self.hardware_id} enrolled with Node ID {self.node_id}')
                    return
            except TimeoutError: pass # Nothing received within timeout
            except AssertionError: pass # Packet was not EnrollmentResponsePacket
