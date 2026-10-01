"""
LoRaWAN FUOTA application-layer packages.

- TS005-2.0.0: Remote Multicast Setup (PackageIdentifier 2, FPort 200).
- TS004-2.0.0: Fragmented Data Block Transport (PackageIdentifier 3, FPort 201).
"""

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

__all__ = [
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
]
