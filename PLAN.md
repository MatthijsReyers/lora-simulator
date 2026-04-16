# LoRaWAN Implementation Plan

## Overview

This document describes the plan for implementing LoRaWAN MAC layer support in `simulator/lorawan/`.  
The implementation builds on top of the existing LoRa PHY layer (`simulator/lora/`) and follows the **LoRaWAN L2 1.0.4 specification** (`thesis/docs/ts001-1-0-4-lorawan-l2-1-0-4-specification.pdf`).

## Requirements

- **Full spec coverage** — Design for completeness, even if we implement incrementally.
- **Application layer abstraction** — LoRaWAN routes payloads by FPort to registered application handlers. We model this with an `Application` base class that handles packets going in/out of a specific port.
- **Multi-frequency support later** — Ignore channel plans for now (use a single frequency), but design the architecture so multi-channel/multi-SF can be added without major refactoring.
- **EU868 region** — Use EU868 parameters as the default region profile.
- **ABP first, then OTAA** — Start with Activation By Personalization for simplicity, add Over-The-Air Activation once the MAC layer is solid.
- **Build toward FUOTA** — The end goal is firmware update over the air (TS004 fragmentation, TS003 clock sync, TS005 remote multicast setup).

## Architecture

### Module structure

```
simulator/lorawan/
├── __init__.py
├── region.py              # Region parameters (EU868 DR table, RX2 defaults, etc.)
├── frame.py               # LoRaWAN frame encoding/decoding (PHYPayload, MHDR, MACPayload, MIC)
├── crypto.py              # AES-128 MIC, payload encryption, key derivation
├── mac_commands.py         # MAC command parsing and generation (LinkADRReq, DevStatusReq, etc.)
├── device.py              # End-device: session state, MAC layer, Class A/B/C operating modes
├── application.py         # Application base class (port-based payload routing)
├── gateway.py             # Gateway: forwards frames between radio and network server
├── network_server.py      # Network server: join handling, session management, downlink scheduling
└── join.py                # OTAA Join Request / Join Accept handling
```

### How it maps to the spec

| Spec concept         | Module                 | Notes |
|:---------------------|:-----------------------|:------|
| PHYPayload           | `frame.py`             | MHDR + MACPayload + MIC |
| MACPayload           | `frame.py`             | FHDR + FPort + FRMPayload |
| Frame encryption     | `crypto.py`            | AES-128 CTR mode per spec §4 |
| MIC calculation      | `crypto.py`            | AES-128 CMAC per spec §4 |
| MAC commands         | `mac_commands.py`      | CID-based parsing, FOpts vs port 0 |
| Class A timing       | `device.py`            | RECEIVE_DELAY1/2, RX1/RX2 windows |
| Class B timing       | `device.py`            | Beacon sync, ping slots (future) |
| Class C behavior     | `device.py`            | Continuous RX2 when not TX/RX1 |
| Operating mode switch| `device.py`            | Device can switch A↔B↔C at runtime |
| Join procedure       | `join.py`              | JoinRequest, JoinAccept, key derivation |
| Region parameters    | `region.py`            | DR table, TX power table, duty cycle |
| Application routing  | `application.py`       | FPort → Application instance |
| Network server       | `network_server.py`    | Downlink scheduling, ADR, session DB |

### Application abstraction

LoRaWAN routes payloads by `FPort` (1–223). Each port maps to an application handler.  
User-defined applications extend the `Application` base class:

```python
class Application(ABC):
    """Base class for LoRaWAN application handlers."""
    
    @abstractmethod
    def port(self) -> int:
        """The FPort this application handles (1–223)."""
        ...
    
    @abstractmethod
    async def on_downlink(self, payload: bytes) -> None:
        """Called when a downlink payload arrives on this port."""
        ...
    
    @abstractmethod
    async def on_uplink(self, payload: bytes) -> None:
        """Called when an uplink payload is sent on this port (for server-side apps)."""
        ...
```

This lets FUOTA-related services (fragmentation, clock sync, multicast setup) be implemented as `Application` subclasses on their respective standardized ports, independently from the MAC layer.

### Multi-frequency design

For now, all devices use a **single frequency** with the existing `LoraRadio`. The architecture accommodates multi-frequency later by:

1. `region.py` defines the channel plan (list of frequencies + allowed DRs per channel).
2. The device's MAC layer selects a channel before each uplink.
3. The gateway can be extended to hold multiple `LoraRadio` instances (one per channel) or we add frequency-awareness to the PHY layer.

These are future concerns — the current single-radio design works as-is for the initial implementation.

## Implementation Phases

### Phase 1: Core frame layer
**Goal:** Encode and decode LoRaWAN frames, compute MIC, encrypt/decrypt payloads.

Files: `frame.py`, `crypto.py`, `region.py`

- Implement `PHYPayload` with MHDR + MACPayload + MIC
- Implement frame direction (uplink/downlink)
- AES-128 CMAC for MIC (§4.4)
- AES-128 CTR for FRMPayload encryption (§4.3.3)
- EU868 data rate table and default RX2 parameters
- **Unit tests** for encode/decode roundtrips, MIC verification, encryption

### Phase 2: ABP Class A device + gateway
**Goal:** A working end-to-end uplink/downlink exchange.

Files: `device.py`, `gateway.py`, `network_server.py`, `application.py`

- `LoRaWanDevice` with pre-provisioned keys (DevAddr, NwkSKey, AppSKey)
- Single device class with `OperatingMode` enum (CLASS_A, CLASS_B, CLASS_C)
- Class A is the baseline: all devices start as Class A
- Class A uplink: build frame → TX → wait RX1 → wait RX2
- Gateway: continuous RX → strip PHY → forward to network server
- Network server: verify MIC, decrypt, route to application by FPort
- Network server: schedule downlink in RX1/RX2 window
- Application base class with port registration
- Frame counter management (FCntUp, FCntDown)
- **Example** in `examples/lorawan/` demonstrating a simple sensor uplink

### Phase 3: MAC commands
**Goal:** Support essential MAC commands for network management.

File: `mac_commands.py`

- `LinkCheckReq/Ans` — connectivity verification
- `LinkADRReq/Ans` — adaptive data rate
- `DevStatusReq/Ans` — battery + SNR reporting
- `RXParamSetupReq/Ans` — RX2 configuration
- `RXTimingSetupReq/Ans` — RX1 delay configuration
- FOpts piggybacking vs FPort 0 transport
- **Unit tests** for each command's encode/decode

### Phase 4: OTAA
**Goal:** Over-The-Air Activation with Join Request / Join Accept.

Files: `join.py`, updates to `device.py` and `network_server.py`

- JoinRequest frame (AppEUI, DevEUI, DevNonce)
- JoinAccept frame (AppNonce, NetID, DevAddr, DLSettings, RXDelay)
- Session key derivation (NwkSKey, AppSKey from AppKey + JoinNonce)
- Join server logic in network server
- DevNonce tracking (replay protection)

### Phase 5: Class B, Class C + Multicast
**Goal:** Additional operating modes and multicast group support (needed for FUOTA).

Files: updates to `device.py`

- Class C mode: RX2 window always open when not transmitting
- Class B mode: beacon synchronization, periodic ping slots (future)
- Device can switch operating mode at runtime (e.g., Class A → Class C for FUOTA, then back)
- Multicast group: shared DevAddr + session keys, downlink only
- Multicast address management on network server

### Phase 6: FUOTA
**Goal:** Firmware Update Over-The-Air built as applications.

New files (likely in `simulator/lorawan/fuota/` or as applications):

- **Clock Synchronization** (TS003, AppClockSync on FPort 202)
- **Remote Multicast Setup** (TS005, FPort 200)
- **Fragmentation** (TS004, FPort 201) — fragment session, redundancy (via coding), reassembly
- End-to-end FUOTA orchestration: network server creates multicast group → distributes keys → starts fragmentation session → devices reassemble firmware image
