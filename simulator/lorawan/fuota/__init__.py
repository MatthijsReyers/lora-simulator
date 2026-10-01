"""
LoRaWAN FUOTA application-layer packages and campaign orchestration.

- TS005-2.0.0: Remote Multicast Setup (PackageIdentifier 2, FPort 200).
- TS004-2.0.0: Fragmented Data Block Transport (PackageIdentifier 3, FPort 201).

``campaign`` and ``device_stack`` put those two together with TS003 clock synchronisation
(FPort 202) into the end-to-end FUOTA process: :class:`FuotaCampaign` on the network-server
side, :class:`FuotaDeviceStack` on the device side.

Name clashes between the two packages
-------------------------------------
TS004 and TS005 define commands and helpers with the *same names* and *different wire
layouts*: ``PackageVersionReq``/``PackageVersionAns`` (CID 0x00 in both, but
``PackageIdentifier`` 2 vs 3), ``encode_commands``, ``parse_downlink_commands``,
``parse_uplink_commands`` and the ``PACKAGE_IDENTIFIER``/``PACKAGE_VERSION`` constants.
Re-exporting either set unprefixed from this package would silently shadow the other, so
**every clashing name is exported under a package-prefixed alias** -- ``Mc...`` for TS005
Remote Multicast Setup, ``Frag...`` for TS004 Fragmented Data Block Transport::

    from simulator.lorawan.fuota import McPackageVersionAns, FragPackageVersionAns
    from simulator.lorawan.fuota import encode_mc_commands, encode_frag_commands

The unprefixed names remain available from the modules themselves, which is the
unambiguous way to use them::

    from simulator.lorawan.fuota import multicast_setup, frag_transport
    multicast_setup.encode_commands([...])
    frag_transport.encode_commands([...])
"""

from simulator.lorawan.fuota.campaign import (
    FuotaCampaign,
    FuotaCampaignConfig,
    FuotaCampaignResult,
    FuotaCampaignState,
)
from simulator.lorawan.fuota.crypto import (
    MulticastKeyMaterial,
    compute_data_block_mic,
    decrypt_mc_key,
    derive_data_block_int_key,
    derive_mc_app_s_key,
    derive_mc_ke_key,
    derive_mc_nwk_s_key,
    derive_mc_root_key,
    derive_multicast_key_material,
    encrypt_mc_key,
)
from simulator.lorawan.fuota import frag_transport, multicast_setup
from simulator.lorawan.fuota.device_app import (
    FuotaDeviceApplication,
    PendingUplink,
    UplinkPayload,
)
from simulator.lorawan.fuota.device_stack import (
    DEFAULT_APPLICATION_FPORT,
    FuotaDeviceMetrics,
    FuotaDeviceStack,
)
from simulator.lorawan.fuota.frag_transport import (
    DATA_FRAGMENT_HEADER_SIZE,
    FRAGMENTATION_FPORT,
    MAX_FRAG_SESSIONS,
    MAX_MISSING_FRAG,
    MAX_NB_FRAG_RECEIVED,
    DataFragment,
    FragCID,
    FragDataBlockReceivedAns,
    FragDataBlockReceivedReq,
    FragmentationDeviceApplication,
    FragmentationServerApplication,
    FragServerSession,
    FragSessionDeleteAns,
    FragSessionDeleteReq,
    FragSessionSetupAns,
    FragSessionSetupReq,
    FragSessionState,
    FragSessionStatusAns,
    FragSessionStatusReq,
    FragStatusReport,
    block_ack_delay_seconds,
    max_fragment_payload,
)
from simulator.lorawan.fuota.frag_transport import (
    PACKAGE_IDENTIFIER as FRAG_PACKAGE_IDENTIFIER,
)
from simulator.lorawan.fuota.frag_transport import (
    PACKAGE_VERSION as FRAG_PACKAGE_VERSION,
)
from simulator.lorawan.fuota.frag_transport import (
    PackageVersionAns as FragPackageVersionAns,
)
from simulator.lorawan.fuota.frag_transport import (
    PackageVersionReq as FragPackageVersionReq,
)
from simulator.lorawan.fuota.frag_transport import (
    encode_commands as encode_frag_commands,
)
from simulator.lorawan.fuota.frag_transport import (
    parse_downlink_commands as parse_frag_downlink_commands,
)
from simulator.lorawan.fuota.frag_transport import (
    parse_uplink_commands as parse_frag_uplink_commands,
)
from simulator.lorawan.fuota.fragmentation import (
    MAX_NB_FRAG,
    FragmentationDecoder,
    FragmentationEncoder,
    encode_data_block,
    fragment_indices_for_coded,
    is_power_of_two,
    matrix_line,
    matrix_line_bits,
    prbs23,
    required_memory_bytes,
)
from simulator.lorawan.fuota.multicast_setup import (
    MAX_MULTICAST_GROUPS,
    MULTICAST_SETUP_FPORT,
    TIME_TO_START_UNSYNCHRONIZED,
    DeviceSetupState,
    McCID,
    McClassBSessionAns,
    McClassBSessionReq,
    McClassCSessionAns,
    McClassCSessionReq,
    McGroupDeleteAns,
    McGroupDeleteReq,
    McGroupSetupAns,
    McGroupSetupReq,
    McGroupStatusAns,
    McGroupStatusEntry,
    McGroupStatusReq,
    MulticastSessionRequest,
    MulticastSetupDeviceApplication,
    MulticastSetupServerApplication,
    ServerMulticastGroup,
)
from simulator.lorawan.fuota.multicast_setup import (
    PACKAGE_IDENTIFIER as MC_PACKAGE_IDENTIFIER,
)
from simulator.lorawan.fuota.multicast_setup import (
    PACKAGE_VERSION as MC_PACKAGE_VERSION,
)
from simulator.lorawan.fuota.multicast_setup import (
    PackageVersionAns as McPackageVersionAns,
)
from simulator.lorawan.fuota.multicast_setup import (
    PackageVersionReq as McPackageVersionReq,
)
from simulator.lorawan.fuota.multicast_setup import (
    encode_commands as encode_mc_commands,
)
from simulator.lorawan.fuota.multicast_setup import (
    parse_downlink_commands as parse_mc_downlink_commands,
)
from simulator.lorawan.fuota.multicast_setup import (
    parse_uplink_commands as parse_mc_uplink_commands,
)

__all__ = [
    # -- fragmentation (TS004 FEC) --
    "MAX_NB_FRAG",
    "FragmentationDecoder",
    "FragmentationEncoder",
    "encode_data_block",
    "fragment_indices_for_coded",
    "is_power_of_two",
    "matrix_line",
    "matrix_line_bits",
    "prbs23",
    "required_memory_bytes",
    # -- crypto (TS004 §3.3, TS005 §4.3) --
    "MulticastKeyMaterial",
    "compute_data_block_mic",
    "decrypt_mc_key",
    "derive_data_block_int_key",
    "derive_mc_app_s_key",
    "derive_mc_ke_key",
    "derive_mc_nwk_s_key",
    "derive_mc_root_key",
    "derive_multicast_key_material",
    "encrypt_mc_key",
    # -- the two codec modules, for the names that clash between them --
    "frag_transport",
    "multicast_setup",
    # -- shared device-side base --
    "FuotaDeviceApplication",
    "PendingUplink",
    "UplinkPayload",
    # -- TS005 Remote Multicast Setup (FPort 200) --
    "MULTICAST_SETUP_FPORT",
    "MAX_MULTICAST_GROUPS",
    "MC_PACKAGE_IDENTIFIER",
    "MC_PACKAGE_VERSION",
    "TIME_TO_START_UNSYNCHRONIZED",
    "DeviceSetupState",
    "McCID",
    "McClassBSessionAns",
    "McClassBSessionReq",
    "McClassCSessionAns",
    "McClassCSessionReq",
    "McGroupDeleteAns",
    "McGroupDeleteReq",
    "McGroupSetupAns",
    "McGroupSetupReq",
    "McGroupStatusAns",
    "McGroupStatusEntry",
    "McGroupStatusReq",
    "McPackageVersionAns",
    "McPackageVersionReq",
    "MulticastSessionRequest",
    "MulticastSetupDeviceApplication",
    "MulticastSetupServerApplication",
    "ServerMulticastGroup",
    "encode_mc_commands",
    "parse_mc_downlink_commands",
    "parse_mc_uplink_commands",
    # -- TS004 Fragmented Data Block Transport (FPort 201) --
    "DATA_FRAGMENT_HEADER_SIZE",
    "FRAGMENTATION_FPORT",
    "FRAG_PACKAGE_IDENTIFIER",
    "FRAG_PACKAGE_VERSION",
    "MAX_FRAG_SESSIONS",
    "MAX_MISSING_FRAG",
    "MAX_NB_FRAG_RECEIVED",
    "DataFragment",
    "FragCID",
    "FragDataBlockReceivedAns",
    "FragDataBlockReceivedReq",
    "FragServerSession",
    "FragSessionDeleteAns",
    "FragSessionDeleteReq",
    "FragSessionSetupAns",
    "FragSessionSetupReq",
    "FragSessionState",
    "FragSessionStatusAns",
    "FragSessionStatusReq",
    "FragStatusReport",
    "FragPackageVersionAns",
    "FragPackageVersionReq",
    "FragmentationDeviceApplication",
    "FragmentationServerApplication",
    "block_ack_delay_seconds",
    "encode_frag_commands",
    "max_fragment_payload",
    "parse_frag_downlink_commands",
    "parse_frag_uplink_commands",
    # -- end-to-end orchestration --
    "DEFAULT_APPLICATION_FPORT",
    "FuotaCampaign",
    "FuotaCampaignConfig",
    "FuotaCampaignResult",
    "FuotaCampaignState",
    "FuotaDeviceMetrics",
    "FuotaDeviceStack",
]
