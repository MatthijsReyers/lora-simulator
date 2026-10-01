
## Example simulations

To demonstrate how to use the emulator and the various LoRa specific emulations tools provided by the simulator we have various example projects to demonstrate how to build simulation setups of increasing complexity. 
We recommend checking out the basic smaller examples first (listed at the top of the table below) and note that the more complex multi-file examples usually have their own README you can check out for more details.

| Name | File | Description |
| :--: | :--- | :---------- |
| Counters | `examples/counters.py` | Basic demonstration of how to run multiple tasks in the simulator simultaneously. |
| Child tasks | `examples/child_tasks` | Shows how to start new tasks/processes while the simulator is already running. |
| Queue | `examples/queue.py` | A basic demonstration to show how to use the Queue class provided by this simulator (note that you can *NOT* use a normal `asyncio.Queue` as the simulation might run for any amount of time while sending data between tasks).
| HTTP Request | `example/http_req.py` | Shows how to interleave real asynchronous work (like an HTTP request to a real LoRaWAN network server) safely within the simulator.
| LoRaWAN Clock Sync | `examples/lorawan_clock_sync/main.py` | A single LoRaWAN class A device running the TS003 clock synchronization application. |
| LoRaWAN FUOTA | `examples/lorawan_fuota/main.py` | A complete firmware-update-over-the-air campaign over the standard LoRa Alliance stack (TS003 + TS005 + TS004): multicast group setup, a fragmentation session, a class C (or class B) multicast broadcast of coded fragments, status rounds and repair rounds, with plots and a CLI. |
| LoRaWAN FUOTA vs. paper | `examples/lorawan_fuota/compare_with_paper.py` | The Malumbres 2024 scenario of `papers/fuota-unicast-broadcast-2024` re-run over the standard LoRaWAN FUOTA stack, printing the same headline metrics for a side-by-side comparison. |
| Multi-channel Ping Pong | `examples/multi-channel-ping-pong/main.py` | Three nodes ping a gateway on three different frequencies simultaneously, demonstrating carrier frequencies and multi-chain gateway radios. |
| Native Ping Pong | `examples/native_ping_pong/main.py` | Shows how to use the native C API of the simulator to run nodes with C firmware. |

## LoRaWAN FUOTA

`simulator/lorawan/fuota/` implements the LoRa Alliance firmware-update-over-the-air stack on
top of the simulator's LoRaWAN layer. It is a *standard-conformant* baseline: the wire formats,
key derivations and the forward-error-correction scheme all follow the published
specifications, with the section numbers cited in the docstrings.

| Module | Implements |
| :----- | :--------- |
| `crypto.py` | The FUOTA key hierarchy: `McRootKey`, `McKEKey`, `McKey` transport and the per-group `McAppSKey`/`McNwkSKey` (TS005 §4.2.6), plus `DataBlockIntKey` and the data-block MIC (TS004 §3.3). Both the LoRaWAN 1.0.x (`GenAppKey`) and 1.1 (`AppKey`) derivations. |
| `multicast_setup.py` | **TS005 Remote Multicast Setup v2.0.0** on FPort 200: codecs for `PackageVersion`, `McGroupStatus`, `McGroupSetup`, `McGroupDelete`, `McClassCSession` and `McClassBSession` (CIDs 0x00–0x05), plus the device-side and server-side `Application`s that drive the multicast contexts and schedule the class B/C sessions. |
| `fragmentation.py` | **TS004 Annex A** FragAlgo 0: the PRBS23-driven parity matrix, the coded-fragment encoder and the GF(2) Gaussian-elimination decoder. A pure algorithm module with no simulator dependencies. |
| `frag_transport.py` | **TS004 Fragmented Data Block Transport v2.0.0** on FPort 201: codecs for `FragSessionSetup`, `FragSessionDelete`, `FragSessionStatus`, `FragDataBlockReceived` and `DataFragment` (CIDs 0x00–0x04 and 0x08), and the device/server `Application`s that run a fragmentation session end to end. |
| `device_app.py` | Shared base for the device-side packages: the pending-uplink queue with per-entry `ready_at` and late-bound payloads (needed because TS005 `TimeToStart` is relative to the uplink). |
| `device_stack.py` | `FuotaDeviceStack` — the three packages (TS003 FPort 202, TS005 FPort 200, TS004 FPort 201) wired onto one `LoRaWanDevice`, with the periodic application uplink that paces the campaign, and `FuotaDeviceMetrics`. |
| `campaign.py` | `FuotaCampaign` — the server-side orchestration of the whole LoRa Alliance FUOTA process as a single simulation task: group setup → fragmentation session setup → multicast session → broadcast → status → repair rounds → cleanup, every phase bounded by a timeout. `FuotaCampaignConfig` and `FuotaCampaignResult` hold the parameters and the outcome. |

Spec versions: **TS003** (application clock synchronisation, reused from
`simulator/lorawan/applications/clock_sync.py`), **TS004-2.0.0** and **TS005-2.0.0**, both
FINAL, April 2022. TS006 Firmware Management is *not* implemented.

See `examples/lorawan_fuota/` for a runnable campaign and the list of simplifications
(single channel, simulation time as GPS time, duty-cycle model, class B caveats).

Running the FUOTA tests:

```bash
# Everything FUOTA
pipenv run pytest tests/ -k fuota

# The full-simulation campaigns (slowest, most interesting)
pipenv run pytest tests/test_lorawan_fuota_end_to_end.py -v
```

## Unit testing

Those wishing to extend the simulator with new features for their own use should know that there are unit tests located in the `tests/` directory. 
Test files should follow the naming convention `test_*.py`.
You can run/debug the unit tests using the following commands:

```bash
# Run all tests
pipenv run pytest

# Run with verbose output
pipenv run pytest -v

# Run a specific test file
pipenv run pytest tests/test_environment.py

# Run tests with print output visible
pipenv run pytest -v -s
```

