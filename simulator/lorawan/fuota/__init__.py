"""
LoRaWAN FUOTA application-layer packages and campaign orchestration.

- TS005-2.0.0: Remote Multicast Setup (PackageIdentifier 2, FPort 200).
- TS004-2.0.0: Fragmented Data Block Transport (PackageIdentifier 3, FPort 201).

``campaign`` and ``device_stack`` put those two together with TS003 clock synchronisation
(FPort 202) into the end-to-end FUOTA process: :class:`FuotaCampaign` on the network-server
side, :class:`FuotaDeviceStack` on the device side.
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
from simulator.lorawan.fuota.device_app import FuotaDeviceApplication
from simulator.lorawan.fuota.device_stack import (
    DEFAULT_APPLICATION_FPORT,
    FuotaDeviceMetrics,
    FuotaDeviceStack,
)
from simulator.lorawan.fuota.frag_transport import (
    FRAGMENTATION_FPORT,
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
    MULTICAST_SETUP_FPORT,
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
    McGroupStatusReq,
    MulticastSessionRequest,
    MulticastSetupDeviceApplication,
    MulticastSetupServerApplication,
    ServerMulticastGroup,
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
    # -- shared device-side base --
    "FuotaDeviceApplication",
    # -- TS005 Remote Multicast Setup (FPort 200) --
    "MULTICAST_SETUP_FPORT",
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
    "McGroupStatusReq",
    "MulticastSessionRequest",
    "MulticastSetupDeviceApplication",
    "MulticastSetupServerApplication",
    "ServerMulticastGroup",
    # -- TS004 Fragmented Data Block Transport (FPort 201) --
    "FRAGMENTATION_FPORT",
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
    "FragmentationDeviceApplication",
    "FragmentationServerApplication",
    "block_ack_delay_seconds",
    "max_fragment_payload",
    # -- end-to-end orchestration --
    "DEFAULT_APPLICATION_FPORT",
    "FuotaCampaign",
    "FuotaCampaignConfig",
    "FuotaCampaignResult",
    "FuotaCampaignState",
    "FuotaDeviceMetrics",
    "FuotaDeviceStack",
]
