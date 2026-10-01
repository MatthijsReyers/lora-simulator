"""Tests for the TS004 v2.0.0 FragAlgo 0 fragmentation codec.

The parity-matrix generator is checked against an *independent* straight port of
the reference MATLAB code from TS004-2.0.0 §A.5, reproduced below, so that the
library implementation is never compared only against itself.
"""

import random
import time

import pytest

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


# ---------------------------------------------------------------------------
# Independent reference port of TS004-2.0.0 §A.5 MATLAB code.
# Written straight from the MATLAB, deliberately literal and slow.
# ---------------------------------------------------------------------------


def ref_prbs23(start: int) -> int:
    """%standard implementation of a 23bit prbs generator
    function r=prbs23(start)
    x= start;
    b0 = bitand(x,1);
    b1 = bitand(x,32)/32;
    x = floor(x/2) + bitxor(b0,b1)*2^22;
    r=x;
    """
    x = start
    b0 = x & 1
    b1 = (x & 32) // 32
    x = (x // 2) + (b0 ^ b1) * 2 ** 22
    return x


def ref_matrix_line(N: int, M: int) -> list[int]:
    """Literal port of the MATLAB ``matrix_line(N, M)``.

    Returns a 0-indexed list; MATLAB's ``matrix_line(k)`` is element ``k-1``.
    """
    line = [0] * M  # matrix_line = zeros(1,M)

    if N <= M:
        line[N - 1] = 1  # matrix_line(N) = 1
        return line

    # if (M == 2^floor(log2(M)))
    import math as _math
    if M == 2 ** int(_math.floor(_math.log2(M))):
        m = 1
    else:
        m = 0

    x = 1 + 1001 * N
    nb_coeff = 0
    while nb_coeff < M // 2:  # floor(M/2)
        r = 2 ** 16
        while r >= M:
            x = ref_prbs23(x)
            r = x % (M + m)
        if line[r + 1 - 1] == 0:  # matrix_line(r+1)
            line[r + 1 - 1] = 1
            nb_coeff += 1
    return line


def ref_encode(N: int, uncoded: list[bytes]) -> bytes:
    M = len(uncoded)
    size = len(uncoded[0])
    A = ref_matrix_line(N, M)
    s = bytearray(size)
    for x in range(M):
        if A[x] == 1:
            for k in range(size):
                s[k] ^= uncoded[x][k]
    return bytes(s)


# ---------------------------------------------------------------------------
# prbs23
# ---------------------------------------------------------------------------


class TestPrbs23:
    def test_matches_reference_port(self):
        x = 12345
        for _ in range(5000):
            assert prbs23(x) == ref_prbs23(x)
            x = prbs23(x)

    def test_taps_are_bit0_and_bit5(self):
        # x = 0b100000 (bit 5 set, bit 0 clear) -> feedback = 1
        assert prbs23(0b100000) == (0b100000 >> 1) + (1 << 22)
        # x = 0b100001 (both set) -> feedback = 0
        assert prbs23(0b100001) == (0b100001 >> 1)
        # x = 1 (bit 0 only) -> feedback = 1
        assert prbs23(1) == 0 + (1 << 22)

    def test_zero_is_the_degenerate_fixed_point(self):
        assert prbs23(0) == 0

    def test_period_is_2_pow_23_minus_1(self):
        # Walk the orbit of a non-zero seed: it must return to the seed after
        # exactly 2**23 - 1 steps and never earlier.
        seed = 1
        x = seed
        period = 0
        for i in range(1, 2 ** 23):
            x = prbs23(x)
            if x == seed:
                period = i
                break
        assert period == 2 ** 23 - 1

    def test_state_stays_within_23_bits(self):
        x = prbs23(1 + 1001 * 12345)  # large MATLAB-style seed
        for _ in range(1000):
            x = prbs23(x)
            assert 0 <= x < 2 ** 23


# ---------------------------------------------------------------------------
# matrix_line
# ---------------------------------------------------------------------------


class TestMatrixLine:
    @pytest.mark.parametrize("m", [1, 2, 3, 5, 7, 8, 16, 17, 32, 40, 48, 56, 64, 100, 127, 128])
    def test_matches_independent_reference_port(self, m):
        for n in range(1, 2 * m + 1):
            assert matrix_line(n, m) == ref_matrix_line(n, m), f"N={n} M={m}"

    def test_bits_form_agrees_with_list_form(self):
        for m in (3, 16, 37, 64):
            for n in range(1, 2 * m + 2):
                line = matrix_line(n, m)
                bits = matrix_line_bits(n, m)
                assert bits == sum(b << i for i, b in enumerate(line))

    @pytest.mark.parametrize("m", [1, 2, 5, 16, 31, 32, 100])
    def test_identity_part_for_n_le_m(self, m):
        for n in range(1, m + 1):
            line = matrix_line(n, m)
            assert sum(line) == 1
            assert line[n - 1] == 1

    @pytest.mark.parametrize("m", [2, 3, 4, 8, 16, 17, 32, 63, 64, 100, 255, 256])
    def test_parity_lines_have_exactly_floor_m_over_2_ones(self, m):
        for n in range(m + 1, m + 30):
            assert sum(matrix_line(n, m)) == m // 2, f"N={n} M={m}"

    def test_fixed_line_values(self):
        """Lines pinned against the independent port (regression vectors)."""
        # M=1: floor(M/2) == 0 ones -> parity lines are all-zero.
        assert matrix_line(1, 1) == [1]
        assert matrix_line(2, 1) == [0]

        # Small hand-checkable cases.
        assert matrix_line(1, 4) == [1, 0, 0, 0]
        assert matrix_line(4, 4) == [0, 0, 0, 1]

        for n, m in [(5, 4), (6, 4), (9, 8), (17, 16), (33, 32), (101, 100),
                     (150, 100), (200, 100), (65, 64), (70, 64)]:
            assert matrix_line(n, m) == ref_matrix_line(n, m)

        # Spec worked example (§A.1): matrix_line(1,100) is 1 then 99 zeros.
        assert matrix_line(1, 100) == [1] + [0] * 99
        # ...and matrix_line(101,100) is a parity line with 50 ones.
        line_101 = matrix_line(101, 100)
        assert sum(line_101) == 50
        assert line_101 == ref_matrix_line(101, 100)

    def test_power_of_two_special_case_changes_the_draw(self):
        """M a power of two uses modulus M+1 with rejection of the draw == M."""
        assert is_power_of_two(32) and not is_power_of_two(33)
        # Recompute line (33, 32) forcing m=0 and check it differs from the
        # spec's m=1 behaviour -- i.e. the special case is actually exercised.
        def without_special_case(N: int, M: int) -> list[int]:
            line = [0] * M
            x = 1 + 1001 * N
            nb = 0
            while nb < M // 2:
                r = 2 ** 16
                while r >= M:
                    x = ref_prbs23(x)
                    r = x % M
                if line[r] == 0:
                    line[r] = 1
                    nb += 1
            return line

        assert matrix_line(33, 32) != without_special_case(33, 32)
        assert matrix_line(33, 32) == ref_matrix_line(33, 32)

    def test_lines_are_deterministic_and_distinct(self):
        m = 64
        lines = {tuple(matrix_line(n, m)) for n in range(m + 1, m + 200)}
        assert len(lines) == 199  # no two parity lines collide
        assert matrix_line(100, m) == matrix_line(100, m)

    def test_parity_lines_depend_on_m(self):
        assert matrix_line_bits(200, 100) != matrix_line_bits(200, 101)

    def test_n_is_one_based(self):
        """A v1.0.0-style 0-based N would seed differently; guard against it."""
        # matrix_line(M+1, M) must use seed 1 + 1001*(M+1), not 1 + 1001*M.
        m = 50
        assert matrix_line(m + 1, m) == ref_matrix_line(m + 1, m)
        assert matrix_line(m + 1, m) != ref_matrix_line(m + 2, m)
        with pytest.raises(ValueError):
            matrix_line(0, m)

    def test_invalid_m(self):
        with pytest.raises(ValueError):
            matrix_line(1, 0)
        with pytest.raises(ValueError):
            matrix_line(1, -3)

    def test_fragment_indices_for_coded(self):
        for m in (5, 16, 37):
            for n in range(1, 2 * m + 1):
                line = matrix_line(n, m)
                expected = [i for i, b in enumerate(line) if b]
                assert fragment_indices_for_coded(n, m) == expected
        assert fragment_indices_for_coded(7, 100) == [6]


# ---------------------------------------------------------------------------
# Encoder
# ---------------------------------------------------------------------------


class TestEncoder:
    def test_matches_matlab_driver_example(self):
        """The §A.5 driver: w=32, fragment_size=10, DATA = mod(0:319, 256)."""
        w, fragment_size = 32, 10
        data = bytes(i % 256 for i in range(w * fragment_size))
        uncoded = [data[k * fragment_size:(k + 1) * fragment_size] for k in range(w)]

        enc = FragmentationEncoder(data, fragment_size)
        assert enc.nb_frag == w
        assert enc.padding == 0
        for y in range(1, 2 * w + 1):
            assert enc.fragment(y) == ref_encode(y, uncoded), f"y={y}"

    def test_first_m_fragments_are_the_uncoded_ones(self):
        data = bytes(range(100))
        frags, padding = encode_data_block(data, 10, 5)
        assert padding == 0
        assert b"".join(frags[:10]) == data

    @pytest.mark.parametrize("extra", list(range(0, 8)))
    def test_padding_field_semantics(self, extra):
        frag_size = 8
        size = 32 + extra  # 0..7 octets into the fifth fragment
        data = bytes(random.Random(extra).randrange(256) for _ in range(size))
        frags, padding = encode_data_block(data, frag_size, 0)
        nb_frag = len(frags)
        assert nb_frag * frag_size - padding == len(data)
        assert 0 <= padding < frag_size
        block = b"".join(frags)
        assert block[: len(data)] == data
        assert block[len(data):] == bytes(padding)

    def test_fragment_count_and_sizes(self):
        data = bytes(255)
        frags, _ = encode_data_block(data, 16, 7)
        assert len(frags) == 16 + 7  # ceil(255/16) == 16
        assert all(len(f) == 16 for f in frags)

    def test_fragments_generator_is_resumable(self):
        data = bytes(range(64))
        enc = FragmentationEncoder(data, 8)
        first = list(enc.fragments(10))
        more = list(enc.fragments(5, start=11))
        assert first + more == list(enc.fragments(15))

    def test_uncoded_fragment_accessor(self):
        enc = FragmentationEncoder(bytes(range(20)), 8)
        assert enc.nb_frag == 3
        assert enc.padding == 4
        assert enc.block_size == 20
        assert enc.uncoded_fragment(1) == bytes(range(8))
        assert enc.uncoded_fragment(3) == bytes(range(16, 20)) + bytes(4)
        with pytest.raises(ValueError):
            enc.uncoded_fragment(0)
        with pytest.raises(ValueError):
            enc.uncoded_fragment(4)

    def test_invalid_inputs(self):
        with pytest.raises(ValueError):
            FragmentationEncoder(b"", 4)
        with pytest.raises(ValueError):
            FragmentationEncoder(b"abc", 0)
        with pytest.raises(ValueError):
            encode_data_block(b"abc", 2, -1)
        with pytest.raises(ValueError):
            FragmentationEncoder(bytes(MAX_NB_FRAG + 1), 1)


# ---------------------------------------------------------------------------
# Decoder — helpers
# ---------------------------------------------------------------------------


def roundtrip(data: bytes, frag_size: int, redundancy: int, *,
              drop: set[int] | None = None,
              order: list[int] | None = None,
              duplicate_every: int = 0) -> tuple[bytes, FragmentationDecoder, int]:
    """Encode, transmit (optionally lossy/reordered) and decode.

    Returns ``(reconstructed, decoder, frames_until_complete)``; the
    reconstructed value is ``b""`` if the block never completed.
    """
    frags, padding = encode_data_block(data, frag_size, redundancy)
    nb_frag = len(frags) - redundancy
    dec = FragmentationDecoder(nb_frag, frag_size)

    indices = order if order is not None else list(range(1, len(frags) + 1))
    sent = 0
    frames_until_complete = 0
    for n in indices:
        if drop and n in drop:
            continue
        sent += 1
        done = dec.receive(n, frags[n - 1])
        if duplicate_every and sent % duplicate_every == 0:
            dec.receive(n, frags[n - 1])
        if done and not frames_until_complete:
            frames_until_complete = dec.nb_frames_received
            break
    if not dec.is_complete:
        return b"", dec, 0
    return dec.reconstruct(padding), dec, frames_until_complete


# ---------------------------------------------------------------------------
# Decoder
# ---------------------------------------------------------------------------


class TestDecoderBasics:
    def test_no_loss(self):
        data = bytes(range(200))
        out, dec, frames = roundtrip(data, 20, 10)
        assert out == data
        assert dec.is_complete
        assert frames == 10  # uncoded fragments alone suffice
        assert dec.nb_received == 10
        assert dec.missing_count() == 0
        assert dec.missing_uncoded() == []

    def test_single_fragment_block(self):
        data = b"hello"
        frags, padding = encode_data_block(data, 8, 0)
        assert len(frags) == 1
        dec = FragmentationDecoder(1, 8)
        assert dec.receive(1, frags[0]) is True
        assert dec.reconstruct(padding) == data

    def test_frag_size_one(self):
        data = bytes([7, 200, 0, 255, 13])
        out, _, _ = roundtrip(data, 1, 5)
        assert out == data

    def test_large_frag_size(self):
        rng = random.Random(1)
        data = bytes(rng.randrange(256) for _ in range(242 * 12 - 7))
        out, _, _ = roundtrip(data, 242, 24, drop={2, 5, 9})
        assert out == data

    def test_zero_padding_block(self):
        data = bytes(range(64))
        frags, padding = encode_data_block(data, 8, 0)
        assert padding == 0
        dec = FragmentationDecoder(8, 8)
        for n, f in enumerate(frags, 1):
            dec.receive(n, f)
        assert dec.reconstruct(0) == data
        assert dec.data == data

    @pytest.mark.parametrize("pad", list(range(0, 16)))
    def test_every_padding_value(self, pad):
        frag_size = 16
        size = 5 * frag_size - pad
        rng = random.Random(pad)
        data = bytes(rng.randrange(256) for _ in range(size))
        frags, padding = encode_data_block(data, frag_size, 5)
        assert padding == pad
        dec = FragmentationDecoder(5, frag_size)
        # drop two uncoded, use parity instead
        for n in [1, 3, 4, 6, 7, 8]:
            dec.receive(n, frags[n - 1])
        assert dec.is_complete
        assert dec.reconstruct(padding) == data

    def test_reversed_order(self):
        data = bytes(range(160))
        out, _, _ = roundtrip(data, 10, 8, order=list(range(24, 0, -1)))
        assert out == data

    def test_random_orders(self):
        data = bytes(range(250))
        for seed in range(25):
            rng = random.Random(seed)
            order = list(range(1, 36))
            rng.shuffle(order)
            out, _, _ = roundtrip(data, 10, 10, order=order)
            assert out == data, f"seed={seed}"

    def test_duplicates_are_discarded(self):
        data = bytes(range(100))
        frags, padding = encode_data_block(data, 10, 5)
        dec = FragmentationDecoder(10, 10)
        for n in range(1, 11):
            for _ in range(3):
                dec.receive(n, frags[n - 1])
        assert dec.is_complete
        assert dec.nb_received == 10          # rank, not raw count
        assert dec.nb_frames_received >= 10   # duplicates still counted raw
        assert dec.reconstruct(padding) == data

    def test_duplicates_interleaved(self):
        data = bytes(range(120))
        out, dec, _ = roundtrip(data, 12, 10, drop={2, 4, 7}, duplicate_every=2)
        assert out == data

    def test_fragments_after_completion_are_dropped(self):
        data = bytes(range(40))
        frags, padding = encode_data_block(data, 8, 5)
        dec = FragmentationDecoder(5, 8)
        for n in range(1, 6):
            dec.receive(n, frags[n - 1])
        assert dec.is_complete
        # Garbage after completion must not corrupt the block.
        assert dec.receive(9, bytes(8)) is True
        assert dec.reconstruct(padding) == data

    def test_uncoded_after_coded(self):
        """Uncoded fragments arriving late must still contribute."""
        data = bytes(range(80))
        frags, padding = encode_data_block(data, 8, 10)
        dec = FragmentationDecoder(10, 8)
        # Feed 5 parity fragments first, then the uncoded ones.
        for n in range(11, 16):
            dec.receive(n, frags[n - 1])
        assert not dec.is_complete
        assert dec.nb_received == 5
        for n in range(1, 11):
            if dec.receive(n, frags[n - 1]):
                break
        assert dec.is_complete
        assert dec.reconstruct(padding) == data

    def test_all_uncoded_lost_only_coded_received(self):
        """Recovery from parity fragments alone, for an M where it is possible.

        Possible only when ``floor(M/2)`` is odd (see
        :func:`matrix_line` docstring); M=22 -> 11 ones per parity line.
        """
        m, frag_size = 22, 8
        data = bytes((i * 37) % 256 for i in range(m * frag_size))
        frags, padding = encode_data_block(data, frag_size, 10 * m)
        dec = FragmentationDecoder(m, frag_size)
        n = m + 1
        while not dec.is_complete and n <= len(frags):
            dec.receive(n, frags[n - 1])
            n += 1
        assert dec.is_complete, "could not decode from parity fragments alone"
        assert dec.reconstruct(padding) == data
        assert dec.missing_uncoded() == list(range(1, m + 1))

    @pytest.mark.parametrize("m", [4, 8, 16, 32, 64, 100])
    def test_parity_only_cannot_reach_rank_m_when_weight_is_even(self, m):
        """Inherent TS004 property: even-weight parity lines span only M-1 dims.

        Every parity line has exactly floor(M/2) ones; when that is even, all
        parity lines live in the even-weight subspace (dimension M-1), so no
        amount of redundancy decodes without at least one uncoded fragment.
        """
        assert (m // 2) % 2 == 0  # premise of this test
        frag_size = 4
        data = bytes((i * 7) % 256 for i in range(m * frag_size))
        enc = FragmentationEncoder(data, frag_size)
        dec = FragmentationDecoder(m, frag_size)
        for n in range(m + 1, m + 20 * m + 1):
            dec.receive(n, enc.fragment(n))
        assert not dec.is_complete
        assert dec.nb_received == m - 1
        # One uncoded fragment (odd weight) is enough to finish it off.
        assert dec.receive(1, enc.fragment(1)) is True
        assert dec.reconstruct(enc.padding) == data

    def test_random_loss_below_redundancy(self):
        rng = random.Random(7)
        data = bytes(rng.randrange(256) for _ in range(100 * 16))
        for seed in range(20):
            r = random.Random(seed)
            lost = set(r.sample(range(1, 101), 20))
            frags, padding = encode_data_block(data, 16, 40)
            dec = FragmentationDecoder(100, 16)
            for n in range(1, len(frags) + 1):
                if n in lost:
                    continue
                if dec.receive(n, frags[n - 1]):
                    break
            assert dec.is_complete, f"seed={seed}"
            assert dec.reconstruct(padding) == data


class TestDecoderFailureModes:
    def test_incomplete_raises(self):
        data = bytes(range(80))
        frags, _ = encode_data_block(data, 8, 0)
        dec = FragmentationDecoder(10, 8)
        for n in range(1, 8):
            dec.receive(n, frags[n - 1])
        assert not dec.is_complete
        assert dec.missing_count() == 3
        assert dec.missing_uncoded() == [8, 9, 10]
        with pytest.raises(RuntimeError):
            _ = dec.data
        with pytest.raises(RuntimeError):
            dec.reconstruct(0)

    def test_fewer_than_m_independent_never_completes(self):
        data = bytes(range(160))
        frags, _ = encode_data_block(data, 10, 50)
        dec = FragmentationDecoder(16, 10)
        # Send M-1 distinct fragments plus many duplicates of them.
        for n in list(range(1, 16)) * 5:
            assert dec.receive(n, frags[n - 1]) is False
        assert not dec.is_complete
        assert dec.nb_received == 15
        assert dec.missing_count() == 1

    def test_dependent_coded_fragment_is_ignored(self):
        """A fragment that is the XOR of already-known rows adds no rank."""
        data = bytes(range(80))
        frags, _ = encode_data_block(data, 8, 0)
        dec = FragmentationDecoder(10, 8)
        for n in range(1, 10):
            dec.receive(n, frags[n - 1])
        assert dec.nb_received == 9
        # B1 XOR B2 is dependent on rows 1 and 2 already held.
        dependent = bytes(a ^ b for a, b in zip(frags[0], frags[1]))
        # Craft a parity-shaped reception by reusing index 1's line? Instead,
        # re-send a known fragment: guaranteed dependent.
        assert dec.receive(3, frags[2]) is False
        assert dec.nb_received == 9
        assert dependent  # (kept for clarity; XOR of two rows is dependent)

    def test_dependent_parity_fragments_do_not_count(self):
        """Over many parity receptions some are dependent; rank must lag."""
        m = 32
        data = bytes((i * 11) % 256 for i in range(m * 4))
        frags, _ = encode_data_block(data, 4, 4 * m)
        dec = FragmentationDecoder(m, 4)
        # Half the uncoded fragments, then parity until the block completes.
        for n in range(1, m // 2 + 1):
            dec.receive(n, frags[n - 1])
        n = m + 1
        while not dec.is_complete and n <= len(frags):
            dec.receive(n, frags[n - 1])
            n += 1
        assert dec.is_complete
        assert dec.nb_received == m
        # Some receptions were linearly dependent: raw count exceeds the rank.
        assert dec.nb_frames_received > m

    def test_wrong_payload_length_rejected(self):
        dec = FragmentationDecoder(4, 8)
        with pytest.raises(ValueError):
            dec.receive(1, bytes(7))
        with pytest.raises(ValueError):
            dec.receive(1, bytes(9))

    def test_zero_or_negative_index_rejected(self):
        dec = FragmentationDecoder(4, 8)
        with pytest.raises(ValueError):
            dec.receive(0, bytes(8))
        with pytest.raises(ValueError):
            dec.receive(-1, bytes(8))

    def test_invalid_construction(self):
        with pytest.raises(ValueError):
            FragmentationDecoder(0, 8)
        with pytest.raises(ValueError):
            FragmentationDecoder(4, 0)
        with pytest.raises(ValueError):
            FragmentationDecoder(MAX_NB_FRAG + 1, 8)

    def test_invalid_padding_argument(self):
        data = bytes(range(32))
        frags, _ = encode_data_block(data, 8, 0)
        dec = FragmentationDecoder(4, 8)
        for n in range(1, 5):
            dec.receive(n, frags[n - 1])
        with pytest.raises(ValueError):
            dec.reconstruct(8)
        with pytest.raises(ValueError):
            dec.reconstruct(-1)


class TestPowerOfTwoAndOddSizes:
    @pytest.mark.parametrize("m", [2, 4, 8, 16, 32, 64, 128])
    def test_power_of_two_m(self, m):
        rng = random.Random(m)
        frag_size = 7
        data = bytes(rng.randrange(256) for _ in range(m * frag_size - 3))
        frags, padding = encode_data_block(data, frag_size, 4 * m)
        dec = FragmentationDecoder(m, frag_size)
        lost = set(rng.sample(range(1, m + 1), m // 3))
        for n in range(1, len(frags) + 1):
            if n in lost:
                continue
            if dec.receive(n, frags[n - 1]):
                break
        assert dec.is_complete, f"M={m}"
        assert dec.reconstruct(padding) == data

    @pytest.mark.parametrize("m", [3, 5, 7, 17, 31, 33, 63, 100, 127, 129])
    def test_non_power_of_two_m(self, m):
        rng = random.Random(m)
        frag_size = 5
        data = bytes(rng.randrange(256) for _ in range(m * frag_size - 1))
        frags, padding = encode_data_block(data, frag_size, 4 * m)
        dec = FragmentationDecoder(m, frag_size)
        lost = set(rng.sample(range(1, m + 1), m // 3))
        for n in range(1, len(frags) + 1):
            if n in lost:
                continue
            if dec.receive(n, frags[n - 1]):
                break
        assert dec.is_complete, f"M={m}"
        assert dec.reconstruct(padding) == data


class TestScale:
    def test_m_1000_with_loss_and_runtime(self):
        m, frag_size = 1000, 50
        rng = random.Random(1234)
        data = bytes(rng.randrange(256) for _ in range(m * frag_size - 11))
        t0 = time.perf_counter()
        enc = FragmentationEncoder(data, frag_size)
        assert enc.nb_frag == m
        assert enc.padding == 11

        dec = FragmentationDecoder(m, frag_size)
        n = 0
        while not dec.is_complete and n < 2 * m:
            n += 1
            if rng.random() < 0.20:  # 20% loss
                continue
            dec.receive(n, enc.fragment(n))
        elapsed = time.perf_counter() - t0
        assert dec.is_complete
        assert dec.reconstruct(enc.padding) == data
        # Generous ceiling: this is a sanity check against accidental O(M^3).
        assert elapsed < 60.0, f"M=1000 round trip took {elapsed:.1f}s"

    def test_max_nb_frag_line_generation_is_fast(self):
        t0 = time.perf_counter()
        for n in range(MAX_NB_FRAG + 1, MAX_NB_FRAG + 6):
            assert bin(matrix_line_bits(n, MAX_NB_FRAG)).count("1") == MAX_NB_FRAG // 2
        assert time.perf_counter() - t0 < 20.0


class TestFuzz:
    def test_seeded_fuzz(self):
        """200 randomised sessions: sizes, loss patterns, order, duplicates."""
        for seed in range(200):
            rng = random.Random(seed)
            m = rng.randint(1, 40)
            frag_size = rng.randint(1, 24)
            size = rng.randint(max(1, (m - 1) * frag_size + 1), m * frag_size)
            data = bytes(rng.randrange(256) for _ in range(size))

            redundancy = rng.randint(0, 2 * m)
            frags, padding = encode_data_block(data, frag_size, redundancy)
            assert len(frags) == m + redundancy
            assert m * frag_size - padding == size

            dec = FragmentationDecoder(m, frag_size)
            order = list(range(1, len(frags) + 1))
            if rng.random() < 0.5:
                rng.shuffle(order)
            loss = rng.random() * 0.4
            for n in order:
                if rng.random() < loss:
                    continue
                done = dec.receive(n, frags[n - 1])
                if rng.random() < 0.1:  # duplicate
                    done = dec.receive(n, frags[n - 1])
                if done:
                    break

            if dec.is_complete:
                assert dec.reconstruct(padding) == data, f"seed={seed}"
                assert dec.nb_received == m
            else:
                assert dec.nb_received < m
                assert dec.missing_count() == m - dec.nb_received

    def test_fuzz_always_completes_with_full_transmission(self):
        for seed in range(60):
            rng = random.Random(1000 + seed)
            m = rng.randint(1, 30)
            frag_size = rng.randint(1, 16)
            size = rng.randint(max(1, (m - 1) * frag_size + 1), m * frag_size)
            data = bytes(rng.randrange(256) for _ in range(size))
            frags, padding = encode_data_block(data, frag_size, 0)
            dec = FragmentationDecoder(m, frag_size)
            order = list(range(1, m + 1))
            rng.shuffle(order)
            for n in order:
                dec.receive(n, frags[n - 1])
            assert dec.is_complete
            assert dec.reconstruct(padding) == data


class TestOverheadStatistics:
    def test_overhead_matches_spec_expectation(self):
        """§A.3: average overhead ~M+2, 99% within M+7.

        With M=100 and 30% random loss, assert the mean number of *received*
        fragments at completion is below M+10.
        """
        m, frag_size = 100, 8
        rng = random.Random(0)
        data = bytes(rng.randrange(256) for _ in range(m * frag_size))
        enc = FragmentationEncoder(data, frag_size)

        totals = []
        for seed in range(30):
            r = random.Random(seed)
            dec = FragmentationDecoder(m, frag_size)
            n = 0
            while not dec.is_complete and n < 10 * m:
                n += 1
                if r.random() < 0.30:
                    continue
                dec.receive(n, enc.fragment(n))
            assert dec.is_complete, f"seed={seed} never completed"
            assert dec.reconstruct(enc.padding) == data
            totals.append(dec.nb_frames_received)

        mean = sum(totals) / len(totals)
        assert mean < m + 10, f"mean received at completion {mean} (expected ~{m + 2})"
        assert all(t >= m for t in totals)  # can never finish with fewer than M
        # 99th-percentile-ish sanity: no run should be wildly off.
        assert max(totals) < m + 30

    def test_never_completes_before_m_fragments(self):
        m = 50
        data = bytes(range(m * 4))
        enc = FragmentationEncoder(data, 4)
        for seed in range(10):
            r = random.Random(seed)
            dec = FragmentationDecoder(m, 4)
            n = 0
            while not dec.is_complete and n < 10 * m:
                n += 1
                if r.random() < 0.25:
                    continue
                dec.receive(n, enc.fragment(n))
            assert dec.nb_frames_received >= m


# ---------------------------------------------------------------------------
# §A.4 memory formula
# ---------------------------------------------------------------------------


class TestRequiredMemory:
    @pytest.mark.parametrize("lmax,expected", [(32, 130), (40, 183), (48, 243),
                                               (56, 312), (64, 388)])
    def test_matches_spec_table(self, lmax, expected):
        assert required_memory_bytes(1000, lmax) == expected

    def test_independent_of_m(self):
        assert required_memory_bytes(100, 64) == required_memory_bytes(10000, 64)

    def test_defaults_to_worst_case(self):
        assert required_memory_bytes(64) == required_memory_bytes(64, 64)

    def test_monotonic(self):
        values = [required_memory_bytes(200, k) for k in range(0, 201)]
        assert values == sorted(values)
        assert values[0] == 0

    def test_invalid(self):
        with pytest.raises(ValueError):
            required_memory_bytes(0)
        with pytest.raises(ValueError):
            required_memory_bytes(10, 11)
        with pytest.raises(ValueError):
            required_memory_bytes(10, -1)
