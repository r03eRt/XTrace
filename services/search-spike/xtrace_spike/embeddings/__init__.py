"""Embeddings visuales: interfaz EmbeddingProvider, fake y SigLIP local (FR-005, ADR-0007).

El import de `SiglipLocalProvider` es seguro sin el extra `siglip`: torch y
open_clip solo se importan de forma lazy dentro del provider (PR-005).
"""

from xtrace_spike.embeddings.fake import FakeEmbeddingProvider
from xtrace_spike.embeddings.provider import EmbeddingProvider
from xtrace_spike.embeddings.siglip_local import SiglipLocalProvider

__all__ = ["EmbeddingProvider", "FakeEmbeddingProvider", "SiglipLocalProvider"]
