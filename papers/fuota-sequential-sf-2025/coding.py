"""
The two decoding models of the paper. Neither needs the actual fragment contents: with a
rateless code every transmitted frame is a fresh coded fragment, so a recipient's state is the
number of coded fragments it holds.

- Ideal (Section VI): any k coded fragments decode the image.
- Raptor (Appendix A): the decoding attempt made upon receiving the k-th fragment fails with
  probability 0.85, and every attempt after that with 0.567, independently.
"""
from __future__ import annotations

import random


class Decoder:
    def __init__(self, fragments: int):
        self.fragments = fragments
        self.received = 0
        self.decoded = False

    def add_fragment(self) -> bool:
        """ Records one received coded fragment and reports whether the image is now decoded. """
        if self.decoded:
            return True
        self.received += 1
        if self.received >= self.fragments and self._attempt_succeeds():
            self.decoded = True
        return self.decoded

    def _attempt_succeeds(self) -> bool:
        raise NotImplementedError


class IdealDecoder(Decoder):
    def _attempt_succeeds(self) -> bool:
        return True


class RaptorDecoder(Decoder):
    def __init__(self, fragments: int, fail_first: float, fail_next: float, rng: random.Random):
        super().__init__(fragments)
        self.fail_first = fail_first
        self.fail_next = fail_next
        self.rng = rng

    def _attempt_succeeds(self) -> bool:
        failure = self.fail_first if self.received == self.fragments else self.fail_next
        return self.rng.random() >= failure


def make_decoder(code: str, fragments: int, fail_first: float, fail_next: float, rng: random.Random) -> Decoder:
    if code == "ideal":
        return IdealDecoder(fragments)
    if code == "raptor":
        return RaptorDecoder(fragments, fail_first, fail_next, rng)
    raise ValueError(f"Unknown code {code!r}")
