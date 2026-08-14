"""Embeddings visuales: interfaz EmbeddingProvider y fakes deterministas (FR-005, ADR-0007)."""

from xtrace_spike.embeddings.fake import FakeEmbeddingProvider
from xtrace_spike.embeddings.provider import EmbeddingProvider

__all__ = ["EmbeddingProvider", "FakeEmbeddingProvider"]
