from examples.gateway.packets import *
from simulator.environment import simulation_env as sim
from simulator.lora.radio import LoraRadio

class Gateway:
    def __init__(self):
        # Hardware ID to Node ID mapping
        self.node_ids = {}
        self.node_id_counter = 1

        self.radio = LoraRadio()
        sim.create_task(self.run())

    async def run(self):
        self.radio.set_rx_config(spreading_factor=7, bandwidth=125)
        self.radio.set_tx_config(power=14, spreading_factor=7, bandwidth=125)
        while sim.is_running():
            try:
                packet = await self.radio.receive_data_wait()
                packet = packet_from_bytes(packet.payload)
                # print(f'{sim.current_time():.4f} Gateway received {packet}')
                match packet.pkt_type:
                    case DemoPacketType.ENROLL_REQUEST:
                        await self.handle_enroll_request(packet)
                    case DemoPacketType.ENROLL_RESPONSE:
                        print("BUG: Somehow received ENROLL_RESPONSE at gateway")
                    case DemoPacketType.DATA:
                        print(f"{sim.current_time():.4f} Gateway received DATA from Node ID {packet.node_id}: {packet.payload}")
            except TimeoutError:
                return

    async def handle_enroll_request(self, packet: EnrollmentRequestPacket):
        hardware_id = packet.hardware_id

        if hardware_id not in self.node_ids:
            node_id = self.node_id_counter
            self.node_id_counter += 1
            self.node_ids[hardware_id] = node_id 
        else:
            node_id = self.node_ids[hardware_id]

        response_packet = EnrollmentResponsePacket(
            node_id=node_id,
            hardware_id=hardware_id
        )
        
        await self.radio.transmit_data_blocking(response_packet.to_bytes())
        print(f'{sim.current_time():.4f} Gateway sent ENROLL_RESPONSE {node_id} -> {hardware_id}')

