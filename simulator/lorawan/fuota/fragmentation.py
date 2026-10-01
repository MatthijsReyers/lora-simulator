"""TS004 v2.0.0 FragAlgo 0 forward-error-correction codec.

This module is a *pure algorithm* implementation of the fragmentation scheme
normatively described in Annex A of the LoRa Alliance specification
"LoRaWAN Fragmented Data Block Transport", TS004-2.0.0:

- §A.1  Fragment generation (encoder, parity-check matrix)
- §A.2  Fragment decoding and reassembly (receiver, GF(2) Gaussian elimination)
- §A.3  Performance of the coding scheme (overhead statistics)
- §A.4  Memory needed for defragmentation
- §A.5  Reference MATLAB code (``matrix_line`` / ``prbs23``)

It deliberately has **no dependency on the rest of the simulator** (no frames,
no LoRaWAN session state, no I/O) so that it can be unit-tested and reused
standalone. Wiring it into the FUOTA package commands (``FragSessionSetupReq``,
``DataFragment``) happens elsewhere.

Terminology (kept identical to the spec to ease cross-reading):

``M`` / ``nb_frag``
    Number of **uncoded** fragments ``B1 … BM`` the data block is split into.
    This is the ``NbFrag`` field of ``FragSessionSetupReq`` (§1.5), at most
    ``2**14 - 1 = 16383`` because the ``DataFragment`` ``N`` field is 14 bits.
``N``
    Index of a **coded** fragment, as carried in ``DataFragment`` (§1.8).
    **``N`` starts at 1 in v2.0.0** (it started at 0 in v1.0.0 — this is a
    breaking change; a v1 encoder and a v2 decoder do not interoperate).
``P^N_M``
    The coded fragment actually transmitted:
    ``P^N_M = C(1)*B1 XOR C(2)*B2 XOR … XOR C(M)*BM`` where ``C`` is the
    M-bit parity line ``matrix_line(N, M)``.
``Padding``
    Number of zero octets appended to ``BM`` so that all fragments are
    ``FragSize`` long. ``len(data block) = NbFrag * FragSize - Padding``.

Bit conventions used internally
-------------------------------
Parity lines are represented two ways:

- as ``list[int]`` of 0/1 of length ``M`` (``matrix_line``), 0-indexed over the
  uncoded fragments, matching the MATLAB reference's 1-based ``matrix_line(i)``
  at Python index ``i - 1``;
- as a Python ``int`` bitmask (``matrix_line_bits``) where **bit ``i`` of the
  integer corresponds to uncoded fragment ``B_{i+1}``**, i.e. the LSB is the
  leftmost column of the spec's vector. With this convention the spec's
  "first non-zero element of C" is simply the lowest set bit, and GF(2) row
  addition is a single ``^``.
"""

from __future__ import annotations

import math
from functools import lru_cache
from typing import Iterator

__all__ = [
    "MAX_NB_FRAG",
    "prbs23",
    "is_power_of_two",
    "matrix_line",
    "matrix_line_bits",
    "fragment_indices_for_coded",
    "encode_data_block",
    "FragmentationEncoder",
    "FragmentationDecoder",
    "required_memory_bytes",
]


#: Maximum number of uncoded fragments: ``N`` in ``DataFragment`` is 14 bits
#: (TS004-2.0.0 §1.5 / §1.8).
MAX_NB_FRAG = (1 << 14) - 1


# ---------------------------------------------------------------------------
# §A.5 — pseudo-random generator and parity-check matrix
# ---------------------------------------------------------------------------


def prbs23(x: int) -> int:
    """Advance the 23-bit PRBS generator one step (TS004-2.0.0 §A.5).

    Verbatim translation of the reference MATLAB::

        function r=prbs23(start)
        x= start;
        b0 = bitand(x,1);
        b1 = bitand(x,32)/32;
        x = floor(x/2) + bitxor(b0,b1)*2^22;
        r=x;

    Gotchas that matter for bit-exactness:

    - ``b1 = bitand(x, 32) / 32`` is **bit 5** (value 32), *not* bit 1.
    - ``floor(x/2)`` on a non-negative integer is ``x >> 1``.
    - The feedback bit is injected at bit 22, giving a 23-bit LFSR with
      period ``2**23 - 1``.

    Args:
        x: Current state. Only the low 23 bits participate once the generator
           has been stepped at least once; the seed ``1 + 1001*N`` used by
           :func:`matrix_line` may be larger and is accepted as-is, exactly as
           MATLAB does.

    Returns:
        The next state.
    """
    b0 = x & 1
    b1 = (x >> 5) & 1
    return (x >> 1) + ((b0 ^ b1) << 22)


def is_power_of_two(m: int) -> bool:
    """Return True when ``m`` is a (positive) power of two.

    Mirrors the MATLAB test ``M == 2^floor(log2(M))`` from §A.5.
    """
    return m > 0 and (m & (m - 1)) == 0


def _matrix_line_bits_uncached(n: int, m: int) -> int:
    """Compute line ``n`` of the MxM parity matrix as a bitmask. See §A.5."""
    if m <= 0:
        raise ValueError(f"nb_frag (M) must be positive, got {m}")
    if n < 1:
        raise ValueError(f"fragment index N is 1-based in TS004 v2.0.0, got {n}")

    # The first M coded fragments ARE the uncoded fragments: identity part.
    if n <= m:
        return 1 << (n - 1)

    # Powers of two tend to generate patterns in the LFSR output; the spec
    # works around it by drawing modulo M+1 and retrying whenever the draw
    # lands on M (which is out of range).
    mm = 1 if is_power_of_two(m) else 0

    x = 1 + 1001 * n  # per-line seed; N is 1-based in v2.0.0
    line = 0
    nb_coeff = 0
    target = m // 2  # exactly floor(M/2) ones per parity line
    while nb_coeff < target:
        # r = 2**16 is a sentinel that merely forces at least one PRBS step;
        # it is >= M for every M the spec allows (M <= 16383).
        r = 1 << 16
        while r >= m:
            x = prbs23(x)
            r = x % (m + mm)
        bit = 1 << r
        # Repeated draws hitting an already-set column are intended: the loop
        # keeps drawing until floor(M/2) *distinct* columns are set.
        if not (line & bit):
            line |= bit
            nb_coeff += 1
    return line


# Lines are requested repeatedly (encoder per transmission, decoder per
# reception) and are deterministic, so memoising is free correctness-wise.
_matrix_line_bits_cached = lru_cache(maxsize=8192)(_matrix_line_bits_uncached)


def matrix_line_bits(n: int, m: int) -> int:
    """Line ``n`` (1-based) of the MxM parity matrix, as an integer bitmask.

    Bit ``i`` of the result is set when uncoded fragment ``B_{i+1}``
    participates in the XOR producing ``P^N_M``.

    See :func:`matrix_line` for the spec-shaped list form and for the full set
    of gotchas.
    """
    return _matrix_line_bits_cached(n, m)


def matrix_line(n: int, m: int) -> list[int]:
    """Line ``n`` of the MxM parity-check matrix (TS004-2.0.0 §A.1/§A.5).

    Translation of the reference MATLAB ``matrix_line(N, M)``. The returned
    list has length ``m``; element ``i`` (0-based) is 1 when uncoded fragment
    ``B_{i+1}`` takes part in ``P^N_M``. MATLAB's ``matrix_line(k)`` is this
    list's element ``k - 1``.

    Gotchas (all load-bearing for interoperability):

    - **``N`` is 1-based in TS004 v2.0.0.** The seed is ``1 + 1001*N``, so an
      off-by-one in ``N`` produces a completely different, silently wrong code.
    - For ``N <= M`` the line is the identity row: ``P^N_M == B_N``. The first
      M "coded" fragments are literally the uncoded fragments, which is what
      makes the receiver's memory overhead independent of M.
    - For ``N > M`` the line has **exactly ``floor(M/2)`` ones**.
    - When ``M`` is a power of two the draw modulus is ``M + 1`` and any draw
      equal to ``M`` is rejected and redrawn; without this special case the
      LFSR produces visible patterns.
    - ``r = 2**16`` before the inner loop is a sentinel forcing at least one
      PRBS step, not a meaningful value.
    - Note ``floor(M/2)`` ones means ``M = 1`` yields an *all-zero* line for
      ``N > 1``: with a single uncoded fragment, redundancy carries no
      information. Callers should not request redundancy for ``M == 1``.
    - **Even-weight consequence** (not called out by the spec, but inherent to
      it): when ``floor(M/2)`` is even — which is the case for every ``M`` the
      spec's own examples use (32/40/48/56/64/100) — *every* parity line has
      even Hamming weight, so the parity lines all live in the even-weight
      subspace of dimension ``M - 1``. Parity fragments alone therefore can
      never reach rank ``M``: at least one uncoded fragment (``N <= M``, an
      odd-weight identity row) must be received. This is harmless in practice
      because the first ``M`` transmitted fragments *are* the uncoded ones, but
      it means "lose all of ``B1..BM``, recover from redundancy only" is
      impossible for those ``M``.

    Args:
        n: Coded fragment index ``N``, starting at 1.
        m: Number of uncoded fragments ``M`` (``NbFrag``).

    Returns:
        A list of ``m`` zeros/ones.

    Raises:
        ValueError: if ``m < 1`` or ``n < 1``.
    """
    bits = matrix_line_bits(n, m)
    return [(bits >> i) & 1 for i in range(m)]


def fragment_indices_for_coded(n: int, m: int) -> list[int]:
    """Indices of the uncoded fragments XORed together to build ``P^N_M``.

    Convenience view over :func:`matrix_line` for encoders that want to walk
    only the set columns.

    Args:
        n: Coded fragment index ``N``, 1-based (TS004 v2.0.0).
        m: Number of uncoded fragments ``M``.

    Returns:
        Sorted **0-based** indices into the uncoded fragment list. For
        ``n <= m`` this is exactly ``[n - 1]``.
    """
    bits = matrix_line_bits(n, m)
    out: list[int] = []
    while bits:
        low = bits & -bits
        out.append(low.bit_length() - 1)
        bits ^= low
    return out


# ---------------------------------------------------------------------------
# §A.1 — encoder
# ---------------------------------------------------------------------------


class FragmentationEncoder:
    """Server-side fragment generator (TS004-2.0.0 §A.1).

    Splits a data block into ``M`` uncoded fragments of ``frag_size`` octets
    (zero-padding the last one) and produces coded fragments ``P^N_M`` on
    demand. Fragments are generated on the fly, so the encoder never stores a
    parity matrix and an arbitrary amount of extra redundancy can be emitted
    later in a session without re-planning.

    Attributes:
        nb_frag: ``M``, the number of uncoded fragments (``NbFrag``).
        frag_size: Octets per fragment (``FragSize``).
        padding: Zero octets appended to the last uncoded fragment
            (``Padding``); ``len(data) == nb_frag * frag_size - padding``.
    """

    __slots__ = ("_uncoded", "frag_size", "nb_frag", "padding")

    def __init__(self, data: bytes, frag_size: int) -> None:
        """Split ``data`` into uncoded fragments.

        Args:
            data: The data block to transport. Must be non-empty.
            frag_size: Octets per fragment, >= 1.

        Raises:
            ValueError: on a non-positive ``frag_size``, empty ``data``, or a
                block requiring more than :data:`MAX_NB_FRAG` fragments.
        """
        if frag_size < 1:
            raise ValueError(f"frag_size must be >= 1, got {frag_size}")
        if not data:
            raise ValueError("data block must not be empty")

        nb_frag = (len(data) + frag_size - 1) // frag_size
        if nb_frag > MAX_NB_FRAG:
            raise ValueError(
                f"data block needs {nb_frag} fragments, exceeding the 14-bit "
                f"NbFrag limit of {MAX_NB_FRAG}"
            )

        self.nb_frag: int = nb_frag
        self.frag_size: int = frag_size
        self.padding: int = nb_frag * frag_size - len(data)

        padded = data + bytes(self.padding)
        self._uncoded: list[int] = [
            int.from_bytes(padded[i * frag_size:(i + 1) * frag_size], "big")
            for i in range(nb_frag)
        ]

    @property
    def block_size(self) -> int:
        """Size of the original (un-padded) data block in octets."""
        return self.nb_frag * self.frag_size - self.padding

    def uncoded_fragment(self, index: int) -> bytes:
        """Return uncoded fragment ``B_index`` (1-based), padding included."""
        if not 1 <= index <= self.nb_frag:
            raise ValueError(f"uncoded index must be 1..{self.nb_frag}, got {index}")
        return self._uncoded[index - 1].to_bytes(self.frag_size, "big")

    def fragment(self, n: int) -> bytes:
        """Build coded fragment ``P^N_M`` for on-air index ``n`` (1-based).

        For ``n <= nb_frag`` this returns the uncoded fragment ``B_n``
        unchanged, per §A.1.

        Args:
            n: ``DataFragment`` index ``N``, starting at 1.

        Returns:
            Exactly ``frag_size`` octets.
        """
        if n < 1:
            raise ValueError(f"fragment index N is 1-based, got {n}")
        if n <= self.nb_frag:
            return self._uncoded[n - 1].to_bytes(self.frag_size, "big")

        acc = 0
        bits = matrix_line_bits(n, self.nb_frag)
        uncoded = self._uncoded
        while bits:
            low = bits & -bits
            acc ^= uncoded[low.bit_length() - 1]
            bits ^= low
        return acc.to_bytes(self.frag_size, "big")

    def fragments(self, count: int, *, start: int = 1) -> Iterator[bytes]:
        """Yield ``count`` consecutive coded fragments starting at index ``start``.

        Generator form matching how a FUOTA server actually transmits: fragment
        indices increase monotonically and extra redundancy can simply continue
        from where the previous burst stopped.
        """
        for n in range(start, start + count):
            yield self.fragment(n)


def encode_data_block(
    data: bytes, frag_size: int, redundancy: int
) -> tuple[list[bytes], int]:
    """Fragment and encode a data block (TS004-2.0.0 §A.1).

    Produces ``M`` uncoded fragments followed by ``redundancy`` coded parity
    fragments, i.e. the on-air indices ``N = 1 … M + redundancy``.

    Args:
        data: The data block to transport.
        frag_size: ``FragSize``, octets per fragment.
        redundancy: Number of parity fragments to append (``N = M+1 … M+R``).
            May be 0.

    Returns:
        ``(fragments, padding)`` where ``fragments[k]`` is the fragment with
        on-air index ``N = k + 1`` and ``padding`` is the ``Padding`` field for
        ``FragSessionSetupReq``: the number of zero octets appended to the last
        uncoded fragment, so that
        ``len(data) == len_uncoded * frag_size - padding``.

    Raises:
        ValueError: for a negative ``redundancy``, or as per
            :class:`FragmentationEncoder`.
    """
    if redundancy < 0:
        raise ValueError(f"redundancy must be >= 0, got {redundancy}")
    enc = FragmentationEncoder(data, frag_size)
    return list(enc.fragments(enc.nb_frag + redundancy)), enc.padding


# ---------------------------------------------------------------------------
# §A.2 — decoder
# ---------------------------------------------------------------------------


class FragmentationDecoder:
    """Device-side defragmenter (TS004-2.0.0 §A.2).

    Implements the normative procedure verbatim:

    1. For each received ``P^N_M``, fetch ``C = matrix_line(N, M)``.
    2. Walk ``C`` left to right; for every set column ``i`` whose matrix row
       ``A(i)`` exists (``A(i)`` has a 1 at position ``i`` by construction),
       do ``C ^= A(i)`` and ``P ^= S_i``.
    3. If ``C`` is now zero the fragment is linearly dependent and is
       discarded; otherwise ``C`` is stored as ``A(i)`` and ``P`` as ``S_i``
       for ``i`` = the first non-zero element of ``C``.
    4. Repeat until all ``M`` rows of ``A`` are filled (rank ``M``).
    5. Back-substitute from ``i = M-1`` down to ``1``: for every 1 at position
       ``j > i`` in ``A(i)``, ``S_i ^= S_j``.
    6. ``S_i == B_i``; the block is ``S_1 : S_2 : … : S_M`` minus ``Padding``.

    Because ``P^i_M == B_i`` for ``i <= M``, an uncoded fragment arriving first
    costs no matrix bookkeeping at all — only fragments with ``N > M`` drive
    the elimination. That is the property that keeps the memory overhead
    independent of ``M`` (see :func:`required_memory_bytes`).

    This implementation keeps ``A`` as a sparse list of integer bit-rows
    indexed by pivot column, which is the Python equivalent of the spec's
    upper-triangular packed matrix, and keeps ``S`` as integers rather than
    byte buffers (XOR of a 1000-bit int is a single operation).

    Properties handled per spec:

    - duplicates are silently discarded (they are linearly dependent);
    - fragments may arrive in any order;
    - uncoded fragments arriving after coded ones still contribute;
    - linearly dependent coded fragments are ignored and **not** counted
      towards the rank.
    """

    __slots__ = (
        "_complete",
        "_matrix",
        "_rank",
        "_store",
        "_uncoded_seen",
        "frag_size",
        "nb_frag",
        "nb_frames_received",
    )

    def __init__(self, nb_frag: int, frag_size: int) -> None:
        """Create a decoder for a session of ``nb_frag`` fragments.

        Args:
            nb_frag: ``M`` / ``NbFrag`` from ``FragSessionSetupReq``.
            frag_size: ``FragSize``, octets per fragment.

        Raises:
            ValueError: for a non-positive ``nb_frag``/``frag_size`` or an
                ``nb_frag`` above :data:`MAX_NB_FRAG`.
        """
        if nb_frag < 1:
            raise ValueError(f"nb_frag must be >= 1, got {nb_frag}")
        if nb_frag > MAX_NB_FRAG:
            raise ValueError(f"nb_frag must be <= {MAX_NB_FRAG}, got {nb_frag}")
        if frag_size < 1:
            raise ValueError(f"frag_size must be >= 1, got {frag_size}")

        self.nb_frag: int = nb_frag
        self.frag_size: int = frag_size
        #: Total DataFragment payloads fed to :meth:`receive`, including
        #: duplicates and linearly dependent ones (``NbFragReceived``-ish).
        self.nb_frames_received: int = 0

        self._matrix: list[int] = [0] * nb_frag  # A, row per pivot column (0 = empty)
        self._store: list[int] = [0] * nb_frag  # S, fragment memory store
        self._rank: int = 0
        self._complete: bool = False
        self._uncoded_seen: set[int] = set()  # 1-based N <= M received directly

    # -- state -------------------------------------------------------------

    @property
    def is_complete(self) -> bool:
        """True once ``M`` linearly independent fragments have been received
        and the block has been reconstructed."""
        return self._complete

    @property
    def nb_received(self) -> int:
        """Number of linearly **independent** fragments accepted so far.

        This is the rank of the reconstructed parity matrix; it reaches
        ``nb_frag`` exactly when the block becomes recoverable. Duplicates and
        dependent fragments do not increase it (use
        :attr:`nb_frames_received` for the raw count).
        """
        return self._rank

    def missing_count(self) -> int:
        """How many more independent fragments are still needed."""
        return self.nb_frag - self._rank

    def missing_uncoded(self) -> list[int]:
        """1-based indices of the uncoded fragments not received directly.

        This is ``L`` in §A.4 terms — the number of fragments lost among the
        first ``M`` — and is what a real device compares against its ``Lmax``
        to decide whether to report ``MemoryError`` in
        ``FragSessionStatusAns``. Note that it stays non-empty even after the
        block has been fully recovered from parity fragments.
        """
        return [n for n in range(1, self.nb_frag + 1) if n not in self._uncoded_seen]

    # -- reception ---------------------------------------------------------

    def receive(self, index_n: int, data: bytes) -> bool:
        """Process one received ``DataFragment`` (§A.2 steps 1–6).

        Args:
            index_n: The ``N`` field of ``DataFragment``, **1-based**.
            data: The coded fragment payload, exactly ``frag_size`` octets.

        Returns:
            True once the whole data block is recovered (and on every later
            call), False while fragments are still missing.

        Raises:
            ValueError: if ``index_n < 1`` or ``len(data) != frag_size``.
        """
        if index_n < 1:
            raise ValueError(f"fragment index N is 1-based, got {index_n}")
        if len(data) != self.frag_size:
            raise ValueError(
                f"fragment payload must be {self.frag_size} octets, got {len(data)}"
            )

        self.nb_frames_received += 1

        if self._complete:
            # §1.8: once the block is reconstructed, further fragments for this
            # session are dropped.
            return True

        if index_n <= self.nb_frag:
            self._uncoded_seen.add(index_n)

        # Step 1.
        c = matrix_line_bits(index_n, self.nb_frag)
        p = int.from_bytes(data, "big")

        # Step 2: walk C left to right. A(i)'s lowest set bit is i, so XORing
        # A(i) clears column i and only touches columns > i; taking the lowest
        # set bit of the running C each time is exactly the spec's left-to-right
        # walk, minus the visits to columns that are zero anyway.
        matrix = self._matrix
        store = self._store
        while c:
            low = c & -c
            i = low.bit_length() - 1
            row = matrix[i]
            if row == 0:
                break
            c ^= row
            p ^= store[i]

        # Step 3a: linearly dependent (this also covers plain duplicates).
        if c == 0:
            return False

        # Step 3b: install at the pivot = first non-zero element of C.
        pivot = (c & -c).bit_length() - 1
        matrix[pivot] = c
        store[pivot] = p
        self._rank += 1

        # Step 4.
        if self._rank == self.nb_frag:
            self._back_substitute()
            self._complete = True
            return True
        return False

    def _back_substitute(self) -> None:
        """§A.2 step 5: resolve the upper-triangular system in place."""
        matrix = self._matrix
        store = self._store
        for i in range(self.nb_frag - 2, -1, -1):
            # Row i has its pivot at column i and zeros to the left; clear the
            # remaining columns j > i using the already-resolved S_j.
            rest = matrix[i] & ~((1 << (i + 1)) - 1)
            acc = store[i]
            while rest:
                low = rest & -rest
                acc ^= store[low.bit_length() - 1]
                rest ^= low
            store[i] = acc
            matrix[i] = 1 << i

    # -- output ------------------------------------------------------------

    @property
    def data(self) -> bytes:
        """The reassembled block **including** padding (``S_1 : … : S_M``).

        Raises:
            RuntimeError: if the block is not yet complete.
        """
        if not self._complete:
            raise RuntimeError(
                f"data block incomplete: {self.missing_count()} of "
                f"{self.nb_frag} fragments still missing"
            )
        size = self.frag_size
        return b"".join(s.to_bytes(size, "big") for s in self._store)

    def reconstruct(self, padding: int) -> bytes:
        """Return the original data block with the ``Padding`` octets stripped.

        Args:
            padding: The ``Padding`` field from ``FragSessionSetupReq`` (§1.5):
                ``len(block) == NbFrag * FragSize - Padding``.

        Returns:
            The original un-padded data block.

        Raises:
            RuntimeError: if the block is not yet complete.
            ValueError: if ``padding`` is outside ``0 .. frag_size - 1``.
        """
        if not 0 <= padding < self.frag_size:
            raise ValueError(
                f"padding must be in 0..{self.frag_size - 1}, got {padding}"
            )
        block = self.data
        return block[: len(block) - padding] if padding else block


# ---------------------------------------------------------------------------
# §A.3 / §A.4 — informative metrics
# ---------------------------------------------------------------------------


def required_memory_bytes(nb_frag: int, lmax: int | None = None) -> int:
    """Decoding memory needed **on top of** the reconstructed block (§A.4).

    The specification gives::

        parity matrix memory (octets) = Lmax * (Lmax + 1) / 2 / 8 + 2 * Lmax

    where ``L`` is the number of fragments lost among the first ``M``
    (``P^1_M … P^M_M``) and ``Lmax`` is the maximum a device chooses to
    tolerate. If ``L > Lmax`` defragmentation is aborted and the device reports
    ``MemoryError`` in ``FragSessionStatusAns``.

    Notably the result does **not** depend on ``M``: a 50 kB block sent as
    1000 x 50-octet fragments with ``Lmax = 64`` needs 50 kB + 388 octets.

    Args:
        nb_frag: ``M``. Used only as the worst-case bound on ``Lmax``.
        lmax: Maximum number of losses among the first ``M`` that the device
            tolerates. Defaults to ``nb_frag`` (worst case: everything lost).

    Returns:
        Octets of parity-matrix bookkeeping, rounded up. Matches the spec's
        table: Lmax 32/40/48/56/64 -> 130/183/243/312/388 octets.

    Raises:
        ValueError: if ``nb_frag < 1`` or ``lmax`` is outside ``0..nb_frag``.
    """
    if nb_frag < 1:
        raise ValueError(f"nb_frag must be >= 1, got {nb_frag}")
    if lmax is None:
        lmax = nb_frag
    if not 0 <= lmax <= nb_frag:
        raise ValueError(f"lmax must be in 0..{nb_frag}, got {lmax}")
    return math.ceil(lmax * (lmax + 1) / 2 / 8) + 2 * lmax
