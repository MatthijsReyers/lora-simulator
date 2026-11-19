#!/usr/bin/env python3
"""
Runs one FUOTA session of the sequential spreading factor scheme on the LoRaWAN stack and
writes the results to `data/`.

    pipenv run python papers/fuota-sequential-sf-2025-lorawan/main.py --nodes 10 --radius 250

Run from the repository root. See README.md for the parameters and the output format.
"""
from __future__ import annotations

import argparse
import logging
import math
import os
import random
import sys
import time
from dataclasses import asdict
from pathlib import Path

sys.path.append(".")  # the simulator package, when running from the repository root
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

import pandas as pd

from simulator.environment import simulation_env as sim
from simulator.lora.phy_layer import sensitivity_from_sf7
from simulator.lora.radio_usage import update_cost
from simulator.lorawan.device import DeviceSession
from simulator.lorawan.gateway import LoRaWanGateway
from simulator.lorawan.network_server import NetworkServer

from campaign import SequentialSfCampaign, SequentialSfConfig
from ladder import FRAGMENT_FRAME_OVERHEAD, data_rate_of, spreading_factor_of
from recipient import Recipient
from scenario import FREQUENCY, Scenario, read_layout, write_layout

# ABP session keys, the same for every device to keep this short. The FUOTA keys (McKEKey,
# DataBlockIntKey) are per device, derived from its GenAppKey.
NWK_S_KEY = bytes.fromhex("2B7E151628AED2A6ABF7158809CF4F3C")
APP_S_KEY = bytes.fromhex("3C4F9C098815F7ABA6D2AE281615E72B")
FIRST_DEV_ADDR = 0x26041000
MC_ADDR = 0xFF000010
MC_KEY = bytes.fromhex("A0A1A2A3A4A5A6A7A8A9AAABACADAEAF")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    defaults = Scenario()
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("-L", "--first-sf", type=int, default=defaults.first_sf, help="L, smallest SF of the ladder")
    parser.add_argument("-M", "--last-sf", type=int, default=defaults.last_sf, help="M, largest SF of the ladder")
    parser.add_argument("-w", "--transmissions-per-sf", type=int, default=None, help="w, frames per SF (default: 1.1 times the number of fragments)")
    parser.add_argument("--nodes", type=int, default=defaults.nodes)
    parser.add_argument("--radius", type=float, default=defaults.radius, help="R0 in meters")
    parser.add_argument("--layout", choices=["radial", "uniform"], default=defaults.layout)
    parser.add_argument("--layout-file", type=Path, default=None, help="read the node positions from this CSV instead of placing them")
    parser.add_argument("--firmware-kb", type=float, default=defaults.firmware_size / 1000, help="firmware size in kB (1000 bytes)")
    parser.add_argument("--fragment-size", type=int, default=None, help="FragSize in bytes (default: the largest that fits a frame with the last SF)")
    parser.add_argument("--duty-cycle", type=float, default=defaults.duty_cycle_percent, help="gateway duty cycle in percent")
    parser.add_argument("--listen", choices=["slotted", "continuous"], default=defaults.listen, help="a receive window per frame, or Class C for the session")
    parser.add_argument("--uplink-interval", type=float, default=defaults.uplink_interval, help="seconds between a device's own uplinks, which pace the setup")
    parser.add_argument("--uplink-sf", default="7", help="SF of the devices' uplinks, or 'auto' for one per device by its link budget")
    parser.add_argument("--setup-attempts", type=int, default=None, help="how often a setup command can be tried on a device; every attempt lengthens the setup phases by an uplink interval")
    parser.add_argument("--block-ack-delay", type=int, default=defaults.block_ack_delay, help="TS004 BlockAckDelay: acknowledgements are spread over 2^(4 + this) seconds")
    parser.add_argument("--exponent", type=float, default=defaults.path_loss_exponent, help="path loss exponent α")
    parser.add_argument("--reference-loss", type=float, default=defaults.reference_loss_db, help="path loss at 1 m in dB")
    parser.add_argument("--no-fading", action="store_true", help="disable the Rayleigh fading")
    parser.add_argument("--sensitivity", type=float, default=None, help="receiver sensitivity in dBm at SF7 for every radio, 2.5 dB better per spreading factor up; the paper's table by default")
    parser.add_argument("--shadowing", type=float, default=0.0, help="standard deviation in dB of a log-normal shadowing per frame")
    parser.add_argument("--frame-loss", type=float, default=0.0, help="share of the frames a receiver does not hear at all, on top of the channel model; the loss of the protocol comparison")
    parser.add_argument("--max-hours", type=float, default=None, help="the gateway gives up after this many hours of transmitting (default: when the fragment index runs out)")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--output-dir", type=Path, default=Path(__file__).parent / "data")
    parser.add_argument("--tag", default="", help="extra text for the run name")
    parser.add_argument("--log-level", default="WARNING")
    return parser.parse_args(argv)


def scenario_from_args(args: argparse.Namespace) -> Scenario:
    return Scenario(
        first_sf=args.first_sf,
        last_sf=args.last_sf,
        transmissions_per_sf=args.transmissions_per_sf,
        duty_cycle_percent=args.duty_cycle,
        max_broadcast_time=args.max_hours * 3600 if args.max_hours is not None else None,
        firmware_size=int(round(args.firmware_kb * 1000)),
        fragment_size=args.fragment_size,
        nodes=args.nodes,
        radius=args.radius,
        layout=args.layout,
        listen=args.listen,
        uplink_interval=args.uplink_interval,
        uplink_sf=None if args.uplink_sf == "auto" else int(args.uplink_sf),
        setup_attempts=args.setup_attempts,
        block_ack_delay=args.block_ack_delay,
        path_loss_exponent=args.exponent,
        reference_loss_db=args.reference_loss,
        rayleigh_fading=not args.no_fading,
        shadowing_db=args.shadowing,
        **({"sensitivity": {int(sf): dbm for (sf, dbm) in sensitivity_from_sf7(args.sensitivity).items() if sf >= 7}}
           if args.sensitivity is not None else {}),
        seed=args.seed,
    )


def campaign_config(scenario: Scenario) -> SequentialSfConfig:
    """
        The timing of the setup phases, by the rules of `examples/lorawan_fuota`: a unicast
        command reaches a device in the RX1 window of one uplink and is answered with the
        next, and the lead time before the first frame has to hold two such exchanges per
        device, the session request and the fragmentation session setup.
    """
    interval = scenario.uplink_interval
    exchanges = 3.0
    lead_time = max(60.0, 4.0 * interval + 5.0)
    retries: dict[str, int] = {}
    if scenario.setup_attempts is not None:
        exchanges = scenario.setup_attempts + 1.0
        lead_time = max(60.0, 2.0 * exchanges * interval + 5.0)
        retries = {"command_retries": max(3, scenario.setup_attempts - 1)}
    return SequentialSfConfig(
        mc_addr=MC_ADDR,
        mc_key=MC_KEY,
        frag_size=scenario.fragment_size,
        dl_frequency=FREQUENCY,
        descriptor=0x00010002,
        block_ack_delay=scenario.block_ack_delay,
        session_lead_time=lead_time,
        device_uplink_interval=interval,
        setup_timeout=max(120.0, exchanges * interval),
        cleanup_timeout=max(90.0, exchanges * interval),
        broadcast_settle=2.0,
        spreading_factors=scenario.spreading_factors,
        frames_per_sf=scenario.transmissions_per_sf,
        duty_cycle=scenario.duty_cycle_percent / 100,
        max_broadcast_time=scenario.max_broadcast_time,
        **retries,
    )


def run_name(scenario: Scenario, campaign: SequentialSfCampaign, args: argparse.Namespace) -> str:
    s = scenario
    name = (
        f"L{s.first_sf}M{s.last_sf}w{campaign.rounds[0].frames}_f{campaign.frag_size}_{s.listen}"
        f"_n{s.nodes}_r{s.radius:.0f}_{s.layout[0]}_s{s.seed}"
    )
    return name + (f"_{args.tag}" if args.tag else "")


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    logging.basicConfig(level=args.log_level, format="%(levelname)s %(name)s: %(message)s", force=True)
    for name in ("simulator", "fuota"):
        logging.getLogger(name).setLevel(args.log_level)
    scenario = scenario_from_args(args)
    rng = random.Random(scenario.seed)
    random.seed(scenario.seed)  # the channel model (fading) draws from the global generator

    scenario.setup_phy(frame_loss=args.frame_loss)
    positions = read_layout(args.layout_file) if args.layout_file else scenario.node_positions(rng)
    scenario.nodes = len(positions)
    image = bytes(rng.randrange(256) for _ in range(scenario.firmware_size))

    addrs = [FIRST_DEV_ADDR + node for node in range(scenario.nodes)]
    gen_app_keys = {addr: bytes([(0x40 + node) & 0xFF]) * 16 for (node, addr) in enumerate(addrs)}
    uplink_sfs = [scenario.uplink_sf_for(math.hypot(*position)) for position in positions]

    ns = NetworkServer(default_data_rate=data_rate_of(7))
    for addr in addrs:
        ns.register_device(addr, NWK_S_KEY, APP_S_KEY)
    gateway = LoRaWanGateway(
        network_server=ns, data_rate=data_rate_of(7), tx_power=scenario.tx_power,
        duty_cycle=scenario.duty_cycle_percent / 100,
        uplink_data_rates=sorted({data_rate_of(sf) for sf in uplink_sfs}),
    )
    campaign = SequentialSfCampaign(
        ns, key_provider=gen_app_keys, image=image, config=campaign_config(scenario),
        gateway=gateway, rng=random.Random(scenario.seed + 1),
    )
    cfg = campaign.config

    # Long enough for the setup, the whole schedule and the cleanup. The simulation cannot
    # end early, so `--max-hours` is also what bounds the wall time of a run.
    broadcast = sum(r.duration for r in campaign.rounds)
    sim_length = math.ceil(
        scenario.uplink_interval + cfg.setup_timeout + cfg.session_lead_time + broadcast
        + cfg.cleanup_timeout + 2 * scenario.uplink_interval + 120.0
    )

    recipients = [
        Recipient(
            node, position,
            session=DeviceSession(dev_addr=addrs[node], nwk_s_key=NWK_S_KEY, app_s_key=APP_S_KEY),
            gen_app_key=gen_app_keys[addrs[node]],
            uplink_data_rate=data_rate_of(uplink_sfs[node]),
            tx_power=scenario.tx_power,
            listen=scenario.listen,
            uplink_interval=scenario.uplink_interval,
            # Every device its own slot within the interval, so that their RX1 replies do
            # not have to share the gateway's one transmitter.
            first_uplink=1.0 + node * scenario.uplink_interval / scenario.nodes,
            stop_after=sim_length - 8.0,
            rng=random.Random(1000 + node),
        )
        for (node, position) in enumerate(positions)
    ]
    campaign.start()

    name = run_name(scenario, campaign, args)
    ladder = ", ".join(
        f"SF{spreading_factor_of(r.data_rate)}: {r.frames} x {r.period:.1f} s" for r in campaign.rounds
    )
    print(
        f"{name}: {scenario.nodes} recipients within {scenario.radius:.0f} m, "
        f"{campaign.fragmentation_plan()}, frames of {campaign.frag_size + FRAGMENT_FRAME_OVERHEAD} B\n"
        f"  ladder: {ladder}\n"
        f"  first frame at most {(cfg.setup_timeout + cfg.session_lead_time) / 3600:.1f} h after "
        f"the start, simulating {sim_length / 3600:.1f} h"
    )
    wall_start = time.time()
    sim.run(simulation_length=sim_length)
    wall_time = time.time() - wall_start

    # For the protocol comparison: the cost from the uplink that brought a device its first
    # FUOTA command up to the moment it holds the verified image, or up to the end of the
    # simulation for one that never does. Its periodic uplinks before that are its normal
    # traffic.
    result = campaign.result
    session_start = campaign.schedule.start_time if campaign.schedule is not None else None
    rows = []
    for recipient in recipients:
        completed = recipient.completion_time
        received = recipient.image()
        updated = completed is not None and received is not None and received[:len(image)] == image
        metrics = recipient.stack.metrics()
        rows.append({
            "node": recipient.node,
            "distance": recipient.distance,
            "x": recipient.position[0],
            "y": recipient.position[1],
            "uplink_sf": uplink_sfs[recipient.node],
            "decoded": updated,
            "started_at": recipient.stack.fuota_started_at(),
            "completed_at": completed,
            "delivery_time": completed - session_start if updated and session_start is not None else None,
            "decode_sf": recipient.decode_sf,
            "acknowledged": addrs[recipient.node] in result.completion_time,
            "fragments_received": metrics.fragments_received,
            "windows_opened": recipient.device.windows_opened,
            "slots_missed": recipient.device.slots_missed,
            "uplinks_sent": metrics.uplinks_sent + metrics.package_uplinks,
            **update_cost(
                recipient.device.radio, start=recipient.stack.fuota_started_at() or 0.0,
                end=completed if updated and completed is not None else float(sim_length),
                updated=updated,
            ),
        })
    nodes = pd.DataFrame(rows)
    decoded = nodes[nodes["decoded"]]
    mean_dt = decoded["delivery_time"].mean() / 3600
    print(
        f"{len(decoded)}/{scenario.nodes} decoded, campaign {result.state.value} after "
        f"{result.total_time / 3600:.1f} h, first frame at "
        f"{(session_start or float('nan')) / 3600:.1f} h, frames {dict(sorted(campaign.frames_by_sf.items()))}, "
        f"{campaign.slots_skipped} slots skipped, mean DT {mean_dt:.2f} h, "
        f"mean energy {decoded['cmp_total_j'].mean():.2f} J, {wall_time:.0f} s wall time"
    )
    if campaign.failure_reason is not None:
        print(f"  {campaign.failure_reason}")

    args.output_dir.mkdir(parents=True, exist_ok=True)
    nodes.insert(0, "run", name)
    nodes.to_csv(args.output_dir / f"{name}_nodes.csv", index=False)
    if not args.layout_file:
        write_layout(args.output_dir / f"layout_n{scenario.nodes}_r{scenario.radius:.0f}_{scenario.layout}_s{scenario.seed}.csv", positions)
    summary: dict[str, object] = {"run": name}
    summary.update({k: v for (k, v) in asdict(scenario).items() if not isinstance(v, dict)})
    summary.update({
        "fragment_size_used": campaign.frag_size,
        "fragments": result.nb_frag,
        "padding_fragments": (len(campaign.firmware) - len(image)) // campaign.frag_size,
        "decoded": len(decoded),
        "campaign_state": result.state.value,
        "campaign_hours": result.total_time / 3600,
        "session_start_hours": session_start / 3600 if session_start is not None else None,
        "broadcast_end_hours": campaign.broadcast_ended_at / 3600 if campaign.broadcast_ended_at else None,
        "frames_sent": sum(campaign.frames_by_sf.values()),
        "slots_skipped": campaign.slots_skipped,
        "multicast_airtime": result.multicast_airtime,
        "unicast_downlinks": result.unicast_downlinks,
        "uplinks_received": result.uplinks_received,
        "mean_delivery_hours": mean_dt,
        "max_delivery_hours": decoded["delivery_time"].max() / 3600 if len(decoded) else float("nan"),
        "mean_energy_j": decoded["cmp_total_j"].mean(),
        "wall_time": wall_time,
    })
    pd.DataFrame([summary]).to_csv(args.output_dir / f"{name}_summary.csv", index=False)


if __name__ == "__main__":
    main()
