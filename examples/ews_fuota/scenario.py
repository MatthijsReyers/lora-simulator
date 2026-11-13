"""
    Parameters of the EWS FUOTA scenario, the sensor's power model and the per-device radio log.

    Every protocol number comes from the sensor firmware (EWS_Firmware-rs, branch fuota-downlink)
    and the radio-protocol crate. The values marked ASSUMPTION are not fixed by any code that was
    available, see the README.
"""
import csv
import math
from dataclasses import dataclass, field
from pathlib import Path

from simulator.environment import simulation_env as sim
from simulator.lora.airtime import estimate_airtime
from simulator.lora.phy_layer import LoraPhyLayer
from simulator.path_loss.constant_loss import constant_path_loss
from simulator.path_loss.log_distance_path_loss import log_distance_path_loss

import frames


def channel(index: int) -> int:
    """ Frequency in hertz of a channel of the EWS channel plan (200 kHz raster from 863.1 MHz). """
    return 863_100_000 + 200_000 * index


@dataclass
class Scenario:
    sensors: int = 10
    hours: float = 24.0
    seed: int = 1

    # Every sensor pings once per interval, in its own static slot at the start of it. The
    # gateway updates one sensor per interval: it first moves that sensor's ping schedule to
    # the FUOTA moment, well away from the ping slots, so that no ping falls inside a transfer.
    ping_interval: float = 3600.0
    ping_lead: float = 1.0  # from the start of the interval to the first slot
    ping_slot: float = 3.0  # room for five attempts and an ACK with commands
    fuota_offset: float = 1800.0  # from the start of the interval to the FUOTA moment
    image_bytes: int = 100_000

    # Modulation, identical for every frame in both directions.
    spreading_factor: int = 7
    bandwidth: int = 125
    code_rate: int = 5
    preamble_len: int = 8
    sensor_tx_power: int = 10
    gateway_tx_power: int = 14

    async_channel: int = channel(26)  # 868.3 MHz: pings, ACKs, MP_START and its accept
    channel_pool: list[int] = field(default_factory=lambda: [channel(i) for i in range(7)])

    # Routine exchange.
    ack_timeout: float = 0.200
    max_attempts: int = 5
    retry_backoff: float = 0.100  # times the attempt number
    retry_jitter: float = 0.100
    gateway_ack_delay: float = 0.060  # after the end of the uplink

    # Clear channel assessment before every sensor transmission.
    cca_retries: int = 5
    cca_defer_min: float = 0.001
    cca_defer_max: float = 0.050
    cca_duration: float = 0.002  # ASSUMPTION: 1 ms settle plus 8 RSSI reads

    # Multi-packet download. `burst_packets` overrides how many fragments go into one burst;
    # when left empty it is as many full packets as fit in `burst_duration`.
    burst_duration: float = 1.0
    burst_packets: int | None = None
    packet_gap: float = 0.020
    answer_wait: float = 1.5
    highest_asks: int = 3
    channel_dwell: float = 100.0
    empty_windows: int = 3
    accept_delay: float = 0.010  # ASSUMPTION: radio session close and reopen before the accept
    gateway_turnaround: float = 0.060  # ASSUMPTION: from the sensor's answer to the next burst

    # Where the sensors are, the gateway being at the origin. Without positions every sensor has
    # the same fixed path loss; with them the loss follows the distance.
    positions: list[tuple[float, float]] | None = None
    path_loss_exponent: float = 3.2

    # Shares of the frames that are not heard at all, to exercise the repair paths: downlink
    # frames at the sensor they are for, uplink frames at the gateway.
    downlink_loss: float = 0.0
    uplink_loss: float = 0.0

    def slot_start(self, net_id: int, late: bool = False) -> float:
        """
            Offset of a sensor's ping in every interval: its static slot, or the FUOTA moment it
            is moved to for an update. Sensors are staggered by one slot there too: the sensor
            updated an hour earlier still pings there once, to be moved back.
        """
        base = self.fuota_offset if late else self.ping_lead
        return base + (net_id - 1) * self.ping_slot

    def airtime(self, payload_len: int) -> float:
        return estimate_airtime(
            payload_len, self.bandwidth, self.spreading_factor, self.code_rate,
            preamble_len=self.preamble_len,
        )

    @property
    def full_packet_airtime(self) -> float:
        return self.airtime(frames.MAX_FRAME_LEN)

    @property
    def fuota_window(self) -> float:
        """ Answer wait + burst + one full packet (whole milliseconds, rounded up) + margin. """
        burst = max(self.burst_duration, self.burst_time(self.packets_per_burst))
        return 1.5 + burst + math.ceil(self.full_packet_airtime * 1000) / 1000 + 0.5

    @property
    def total_packets(self) -> int:
        return math.ceil(self.image_bytes / frames.MP_MAX_DATA)

    def fragment_data_len(self, packet_id: int) -> int:
        if packet_id < self.total_packets - 1:
            return frames.MP_MAX_DATA
        return self.image_bytes - frames.MP_MAX_DATA * (self.total_packets - 1)

    @property
    def packets_per_burst(self) -> int:
        """ The gateway budgets every packet of a burst at the airtime of a full one. """
        if self.burst_packets is not None:
            return self.burst_packets
        air = self.full_packet_airtime
        return max(1, int((self.burst_duration + self.packet_gap) // (air + self.packet_gap)))

    def burst_time(self, packets: int) -> float:
        """ What one burst adds to the 100 s channel dwell count on both sides. """
        return packets * self.full_packet_airtime + (packets - 1) * self.packet_gap

    def position(self, net_id: int) -> tuple[float, float]:
        if self.positions is None:
            return (100.0, 0.0)
        return self.positions[net_id - 1]

    def setup_phy(self) -> None:
        if self.positions is None:
            # A fixed, comfortable link budget: losses are injected with the loss options.
            LoraPhyLayer(path_loss=constant_path_loss(100.0))
        else:
            LoraPhyLayer(path_loss=log_distance_path_loss(self.path_loss_exponent, sigma=0.0))


class RadioLog:
    """
        What one device's radio did, one row per event, for comparison with a capture of the real
        system. `time_s` is the start of the event and `end_s` its end: a real receiver usually
        timestamps a frame at its end.

        Events: `tx` and `rx` are frames, `rx_window` is the receiver being on (sensors only) and
        `cca` a clear channel assessment (sensors only).
    """
    COLUMNS = ["time_s", "end_s", "channel_mhz", "event", "bytes", "frame", "duration_ms"]

    def __init__(self, device: str):
        self.device = device
        self.rows: list[tuple[float, list]] = []

    def add(self, event: str, start: float, frequency: int, size: int | str = "",
            frame: str = "", end: float | None = None) -> None:
        end = sim.current_time() if end is None else end
        self.rows.append((start, [
            f"{start:.6f}", f"{end:.6f}", f"{frequency / 1e6:.1f}", event, size, frame,
            f"{(end - start) * 1000:.3f}",
        ]))

    def write(self, directory: Path) -> None:
        with open(directory / f"{self.device}.csv", "w", newline="") as file:
            writer = csv.writer(file)
            writer.writerow(self.COLUMNS)
            writer.writerows(row for (_, row) in sorted(self.rows, key=lambda entry: entry[0]))
