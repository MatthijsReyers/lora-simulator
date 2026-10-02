#!/usr/bin/env python3
"""
A complete LoRaWAN FUOTA campaign: TS003 clock sync, TS005 multicast setup, TS004 fragments.

    pipenv run python examples/lorawan_fuota/main.py
    pipenv run python examples/lorawan_fuota/main.py --devices 20 --image-size 16384 --plots
    pipenv run python examples/lorawan_fuota/main.py --loss 0.2
    pipenv run python examples/lorawan_fuota/main.py --class-b

Run from the repository root. See README.md for the flow, the flags and the caveats.
"""

from __future__ import annotations

import argparse
import logging
import math
import os
import random
import sys
import time

sys.path.append(".")
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from simulator.environment import simulation_env as sim
from simulator.lora.airtime import estimate_airtime
from simulator.lora.distance import distance
from simulator.lora.phy_layer import LoraPhyLayer
from simulator.lorawan.device import DeviceSession, LoRaWanDevice
from simulator.lorawan.fuota.campaign import (
    FRAGMENT_FRAME_OVERHEAD,
    FuotaCampaign,
    FuotaCampaignConfig,
    FuotaCampaignState,
)
from simulator.lorawan.fuota.device_stack import FuotaDeviceStack
from simulator.lorawan.fuota.frag_transport import (
    DataFragment,
    FragmentationDeviceApplication,
    FragmentationServerApplication,
)
from simulator.lorawan.gateway import LoRaWanGateway
from simulator.lorawan.network_server import NetworkServer
from simulator.lorawan.region import BEACON_INTERVAL, EU868_DATA_RATES
from simulator.path_loss.log_distance_path_loss import log_distance_path_loss

logger = logging.getLogger("fuota.example")


# ── Credentials and addressing ──────────────────────────────────────────────
#: ABP session keys. Every device shares them here purely to keep the example short; the
#: FUOTA key material (McKEKey, DataBlockIntKey) *is* per device, derived from GenAppKey.
NWK_S_KEY = bytes.fromhex("2B7E151628AED2A6ABF7158809CF4F3C")
APP_S_KEY = bytes.fromhex("3C4F9C098815F7ABA6D2AE281615E72B")
FIRST_DEV_ADDR = 0x26041000

#: TS005 multicast group address and McKey of this campaign.
MC_ADDR = 0xFF000010
MC_KEY = bytes.fromhex("A0A1A2A3A4A5A6A7A8A9AAABACADAEAF")

#: The LoRaWAN layer in this simulator is single channel: the gateway, the uplinks, RX1 and
#: the multicast session all live on this frequency. DLFreq is carried in McClassC/BSessionReq
#: and honoured by the device, so it must name the gateway's channel or nothing is heard.
DL_FREQUENCY = 868_100_000

#: Channel model: log-distance with a mild exponent, no shadowing, so a run is reproducible.
PATH_LOSS_EXPONENT = 2.7
NOISE_FLOOR = -123.0

#: TS005 Periodicity of the Class B session: 128 >> 2 = 32 ping slots per beacon period.
CLASS_B_PERIODICITY = 2

#: Where the plots land.
PLOT_DIR = os.path.dirname(os.path.abspath(__file__))


# ═══════════════════════════════════════════════════════════════════════════
# A device at the edge of coverage
# ═══════════════════════════════════════════════════════════════════════════

class LossyFragmentationDeviceApplication(FragmentationDeviceApplication):
    """A TS004 receiver that throws fragments away with a fixed, seeded probability.

    Models a node whose link margin is marginal without having to arrange the geometry for
    it, which keeps the loss pattern identical from run to run. The same technique the
    end-to-end tests use (``tests/test_lorawan_fuota_end_to_end.py``).
    """

    def __init__(self, *args, loss: float = 0.0, loss_seed: int = 0, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.loss = loss
        self.loss_rng = random.Random(loss_seed)
        self.fragments_lost_on_air = 0

    async def _handle_data_fragment(self, fragment: DataFragment) -> None:
        if self.loss_rng.random() < self.loss:
            self.fragments_lost_on_air += 1
            return
        await super()._handle_data_fragment(fragment)


def lossy_factory(loss: float, seed: int):
    """A ``fragmentation_factory`` for :class:`FuotaDeviceStack` with a seeded loss model."""

    def factory(device, **kwargs) -> FragmentationDeviceApplication:
        return LossyFragmentationDeviceApplication(
            device, loss=loss, loss_seed=seed, **kwargs
        )

    return factory


# ═══════════════════════════════════════════════════════════════════════════
# Scenario construction
# ═══════════════════════════════════════════════════════════════════════════

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0],
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--devices", type=int, default=10, help="number of end devices")
    parser.add_argument(
        "--image-size", type=int, default=8192, help="firmware image size in octets"
    )
    parser.add_argument(
        "--redundancy", type=float, default=0.2,
        help="coded (parity) fragments as a fraction of NbFrag",
    )
    parser.add_argument("--data-rate", type=int, default=5, help="EU868 data rate, 0-5")
    parser.add_argument(
        "--frag-size", type=int, default=None,
        help="FragSize in octets; the largest that fits one frame by default",
    )
    parser.add_argument(
        "--duty-cycle", type=float, default=0.10,
        help="gateway transmit duty cycle as a fraction; 0 disables the limiter",
    )
    parser.add_argument(
        "--loss", type=float, default=0.0,
        help="per-device fragment loss probability (seeded), applied to every device",
    )
    parser.add_argument(
        "--repair-rounds", type=int, default=2, help="maximum TS004 repair rounds"
    )
    parser.add_argument(
        "--class-b", action="store_true",
        help="run a TS005 Class B multicast session (ping slots) instead of Class C",
    )
    parser.add_argument("--radius", type=float, default=600.0, help="placement radius in m")
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument(
        "--sim-length", type=float, default=None,
        help="simulation length in seconds; derived from the campaign by default",
    )
    parser.add_argument(
        "--uplink-interval", type=float, default=25.0,
        help="seconds between a device's periodic application uplinks",
    )
    parser.add_argument("--plots", action="store_true", help="write the PNG figures")
    parser.add_argument("--log-level", default="INFO")
    return parser.parse_args()


def make_firmware(size: int, rng: random.Random) -> bytes:
    """A deterministic pseudo-random firmware image of *size* octets."""
    return bytes(rng.randrange(256) for _ in range(size))


def place_devices(count: int, radius: float, rng: random.Random) -> list[tuple[float, float]]:
    """Scatter *count* devices uniformly over a disc around the gateway at the origin."""
    positions = []
    for _ in range(count):
        # sqrt keeps the density uniform over the disc instead of crowding the centre.
        r = radius * math.sqrt(rng.random())
        angle = rng.uniform(0.0, 2 * math.pi)
        positions.append((r * math.cos(angle), r * math.sin(angle)))
    return positions


def plan_broadcast(args: argparse.Namespace) -> dict[str, float]:
    """FragSize, fragment count, spacing and broadcast duration implied by the flags.

    The campaign works all of this out for itself; the example needs it up front to size
    the phase timeouts and the simulation length.
    """
    frag_size = (
        args.frag_size
        if args.frag_size is not None
        else FragmentationServerApplication.fragment_payload_size_for(args.data_rate)
    )
    dr = EU868_DATA_RATES[args.data_rate]
    airtime = estimate_airtime(
        payload_len=frag_size + FRAGMENT_FRAME_OVERHEAD,
        bandwidth=dr.bandwidth.to_khz(),
        spreading_factor=dr.spreading_factor.value,
        code_rate=5,
    )
    nb_frag = math.ceil(args.image_size / frag_size)
    planned = nb_frag + math.ceil(nb_frag * max(args.redundancy, 0.0))
    if args.class_b:
        ping_nb = 128 >> CLASS_B_PERIODICITY
        interval = 0.0
        broadcast = math.ceil(planned / ping_nb) * float(BEACON_INTERVAL)
    else:
        interval = (
            airtime / args.duty_cycle if args.duty_cycle > 0 else airtime
        ) + 0.1
        broadcast = planned * interval
    return {
        "frag_size": float(frag_size),
        "airtime": airtime,
        "nb_frag": float(nb_frag),
        "planned": float(planned),
        "interval": interval,
        "broadcast": broadcast,
    }


def build_config(args: argparse.Namespace) -> FuotaCampaignConfig:
    """The campaign parameters implied by the command line."""
    # Every unicast setup command is delivered in the RX1 window of a device's own uplink
    # and answered on the next one, so a unicast round trip costs up to two uplink
    # intervals. In the TR002 order the lead time before SessionTime has to hold two of
    # them — the session answer and the whole fragmentation session setup — so it is
    # sized at four intervals plus a margin; otherwise the last device in the fleet is
    # still waiting for FragSessionSetupReq when the window opens. A repair round only
    # repeats the session request, so its lead time is half that.
    lead_time = max(60.0, 4.0 * args.uplink_interval + 5.0)
    repair_lead_time = max(40.0, 2.0 * args.uplink_interval + 5.0)
    plan = plan_broadcast(args)
    frag_size = int(plan["frag_size"])
    # The campaign sizes every "wait for an answer" from the uplink interval and from the
    # session window it scheduled itself, so `status_timeout` is a plain timeout on the
    # devices again and is left at its default.
    if args.class_b:
        # TS005 §4.6: SessionTime is a multiple of one beacon period and TimeOut counts
        # beacon periods. The devices need beacon lock before it opens, which is why the
        # lead time is a couple of beacon periods rather than the Class C minute.
        return FuotaCampaignConfig(
            mc_addr=MC_ADDR,
            mc_key=MC_KEY,
            frag_size=frag_size,
            data_rate=args.data_rate,
            dl_frequency=DL_FREQUENCY,
            redundancy_ratio=args.redundancy,
            descriptor=0x00010002,
            class_b=True,
            class_b_periodicity=CLASS_B_PERIODICITY,
            device_uplink_interval=args.uplink_interval,
            session_lead_time=max(3 * BEACON_INTERVAL, lead_time),
            broadcast_offset=0.0,
            fragment_interval=0.0,
            setup_timeout=max(180.0, 3.0 * args.uplink_interval),
            cleanup_timeout=max(120.0, 3.0 * args.uplink_interval),
            broadcast_settle=2.0,
            max_repair_rounds=args.repair_rounds,
            repair_lead_time=max(2 * BEACON_INTERVAL, repair_lead_time),
            repair_extra_fragments=2,
        )
    return FuotaCampaignConfig(
        mc_addr=MC_ADDR,
        mc_key=MC_KEY,
        frag_size=frag_size,
        data_rate=args.data_rate,
        dl_frequency=DL_FREQUENCY,
        redundancy_ratio=args.redundancy,
        descriptor=0x00010002,
        # None: the campaign derives the fragment spacing from the gateway's duty cycle.
        fragment_interval=None,
        session_lead_time=lead_time,
        device_uplink_interval=args.uplink_interval,
        setup_timeout=max(120.0, 3.0 * args.uplink_interval),
        cleanup_timeout=max(90.0, 3.0 * args.uplink_interval),
        broadcast_settle=2.0,
        max_repair_rounds=args.repair_rounds,
        repair_lead_time=repair_lead_time,
        repair_extra_fragments=2,
    )


def estimate_class_b_window(
    args: argparse.Namespace, config: FuotaCampaignConfig
) -> tuple[float, float]:
    """When the devices should hold Class B, for a Class B campaign.

    ``FuotaDeviceStack`` takes fixed times, so the example has to predict ``SessionTime``:
    the campaign queues ``McClassBSessionReq`` once the multicast group setup is behind it
    (TR002 order: the fragmentation session is set up *after* the rendezvous, inside the
    lead time) — roughly two uplink intervals, one round trip — and TS005 §4.6 then rounds
    ``now + session_lead_time`` up to a whole beacon period.

    The devices must *not* already be in Class B while that request is being delivered: a
    Class B device spends its time waiting for beacons and ping slots, which makes the RX1
    window after an uplink a far less reliable place to put a unicast command. They switch
    one and a half beacon periods before ``SessionTime`` instead, which still guarantees a
    beacon — and therefore ping-slot lock — before the session opens, and drop back to
    Class A once the window has closed so the status round runs over plain RX1 again.
    """
    plan = plan_broadcast(args)
    setup_done = 2.0 * args.uplink_interval + 2.0
    session_time = (
        math.ceil((setup_done + config.session_lead_time) / BEACON_INTERVAL)
        * BEACON_INTERVAL
    )
    # TimeOut counts beacon periods: 128 * 2**k, the smallest that covers the broadcast.
    window = float(BEACON_INTERVAL)
    while window < plan["broadcast"]:
        window *= 2
    start = session_time - 1.5 * BEACON_INTERVAL
    return start, session_time + window + 2.0 * args.uplink_interval


def build_scenario(args: argparse.Namespace):
    """Network server, gateway, campaign and N device stacks, started but not yet run."""
    rng = random.Random(args.seed)

    phy = LoraPhyLayer(
        path_loss=log_distance_path_loss(exponent=PATH_LOSS_EXPONENT, sigma=0.0),
        noise_floor=NOISE_FLOOR,
    )
    phy.logger.setLevel(logging.WARNING)

    firmware = make_firmware(args.image_size, rng)
    config = build_config(args)

    addrs = [FIRST_DEV_ADDR + index for index in range(args.devices)]
    gen_app_keys = {
        addr: bytes([(0x40 + index) & 0xFF]) * 16 for index, addr in enumerate(addrs)
    }

    ns = NetworkServer(default_data_rate=args.data_rate)
    for addr in addrs:
        ns.register_device(addr, NWK_S_KEY, APP_S_KEY)

    gateway = LoRaWanGateway(
        network_server=ns,
        data_rate=args.data_rate,
        duty_cycle=args.duty_cycle if args.duty_cycle > 0 else None,
        class_b_enabled=args.class_b,
    )
    gateway.radio.logger.setLevel(logging.WARNING)

    campaign = FuotaCampaign(
        ns,
        key_provider=gen_app_keys,
        firmware=firmware,
        config=config,
        gateway=gateway,
        rng=random.Random(args.seed + 1),
    )

    sim_length = (
        args.sim_length
        if args.sim_length is not None
        else estimate_sim_length(args, config)
    )

    class_b_from, class_b_until = (
        estimate_class_b_window(args, config) if args.class_b else (None, None)
    )

    positions = place_devices(args.devices, args.radius, rng)
    stacks: list[FuotaDeviceStack] = []
    for index, addr in enumerate(addrs):
        device = LoRaWanDevice(
            session=DeviceSession(
                dev_addr=addr, nwk_s_key=NWK_S_KEY, app_s_key=APP_S_KEY
            ),
            data_rate=args.data_rate,
        )
        device.radio.position = positions[index]
        device.radio.logger.setLevel(logging.WARNING)
        stack = FuotaDeviceStack(
            device,
            gen_app_key=gen_app_keys[addr],
            uplink_interval=args.uplink_interval,
            # Stagger the fleet. The gateway keeps receiving while it waits out a device's
            # RECEIVE_DELAY1, so nothing is lost without it — but it is a single
            # transceiver, and two RX1 windows that fall on top of each other cost one of
            # the two replies, which then has to be retransmitted on the device's next
            # uplink. One full interval divided by the fleet size gives every device its
            # own slot and keeps the campaign at one round trip per phase.
            first_uplink=1.0 + index * (args.uplink_interval / max(args.devices, 1)),
            stop_after=sim_length - 8.0,
            class_b_from=class_b_from,
            class_b_until=class_b_until,
            rng=random.Random(1000 + index),
            fragmentation_factory=(
                lossy_factory(args.loss, args.seed * 100 + index)
                if args.loss > 0
                else None
            ),
        )
        stack.start()
        stacks.append(stack)

    campaign.start()
    return ns, gateway, campaign, stacks, positions, firmware, sim_length


def estimate_sim_length(args: argparse.Namespace, config: FuotaCampaignConfig) -> float:
    """A simulation long enough for the whole campaign plus its repair rounds."""
    plan = plan_broadcast(args)
    broadcast = plan["broadcast"]
    # The TS005 session window is the next power of two above the broadcast (counted in
    # beacon periods for Class B), and a Class C device stays silent for all of it.
    unit = float(BEACON_INTERVAL) if args.class_b else 1.0
    window = unit
    while window < broadcast + 1.0:
        window *= 2
    # Setup: three unicast round trips (group, fragmentation session, multicast session),
    # roughly one uplink interval each, plus the session lead time.
    setup = config.session_lead_time + 6.0 * args.uplink_interval + 60.0
    # The status phase only starts counting its timeout once the session window has closed
    # and the devices have had a couple of uplink opportunities (see
    # ``FuotaCampaign.answers_possible_at``), and it may poll a silent device again.
    status = (
        config.max_status_rounds * config.status_timeout
        + 2.0 * args.uplink_interval
        + config.cleanup_timeout
    )
    # A repair round only sends a handful of fragments, so its own window is small.
    repair = args.repair_rounds * (
        config.session_lead_time + 4.0 * unit + 6.0 * args.uplink_interval + 120.0
    )
    return math.ceil(setup + window + status + repair + 60.0)


# ═══════════════════════════════════════════════════════════════════════════
# Reporting
# ═══════════════════════════════════════════════════════════════════════════

def print_banner(args: argparse.Namespace, campaign: FuotaCampaign, sim_length: float) -> None:
    cfg = campaign.config
    nb_frag = math.ceil(args.image_size / campaign.frag_size)
    airtime = campaign.fragment_frame_airtime()
    print("=" * 78)
    print("LoRaWAN FUOTA campaign  —  TS003 clock sync + TS005 multicast + TS004 fragments")
    print("=" * 78)
    print(f"  Devices:            {args.devices} within {args.radius:.0f} m of the gateway")
    print(f"  Firmware image:     {args.image_size} octets")
    print(f"  FragSize:           {campaign.frag_size} octets  ->  NbFrag = {nb_frag}")
    print(f"  Redundancy:         {args.redundancy:.0%}  ({math.ceil(nb_frag * args.redundancy)} coded fragments)")
    print(f"  Fragment frame:     {campaign.frag_size + 16} octets, {airtime * 1000:.1f} ms on air")
    print(f"  Data rate:          DR{cfg.data_rate} on {DL_FREQUENCY / 1e6:.1f} MHz")
    print(f"  Session:            {'Class B (ping slots)' if cfg.class_b else 'Class C (continuous RX)'}")
    if cfg.class_b:
        print(f"  Ping slots:         {campaign.ping_nb} per {BEACON_INTERVAL:.0f}s beacon period")
    else:
        print(f"  Fragment interval:  {campaign.fragment_interval():.3f} s")
    print(f"  Gateway duty cycle: {f'{args.duty_cycle:.0%}' if args.duty_cycle > 0 else 'unlimited'}")
    print(f"  Fragment loss:      {args.loss:.0%} per device")
    print(f"  Repair rounds:      up to {args.repair_rounds}")
    print(f"  Uplink interval:    {args.uplink_interval:.1f} s per device")
    print(f"  Simulation length:  {sim_length:.0f} s  (seed {args.seed})")
    print("=" * 78)


def print_summary(
    args: argparse.Namespace,
    campaign: FuotaCampaign,
    gateway: LoRaWanGateway,
    stacks: list,
    positions: list[tuple[float, float]],
    firmware: bytes,
    wall_time: float,
) -> None:
    result = campaign.result

    print()
    print("=" * 78)
    print("Per device")
    print("=" * 78)
    header = (
        f"{'DevAddr':>10}  {'dist':>6}  {'frag rx':>7}  {'dropped':>7}  "
        f"{'uplinks':>7}  {'complete at':>11}  {'energy':>8}  {'image'}"
    )
    print(header)
    print("-" * 78)
    for index, stack in enumerate(stacks):
        metrics = stack.metrics()
        dist = distance(positions[index], (0.0, 0.0))
        completion = (
            f"{metrics.completion_time:10.1f}s"
            if metrics.completion_time is not None
            else f"{'never':>11}"
        )
        energy = (
            f"{metrics.energy_joules:7.2f}J"
            if metrics.energy_joules is not None
            else f"{'n/a':>8}"
        )
        ok = stack.received_images.get(0) == firmware
        print(
            f"0x{metrics.dev_addr:08X}  {dist:5.0f}m  {metrics.fragments_received:7d}  "
            f"{metrics.fragments_dropped:7d}  {metrics.uplinks_sent:7d}  {completion}  "
            f"{energy}  {'OK' if ok else 'INCOMPLETE'}"
        )

    print()
    print("=" * 78)
    print("Campaign totals")
    print("=" * 78)
    print(f"  Final state:          {result.state.value}")
    if campaign.failure_reason is not None:
        print(f"  Failure reason:       {campaign.failure_reason}")
    print(f"  Devices complete:     {result.completed}/{result.devices} "
          f"({result.participants} participant(s), {len(result.excluded)} excluded)")
    print(f"  NbFrag / FragSize:    {result.nb_frag} x {result.frag_size} octets")
    print(f"  Fragments uncoded:    {result.fragments_uncoded}")
    print(f"  Fragments coded:      {result.fragments_coded}")
    print(f"  Fragments repair:     {result.fragments_repair} "
          f"in {result.repair_rounds} repair round(s)")
    print(f"  Fragments scheduled:  {result.fragments_scheduled}")
    print(f"  Multicast frames:     {result.multicast_frames} "
          f"({'ping-slot scheduler keeps no log' if campaign.config.class_b else 'free-running scheduler'})")
    print(f"  Multicast airtime:    {result.multicast_airtime:.1f} s")
    print(f"  Duty-cycle quiet:     {result.duty_cycle_quiet_time:.1f} s")
    print(f"  Unicast downlinks:    {result.unicast_downlinks}")
    print(f"  Uplinks received:     {result.uplinks_received}")
    print(f"  Gateway forwarded:    {gateway.frames_forwarded} uplink frames")
    print()
    print("  Phase durations")
    for state in FuotaCampaignState:
        duration = result.phase_durations.get(state)
        if duration:
            print(f"    {state.value:<14} {duration:8.1f} s")
    print()
    last = result.last_completion_time
    print(f"  Campaign total time:  {result.total_time:.1f} s")
    print(f"  Last device complete: {last:.1f} s" if last is not None else
          "  Last device complete: never")
    goodput = (
        len(firmware) * result.completed / result.total_time if result.total_time else 0.0
    )
    print(f"  Delivered goodput:    {goodput:.0f} octets/s "
          f"({len(firmware) * result.completed / 1000:.1f} kB to {result.completed} device(s))")
    print(f"  Wall-clock time:      {wall_time:.1f} s")
    print(f"  Success:              {result.success}")
    print("=" * 78)


# ═══════════════════════════════════════════════════════════════════════════
# Entry point
# ═══════════════════════════════════════════════════════════════════════════

def main() -> int:
    args = parse_args()
    level = args.log_level.upper()
    logging.basicConfig(level=level, format="%(message)s", force=True)
    # The environment pins the whole ``simulator`` logger tree to WARNING on import, which
    # would hide the campaign's own phase logs; lift it and silence the per-frame chatter
    # of the device and network-server layers individually instead.
    logging.getLogger("simulator").setLevel(level)
    logging.getLogger("simulator.lorawan.device").setLevel(logging.WARNING)
    logging.getLogger("simulator.lorawan.gateway").setLevel(logging.WARNING)
    logging.getLogger("simulator.lorawan.network_server").setLevel(logging.WARNING)

    ns, gateway, campaign, stacks, positions, firmware, sim_length = build_scenario(args)
    print_banner(args, campaign, sim_length)

    wall_start = time.time()
    sim.run(simulation_length=sim_length)
    wall_time = time.time() - wall_start

    print_summary(args, campaign, gateway, stacks, positions, firmware, wall_time)

    if args.plots:
        from plots import plot_campaign

        paths = plot_campaign(
            campaign=campaign,
            gateway=gateway,
            stacks=stacks,
            sim_length=sim_length,
            directory=PLOT_DIR,
        )
        print()
        for path in paths:
            print(f"  Plot written: {path}")

    return 0 if campaign.result.success else 1


if __name__ == "__main__":
    sys.exit(main())
