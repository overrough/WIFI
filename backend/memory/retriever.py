"""
Vector retriever — semantic search over memories using ChromaDB.
Sentence-transformers provide local embeddings (no API key needed).
Swap this class out to use pgvector or Pinecone without touching the agent.
"""

import logging
import uuid
from typing import Optional

import chromadb
from chromadb.config import Settings as ChromaSettings
from sentence_transformers import SentenceTransformer

from core.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

# Lightweight model: fast, good quality, runs on CPU
EMBED_MODEL = "all-MiniLM-L6-v2"


class MemoryRetriever:
    """ChromaDB-backed semantic search over all memory types."""

    def __init__(self) -> None:
        self._client: Optional[chromadb.PersistentClient] = None
        self._embedder: Optional[SentenceTransformer] = None
        self._collection: Optional[chromadb.Collection] = None

    def _get_client(self) -> chromadb.PersistentClient:
        if self._client is None:
            self._client = chromadb.PersistentClient(
                path=settings.chroma_persist_dir,
                settings=ChromaSettings(anonymized_telemetry=False),
            )
        return self._client

    def _get_embedder(self) -> SentenceTransformer:
        if self._embedder is None:
            logger.info("Loading sentence-transformer model %s ...", EMBED_MODEL)
            self._embedder = SentenceTransformer(EMBED_MODEL)
        return self._embedder

    def _get_collection(self, user_id: str) -> chromadb.Collection:
        """One collection per user keeps memories isolated."""
        client = self._get_client()
        return client.get_or_create_collection(
            name=f"memories_{user_id.replace('-', '_')}",
            metadata={"hnsw:space": "cosine"},
        )

    def embed(self, text: str) -> list[float]:
        return self._get_embedder().encode(text, normalize_embeddings=True).tolist()

    def upsert(
        self,
        user_id: str,
        memory_id: str,
        content: str,
        metadata: dict,
    ) -> None:
        col = self._get_collection(user_id)
        embedding = self.embed(content)
        col.upsert(
            ids=[memory_id],
            embeddings=[embedding],
            documents=[content],
            metadatas=[{k: str(v) for k, v in metadata.items()}],
        )

    def search(
        self,
        user_id: str,
        query: str,
        top_k: int = 5,
        memory_type: Optional[str] = None,
    ) -> list[dict]:
        """Return top-k memories most relevant to query."""
        col = self._get_collection(user_id)
        if col.count() == 0:
            return []

        where = {"type": memory_type} if memory_type else None
        embedding = self.embed(query)

        results = col.query(
            query_embeddings=[embedding],
            n_results=min(top_k, col.count()),
            where=where,
            include=["documents", "metadatas", "distances"],
        )

        hits = []
        for doc, meta, dist in zip(
            results["documents"][0],
            results["metadatas"][0],
            results["distances"][0],
        ):
            hits.append({
                "content": doc,
                "metadata": meta,
                "score": 1.0 - dist,   # cosine distance → similarity
            })
        return hits

    def delete(self, user_id: str, memory_id: str) -> None:
        col = self._get_collection(user_id)
        col.delete(ids=[memory_id])


# Module-level singleton
_retriever: Optional[MemoryRetriever] = None


def get_retriever() -> MemoryRetriever:
    global _retriever
    if _retriever is None:
        _retriever = MemoryRetriever()
    return _retriever
