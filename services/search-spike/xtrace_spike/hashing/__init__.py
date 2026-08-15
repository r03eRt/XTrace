"""Módulo hashing: firma perceptual pHash 64-bit (FR-004, ADR-0005)."""

from xtrace_spike.hashing.phash import PHASH_BITS, compute_phash, hamming_distance

__all__ = ["PHASH_BITS", "compute_phash", "hamming_distance"]
