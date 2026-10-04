"""Memory: ten kinds of memory with provenance, hybrid retrieval and owner-controlled forgetting.

Every memory carries where it came from, when it was learned and how confident Natasha is. Retrieval
mixes semantic similarity with keyword, entity, temporal, recency, importance and task signals so a
small local database can still behave like a personal memory. Nothing is stored as "fact" without a
provenance trail, and the owner can correct or forget at any granularity.
"""

from .embeddings import Embedder, Embedding, HashingEmbedder, ProviderEmbedder, cosine, get_embedder
from .models import MemoryKind, MemoryRecord, Provenance, Retention
from .retrieval import HybridRetriever, RetrievalQuery, ScoredMemory
from .manager import MemoryManager, get_memory_manager, reset_memory_managers
from .store import MemoryStore, get_memory_store, reset_memory_store
from .working import WorkingMemory, get_working_memory

__all__ = [
    "Embedder", "Embedding", "HashingEmbedder", "ProviderEmbedder", "cosine", "get_embedder",
    "MemoryKind", "MemoryRecord", "Provenance", "Retention",
    "HybridRetriever", "RetrievalQuery", "ScoredMemory",
    "MemoryStore", "get_memory_store", "reset_memory_store",
    "MemoryManager", "get_memory_manager", "reset_memory_managers",
    "WorkingMemory", "get_working_memory",
]
