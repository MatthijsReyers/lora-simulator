#!/usr/bin/env python3
"""
The Malumbres 2024 scenario, run over the *standard* LoRaWAN FUOTA stack instead of raw LoRa.

    pipenv run python examples/lorawan_fuota/compare_with_paper.py
    pipenv run python examples/lorawan_fuota/compare_with_paper.py --nodes 10 --radius 2000

``papers/fuota-unicast-broadcast-2024`` reproduces

    V. Malumbres, J. Saldana, G. Berné and J. Modrego, *Firmware Updates over the Air via
    LoRa: Unicast and Broadcast Combination for Boosting Update Speed*, Sensors 2024, 24,
    2104.  https://doi.org/10.3390/s24072104

which distributes a firmware image over bare LoRa with MiWi frames, bitmap-driven ARQ and no
LoRaWAN at all. This script sets up the same radio scenario — Table 1 of the paper — and runs
the TS003/TS005/TS004 campaign over it, printing the same headline metrics so the two can be
put side by side. Nothing is imported from ``papers/``: the parameters are restated here.

Parameters mirrored from Table 1 / Figure 6 of the paper
--------------------------------------------------------
10 nodes uniformly distributed in distance over a 400 m radius, one gateway at the origin,
100,000 octets of firmware, SF7 / 125 kHz / CR 4/5, 14 dBm, -125 dBm receiver sensitivity,
log-distance path loss with exponent 3.2 and Nakagami fading, 1% duty cycle.

What could *not* be mirrored, and why — see README.md for the long version:

- **Frame size.** The paper uses 215-octet MiWi frames carrying a 192-octet chunk. A LoRaWAN
  DataFragment is capped by the regional maximum application payload, so the LoRaWAN frame is
  ``FragSize + 3`` octets of TS004 header + 13 octets of LoRaWAN frame. ``FragSize`` is set to
  the paper's 192 octets, which fits at DR5, so the firmware is cut into the same 521
  fragments; the frames are a few octets longer than the paper's.
- **Duty cycle.** The paper applies the 1% quiet time to the whole *medium* after every frame,
  acknowledgements included. ``LoRaWanGateway``'s limiter is a per-transmitter budget, which is
  what the regulations actually require. For the broadcast stage — the part the two protocols
  share — the two models agree, because only the gateway transmits.
- **The repair mechanism itself.** The paper repairs with a per-node bitmap and retransmits
  the named chunks. TS004 repairs with additional *coded* fragments over the same multicast
  group and a ``FragSessionStatusAns`` carrying only ``MissingFrag``. They are different
  protocols; the comparison is of the resulting update time, not of the mechanism.
- **Checksum, flashing and reboot stages.** Sections 3.1(d)/(e) of the paper have no
  equivalent in TS004/TS005 — that is TS006 Firmware Management, which this baseline does not
  implement. The metric compared is therefore the paper's *binary exchange* time: the moment
  the last node holds the whole image.
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
from simulator.lora.enums.spreading_factor import SpreadingFactor
from simulator.lora.phy_layer import LoraPhyLayer
from simulator.lorawan.device import DeviceSession, LoRaWanDevice
from simulator.lorawan.fuota.campaign import (
    FRAGMENT_FRAME_OVERHEAD,
    FuotaCampaign,
    FuotaCampaignConfig,
)
from simulator.lorawan.fuota.device_stack import FuotaDeviceStack
from simulator.lorawan.gateway import LoRaWanGateway
from simulator.lorawan.network_server import NetworkServer
from simulator.lorawan.region import EU868_DATA_RATES
from simulator.path_loss.log_distance_path_loss import log_distance_path_loss
from simulator.path_loss.nakagami_path_loss import nakagami_path_loss

# ── Table 1 of the paper ────────────────────────────────────────────────────
PAPER_NODES = 10
PAPER_RADIUS = 400.0
PAPER_FIRMWARE_SIZE = 100_000       # octets; the paper's "100 kB" is 1000-octet kB
PAPER_CHUNK_SIZE = 192              # octets of firmware per frame
PAPER_DUTY_CYCLE = 0.01             # 1%
PAPER_SPREADING_FACTOR = 7          # SF7 / 125 kHz == EU868 DR5
PAPER_TX_POWER = 14                 # dBm
PAPER_SENSITIVITY = -125.0          # dBm
PAPER_PATH_LOSS_EXPONENT = 3.2      # calibrated in papers/.../README.md

#: Headline results of the paper (section 5.2), hours until the last node holds the binary.
PAPER_RESULTS_HOURS = {
    ("unicast only", 400): 49.1,
    ("broadcast + unicast", 400): 4.66,
    ("broadcast only", 400): 4.87,
    ("unicast only", 2000): 81.8,
    ("broadcast + unicast", 2000): 37.9,
    ("broadcast only", 2000): 18.6,
}

# ── LoRaWAN scenario constants ──────────────────────────────────────────────
NWK_S_KEY = bytes.fromhex("2B7E151628AED2A6ABF7158809CF4F3C")
APP_S_KEY = bytes.fromhex("3C4F9C098815F7ABA6D2AE281615E72B")
FIRST_DEV_ADDR = 0x26050000
MC_ADDR = 0xFF000020
MC_KEY = bytes.fromhex("B0B1B2B3B4B5B6B7B8B9BABBBCBDBEBF")
DL_FREQUENCY = 868_100_000
DATA_RATE = 5  # SF7 / 125 kHz


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0],
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--nodes", type=int, default=PAPER_NODES)
    parser.add_argument("--radius", type=float, default=PAPER_RADIUS)
    parser.add_argument(
        "--firmware-kb", type=float, default=PAPER_FIRMWARE_SIZE / 1000,
        help="firmware size in kB (1000 octets), like the paper's main.py",
    )
    parser.add_argument(
        "--frag-size", type=int, default=PAPER_CHUNK_SIZE,
        help="FragSize in octets; the paper's chunk size by default",
    )
    parser.add_argument(
        "--duty-cycle", type=float, default=PAPER_DUTY_CYCLE * 100,
        help="gateway duty cycle in percent, like the paper's main.py",
    )
    parser.add_argument(
        "--redundancy", type=float, default=0.05,
        help="TS004 coded fragments as a fraction of NbFrag; the paper has no FEC at all, "
             "so 0 is the closest analogue and anything above it is the TS004 advantage",
    )
    parser.add_argument("--repair-rounds", type=int, default=1)
    parser.add_argument("--exponent", type=float, default=PAPER_PATH_LOSS_EXPONENT)
    parser.add_argument(
        "--no-fading", action="store_true",
        help="drop the Nakagami fading and leave plain log-distance path loss",
    )
    parser.add_argument(
        "--uplink-interval", type=float, default=60.0,
        help="seconds between a node's periodic uplinks; the LoRaWAN setup phases are "
             "paced by it, the paper has no equivalent",
    )
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--log-level", default="WARNING")
    return parser.parse_args()


def node_positions(count: int, radius: float, rng: random.Random) -> list[tuple[float, float]]:
    """Figure 6 of the paper: uniform in *distance* over ``[0, radius]``, random bearing."""
    positions = []
    for _ in range(count):
        d = rng.uniform(0.0, radius)
        angle = rng.uniform(0.0, 2 * math.pi)
        positions.append((d * math.cos(angle), d * math.sin(angle)))
    return positions


def setup_phy(args: argparse.Namespace) -> LoraPhyLayer:
    """The paper's channel: log-distance with Nakagami fading, sensitivity -125 dBm."""
    base = log_distance_path_loss(exponent=args.exponent, sigma=0.0)
    sf = SpreadingFactor(PAPER_SPREADING_FACTOR)
    phy = LoraPhyLayer(
        path_loss=base if args.no_fading else nakagami_path_loss(base=base),
        noise_floor=PAPER_SENSITIVITY - sf.minimum_snr(),
    )
    phy.logger.setLevel(logging.WARNING)
    return phy


def fragment_airtime(frag_size: int) -> float:
    dr = EU868_DATA_RATES[DATA_RATE]
    return estimate_airtime(
        payload_len=frag_size + FRAGMENT_FRAME_OVERHEAD,
        bandwidth=dr.bandwidth.to_khz(),
        spreading_factor=dr.spreading_factor.value,
        code_rate=5,
    )


def build(args: argparse.Namespace):
    firmware_size = int(args.firmware_kb * 1000)
    duty_cycle = args.duty_cycle / 100
    frag_size = args.frag_size

    nb_frag = math.ceil(firmware_size / frag_size)
    planned = nb_frag + math.ceil(nb_frag * args.redundancy)
    airtime = fragment_airtime(frag_size)
    interval = airtime / duty_cycle + 0.1
    broadcast = planned * interval
    lead_time = max(60.0, 2.0 * args.uplink_interval)
    # The TS005 session window is the next power of two above the broadcast, and a Class C
    # device stays silent for all of it; the status round cannot be answered before it ends.
    window = 1.0
    while window < broadcast + 1.0:
        window *= 2
    status_timeout = (window - broadcast) + 4.0 * args.uplink_interval + 120.0

    config = FuotaCampaignConfig(
        mc_addr=MC_ADDR,
        mc_key=MC_KEY,
        frag_size=frag_size,
        data_rate=DATA_RATE,
        dl_frequency=DL_FREQUENCY,
        redundancy_ratio=args.redundancy,
        descriptor=0x00010002,
        fragment_interval=None,  # derived from the gateway's duty cycle
        session_lead_time=lead_time,
        repair_lead_time=lead_time,
        setup_timeout=max(180.0, 4.0 * args.uplink_interval),
        status_timeout=status_timeout,
        cleanup_timeout=max(180.0, 4.0 * args.uplink_interval),
        broadcast_settle=2.0,
        max_repair_rounds=args.repair_rounds,
        repair_extra_fragments=2,
    )

    rng = random.Random(args.seed)
    random.seed(args.seed)  # the Nakagami model draws from the global generator
    setup_phy(args)

    firmware = bytes(rng.randrange(256) for _ in range(firmware_size))
    addrs = [FIRST_DEV_ADDR + index for index in range(args.nodes)]
    gen_app_keys = {
        addr: bytes([(0x10 + index) & 0xFF]) * 16 for index, addr in enumerate(addrs)
    }

    ns = NetworkServer(default_data_rate=DATA_RATE)
    for addr in addrs:
        ns.register_device(addr, NWK_S_KEY, APP_S_KEY)

    gateway = LoRaWanGateway(
        network_server=ns, data_rate=DATA_RATE, tx_power=PAPER_TX_POWER,
        duty_cycle=duty_cycle,
    )
    gateway.radio.logger.setLevel(logging.WARNING)

    campaign = FuotaCampaign(
        ns, key_provider=gen_app_keys, firmware=firmware, config=config, gateway=gateway,
        rng=random.Random(args.seed + 1),
    )

    # Setup (three unicast round trips), the lead time, the whole TS005 session window, the
    # status round that can only be answered once that window closed, and the cleanup. A
    # repair round sends a handful of fragments, so its own window is small; budget one
    # beacon-scale window plus another status round for each.
    repair_allowance = args.repair_rounds * (
        lead_time + 1024.0 + 6.0 * args.uplink_interval + 600.0
    )
    sim_length = math.ceil(
        6.0 * args.uplink_interval
        + lead_time
        + window
        + status_timeout
        + config.cleanup_timeout
        + repair_allowance
        + 600.0
    )

    positions = node_positions(args.nodes, args.radius, rng)
    stacks = []
    for index, addr in enumerate(addrs):
        device = LoRaWanDevice(
            session=DeviceSession(
                dev_addr=addr, nwk_s_key=NWK_S_KEY, app_s_key=APP_S_KEY
            ),
            data_rate=DATA_RATE,
            tx_power=PAPER_TX_POWER,
        )
        device.radio.position = positions[index]
        device.radio.log_packets = False
        device.radio.logger.setLevel(logging.WARNING)
        stack = FuotaDeviceStack(
            device,
            gen_app_key=gen_app_keys[addr],
            uplink_interval=args.uplink_interval,
            first_uplink=1.0 + index * (args.uplink_interval / max(args.nodes, 1)),
            stop_after=sim_length - 10.0,
            rng=random.Random(2000 + index),
        )
        stack.start()
        stacks.append(stack)

    campaign.start()
    return {
        "ns": ns,
        "gateway": gateway,
        "campaign": campaign,
        "stacks": stacks,
        "positions": positions,
        "firmware": firmware,
        "sim_length": sim_length,
        "nb_frag": nb_frag,
        "planned": planned,
        "airtime": airtime,
        "interval": interval,
        "broadcast": broadcast,
        "window": window,
    }


def main() -> int:
    args = parse_args()
    level = args.log_level.upper()
    logging.basicConfig(level=level, format="%(message)s", force=True)
    logging.getLogger("simulator").setLevel(level)
    logging.getLogger("simulator.lorawan.device").setLevel(logging.WARNING)
    logging.getLogger("simulator.lorawan.gateway").setLevel(logging.WARNING)
    logging.getLogger("simulator.lorawan.network_server").setLevel(logging.WARNING)

    scenario = build(args)
    campaign = scenario["campaign"]
    gateway = scenario["gateway"]
    stacks = scenario["stacks"]

    print("=" * 84)
    print("Malumbres 2024 scenario over the standard LoRaWAN FUOTA stack (TS003/TS005/TS004)")
    print("=" * 84)
    print(f"  Nodes:              {args.nodes} uniformly in distance within {args.radius:.0f} m")
    print(f"  Firmware:           {int(args.firmware_kb * 1000)} octets")
    print(f"  FragSize:           {args.frag_size} octets (paper chunk: {PAPER_CHUNK_SIZE})")
    print(f"  NbFrag:             {scenario['nb_frag']} "
          f"(+{scenario['planned'] - scenario['nb_frag']} coded, {args.redundancy:.0%})")
    print(f"  Fragment frame:     {args.frag_size + FRAGMENT_FRAME_OVERHEAD} octets, "
          f"{scenario['airtime'] * 1000:.1f} ms on air at SF{PAPER_SPREADING_FACTOR}")
    print(f"  Duty cycle:         {args.duty_cycle:.2f}% -> one fragment every "
          f"{scenario['interval']:.1f} s")
    print(f"  Channel:            log-distance n={args.exponent}"
          f"{'' if args.no_fading else ' + Nakagami fading'}, "
          f"sensitivity {PAPER_SENSITIVITY:.0f} dBm")
    print(f"  Repair rounds:      up to {args.repair_rounds}")
    print(f"  Simulation length:  {scenario['sim_length']} s (seed {args.seed})")
    print("=" * 84)

    wall_start = time.time()
    sim.run(simulation_length=scenario["sim_length"])
    wall_time = time.time() - wall_start

    result = campaign.result
    firmware = scenario["firmware"]

    # ---- Per node, in the shape of the paper's *_nodes.csv ----
    print()
    print(f"{'node':>4} {'distance':>9} {'binary_start':>13} {'binary_end':>11} "
          f"{'binary_time':>12} {'frames_rx':>10} {'uplinks':>8} {'updated':>8}")
    broadcast_start = min(
        (time for (time, state) in campaign.state_history
         if state.value == "BROADCAST"),
        default=0.0,
    )
    for index, stack in enumerate(stacks):
        metrics = stack.metrics()
        d = distance(scenario["positions"][index], (0.0, 0.0))
        end = metrics.completion_time
        print(
            f"{index + 1:>4} {d:8.1f}m {broadcast_start:12.1f}s "
            f"{(f'{end:.1f}s' if end is not None else 'never'):>11} "
            f"{(f'{end - broadcast_start:.1f}s' if end is not None else '-'):>12} "
            f"{metrics.fragments_received:>10} {metrics.uplinks_sent:>8} "
            f"{str(stack.received_images.get(0) == firmware):>8}"
        )

    # ---- Totals, in the shape of the paper's summary line ----
    broadcast_time = result.phase_durations.get(
        next(s for s in result.phase_durations if s.value == "BROADCAST"), 0.0
    ) if result.phase_durations else 0.0
    # The paper's metric is the moment the last *node* holds the whole binary, which the
    # devices know long before the network server does: a Class C device cannot transmit its
    # FragDataBlockReceivedReq until the TS005 session window closes.
    device_times = [s.completion_time for s in stacks if s.completion_time is not None]
    binary_total = max(device_times) if device_times else float("nan")
    reported = result.last_completion_time

    print()
    print("=" * 84)
    print("Headline metrics")
    print("=" * 84)
    print(f"  Last node holds the binary at    {binary_total:.0f} s "
          f"({binary_total / 3600:.2f} h)   <- the paper's metric")
    print(f"  Server learns of it at           "
          f"{reported:.0f} s ({reported / 3600:.2f} h)"
          if reported is not None else
          "  Server learns of it at           never")
    print(f"  of which the broadcast stage     {broadcast_time:.0f} s "
          f"({broadcast_time / 3600:.2f} h)")
    print(f"  Whole campaign (incl. cleanup)   {result.total_time:.0f} s "
          f"({result.total_time / 3600:.2f} h)")
    print(f"  Nodes updated                    {result.completed}/{result.devices}")
    print(f"  Multicast frames                 {result.multicast_frames}")
    print(f"  Multicast airtime                {result.multicast_airtime:.0f} s")
    print(f"  Duty-cycle quiet time            {result.duty_cycle_quiet_time:.0f} s")
    print(f"  Unicast downlinks                {result.unicast_downlinks}")
    print(f"  Uplinks received                 {result.uplinks_received}")
    print(f"  Gateway uplink frames forwarded  {gateway.frames_forwarded}")
    print(f"  Repair rounds                    {result.repair_rounds} "
          f"({result.fragments_repair} fragment(s))")
    print(f"  Wall-clock time                  {wall_time:.1f} s")
    print()
    print("  For reference, the paper's own single runs at this radius (section 5.2):")
    for (method, radius), hours in PAPER_RESULTS_HOURS.items():
        if abs(radius - args.radius) < 1.0:
            print(f"    {method:<22} {hours:>6.2f} h")
    print("  The paper's figures cover the binary exchange only, not checksum/flash/reboot.")
    print("=" * 84)

    return 0 if result.success else 1


if __name__ == "__main__":
    sys.exit(main())
