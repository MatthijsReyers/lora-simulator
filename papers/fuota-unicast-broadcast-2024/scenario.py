"""
Everything the three update methods of the paper have in common: the simulation parameters of
Table 1, the placement of the nodes (Figure 6), the channel model, the duty cycle rule of
section 4 and the base class for the gateway and the nodes.
"""
import logging, math, random
from dataclasses import dataclass, field

from simulator.environment import simulation_env as sim
from simulator.lora.airtime import estimate_airtime
from simulator.lora.client_radio import LoraClientRadio
from simulator.lora.enums.spreading_factor import SpreadingFactor
from simulator.lora.phy_layer import LoraPhyLayer
from simulator.path_loss.log_distance_path_loss import log_distance_path_loss
from simulator.path_loss.nakagami_path_loss import nakagami_path_loss

from frames import COMPACT_HEADER_LEN, FRAGMENT_OVERHEAD, UNICAST_HEADER_LEN, MiWiFrame


@dataclass
class Scenario:
    """ Simulation parameters, the defaults are those of Table 1 of the paper. """
    nodes: int = 10
    radius: float = 400.0               # meters, nodes are placed at uniform distances in [0, radius]
    firmware_size: int = 100 * 1024     # bytes
    frame_len: int = 215                # bytes, including the 23 byte MiWi header
    # Whether the 23 byte MiWi header is actually transmitted. The simulations of the paper only
    # put the 192 byte payload (plus a few bytes of ns-3 header) on the air, see the README.
    header_on_air: bool = True
    duty_cycle: float = 0.01
    # The paper treats the duty cycle as a medium wide quiet time after every frame (section 4),
    # real regulations only limit each transmitter individually. See `DutyCycle`.
    shared_duty_cycle: bool = True
    spreading_factor: int = 7
    bandwidth: int = 125                # kHz
    code_rate: int = 5                  # 4/5
    preamble_len: int = 8
    tx_power: int = 14                  # dBm
    sensitivity: float = -125.0         # dBm
    path_loss_exponent: float = 3.2     # calibrated, see README
    seed: int = 0
    processing_delay: float = 0.05      # seconds a node takes to answer a frame
    checksum_delay: float = 1.0         # seconds the gateway gives a node to verify the checksum
    reboot_delay: float = 10.0          # seconds a node is off the air while flashing and rebooting
    old_version: int = 1
    new_version: int = 2

    @property
    def header_len(self) -> int:
        """ Bytes of frame header that go on the air in front of every OTA payload. """
        return UNICAST_HEADER_LEN if self.header_on_air else COMPACT_HEADER_LEN

    @property
    def chunk_size(self) -> int:
        """
            Firmware bytes per fragment. With the full header on the air the chunk is sized so
            every fragment frame fits in `frame_len`, with the paper's compact frames it is the
            `frame_len - 23` payload of Table 1 (192 bytes by default).
        """
        size = self.frame_len - UNICAST_HEADER_LEN
        if self.header_on_air:
            size -= FRAGMENT_OVERHEAD
        assert size > 0, f"Frame length {self.frame_len} leaves no room for firmware data"
        return size

    @property
    def fragments(self) -> int:
        return math.ceil(self.firmware_size / self.chunk_size)

    def airtime(self, frame_len: int) -> float:
        """ Time on air in seconds of a frame of `frame_len` bytes with this scenario's modulation. """
        return estimate_airtime(
            payload_len=frame_len,
            bandwidth=self.bandwidth,
            spreading_factor=self.spreading_factor,
            code_rate=self.code_rate,
            preamble_len=self.preamble_len,
        )

    def node_positions(self, rng: random.Random) -> list[tuple[float, float]]:
        """ Places the nodes around the gateway (at the origin) like Figure 6 of the paper. """
        positions = []
        for _ in range(self.nodes):
            distance = rng.uniform(0, self.radius)
            angle = rng.uniform(0, 2 * math.pi)
            positions.append((distance * math.cos(angle), distance * math.sin(angle)))
        return positions

    def make_firmware(self, rng: random.Random) -> bytes:
        """ A random firmware image, the first byte holds the version number. """
        return bytes([self.new_version]) + rng.randbytes(self.firmware_size - 1)

    def setup_phy(self) -> LoraPhyLayer:
        """
            Configures the channel model: log-distance path loss with Nakagami fading like the
            ns-3 setup of the paper, and a noise floor that puts the receiver sensitivity exactly
            at `sensitivity` for the used spreading factor.
        """
        sf = SpreadingFactor(self.spreading_factor)
        phy = LoraPhyLayer(
            path_loss=nakagami_path_loss(
                base=log_distance_path_loss(exponent=self.path_loss_exponent, sigma=0.0),
            ),
            noise_floor=self.sensitivity - sf.minimum_snr(),
        )
        # Only one device transmits at a time in these protocols, the detailed capture effect
        # calculations would only cost time.
        phy.enable_capture_effect = False
        return phy

    def configure_radio(self, radio: LoraClientRadio) -> None:
        radio.set_rx_config(
            bandwidth=self.bandwidth,
            spreading_factor=self.spreading_factor,
            code_rate=self.code_rate,
            preamble_len=self.preamble_len,
            max_payload_len=255,
        )
        radio.set_tx_config(
            power=self.tx_power,
            bandwidth=self.bandwidth,
            spreading_factor=self.spreading_factor,
            code_rate=self.code_rate,
            preamble_len=self.preamble_len,
        )


class DutyCycle:
    """
        Duty cycle bookkeeping as described in section 4 of the paper: after a frame with a time
        on air of `ToA` nothing may be sent for `(100/d - 1) * ToA` seconds.

        When one instance is shared between all devices this reproduces the paper's model where
        the quiet time applies to the whole medium, i.e. a node that received a frame waits out
        the quiet time before it answers. Give every device its own instance to model the
        per-transmitter limit of the actual regulations instead.
    """

    def __init__(self, duty_cycle: float, shared: bool):
        assert 0 < duty_cycle <= 1, "Duty cycle must be a fraction in (0, 1]"
        self.duty_cycle = duty_cycle
        self.shared = shared
        self.quiet_until = 0.0

    def quiet_time(self, airtime: float) -> float:
        """ Equation (1) of the paper, the time that must be left free after a frame. """
        return airtime * (1 / self.duty_cycle - 1)

    def reserve(self, airtime: float) -> None:
        """
            Blocks the medium for a transmission of `airtime` seconds starting now plus the quiet
            time that follows it. Call this *before* transmitting: the receiver may be scheduled
            to answer in the very same simulation tick the transmission ends in, and it has to
            see the quiet time by then.
        """
        end_of_quiet = sim.current_time() + airtime + self.quiet_time(airtime)
        self.quiet_until = max(self.quiet_until, end_of_quiet)

    async def wait_for_slot(self) -> None:
        if sim.current_time() < self.quiet_until:
            await sim.sleep_until(self.quiet_until)


def make_duty_cycles(scenario: Scenario, devices: int) -> list[DutyCycle]:
    """ One `DutyCycle` per device, or the same one for all of them (see `DutyCycle`). """
    if scenario.shared_duty_cycle:
        shared = DutyCycle(scenario.duty_cycle, shared=True)
        return [shared] * devices
    return [DutyCycle(scenario.duty_cycle, shared=False) for _ in range(devices)]


class OtaDevice:
    """ Base class for the gateway and the nodes, owns the radio and sends MiWi frames. """

    def __init__(
        self,
        address: int,
        position: tuple[float, float],
        scenario: Scenario,
        duty_cycle: DutyCycle,
    ):
        self.address = address
        self.scenario = scenario
        self.duty_cycle = duty_cycle
        self.radio = LoraClientRadio(position=position)
        scenario.configure_radio(self.radio)
        self.sequence = 0
        self.frames_sent = 0
        self.logger = logging.getLogger(f"fuota.{type(self).__name__}-{address}")

    async def transmit(self, destination: int, payload: bytes) -> float:
        """
            Sends a frame, respecting the duty cycle. Blocks for the time on air of the frame and
            returns it.
        """
        frame = MiWiFrame(
            source=self.address, destination=destination, payload=payload, sequence=self.sequence,
        )
        self.sequence += 1
        data = frame.to_bytes(compact=not self.scenario.header_on_air)
        airtime = self.scenario.airtime(len(data))
        await self.duty_cycle.wait_for_slot()
        self.duty_cycle.reserve(airtime)
        actual_airtime = await self.radio.transmit_data_blocking(data)
        assert math.isclose(airtime, actual_airtime), "Airtime estimate does not match the radio"
        self.frames_sent += 1
        return airtime

    def decode_frame(self, data: bytes) -> MiWiFrame:
        return MiWiFrame.from_bytes(data, compact=not self.scenario.header_on_air)
