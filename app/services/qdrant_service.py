"""Qdrant vector indexing and search helpers for FounderGraph-Lab.

The module intentionally has no hard dependency on qdrant-client or requests.
It talks to Qdrant and Ollama over HTTP and degrades to explicit unavailable
results when either service is down.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


DOCUMENT_COLLECTION = "startup_documents"
ENTITY_COLLECTION = "startup_entities"
DEFAULT_QDRANT_URL = os.getenv("QDRANT_URL", "http://localhost:6333")
DEFAULT_OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
DEFAULT_EMBED_MODEL = os.getenv("EMBEDDING_MODEL", os.getenv("OLLAMA_EMBED_MODEL", "nomic-embed-text"))
DEFAULT_VECTOR_SIZE = int(os.getenv("QDRANT_VECTOR_SIZE", "768"))


class VectorServiceUnavailable(RuntimeError):
    """Raised internally when Qdrant or Ollama cannot be reached."""


@dataclass(frozen=True)
class SearchResult:
    id: str
    score: float
    text: str
    payload: dict[str, Any]


def chunk_text(text: str, chunk_size: int = 900, overlap: int = 120) -> list[str]:
    """Split text into overlapping chunks without splitting every sentence."""

    clean = re.sub(r"\s+", " ", text).strip()
    if not clean:
        return []
    if len(clean) <= chunk_size:
        return [clean]

    chunks: list[str] = []
    start = 0
    while start < len(clean):
        end = min(start + chunk_size, len(clean))
        window = clean[start:end]
        if end < len(clean):
            boundary = max(window.rfind(". "), window.rfind("\n"), window.rfind("; "))
            if boundary >= chunk_size // 2:
                end = start + boundary + 1
                window = clean[start:end]
        chunks.append(window.strip())
        if end >= len(clean):
            break
        start = max(0, end - overlap)
    return [chunk for chunk in chunks if chunk]


def stable_id(*parts: Any) -> str:
    digest = hashlib.sha256("::".join(str(part) for part in parts).encode()).hexdigest()
    return str(uuid.UUID(hex=digest[:32]))


def _json_request(
    method: str,
    url: str,
    body: dict[str, Any] | None = None,
    timeout: float = 10.0,
) -> dict[str, Any]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        raise VectorServiceUnavailable(f"{exc}. Check that the requested Ollama model is pulled.") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise VectorServiceUnavailable(str(exc)) from exc
    return json.loads(raw) if raw else {}


class QdrantService:
    def __init__(
        self,
        qdrant_url: str = DEFAULT_QDRANT_URL,
        ollama_url: str = DEFAULT_OLLAMA_URL,
        embed_model: str = DEFAULT_EMBED_MODEL,
        vector_size: int = DEFAULT_VECTOR_SIZE,
    ) -> None:
        self.qdrant_url = qdrant_url.rstrip("/")
        self.ollama_url = ollama_url.rstrip("/")
        self.embed_model = embed_model
        self.vector_size = vector_size

    def ensure_collections(self) -> dict[str, Any]:
        results: dict[str, Any] = {}
        for collection in (DOCUMENT_COLLECTION, ENTITY_COLLECTION):
            results[collection] = self.ensure_collection(collection)
        return results

    def ensure_collection(self, collection: str) -> dict[str, Any]:
        body = {
            "vectors": {
                "size": self.vector_size,
                "distance": "Cosine",
            }
        }
        try:
            return _json_request(
                "PUT",
                f"{self.qdrant_url}/collections/{collection}",
                body,
                timeout=8,
            )
        except VectorServiceUnavailable as exc:
            return {"available": False, "error": str(exc), "collection": collection}

    def embed(self, text: str) -> list[float]:
        body = {"model": self.embed_model, "input": text}
        response = _json_request("POST", f"{self.ollama_url}/api/embed", body, timeout=30)
        embeddings = response.get("embeddings")
        if isinstance(embeddings, list) and embeddings:
            return [float(value) for value in embeddings[0]]
        embedding = response.get("embedding")
        if isinstance(embedding, list):
            return [float(value) for value in embedding]
        raise VectorServiceUnavailable("Ollama embed response did not include embeddings")

    def upsert_chunks(
        self,
        collection: str,
        records: Iterable[dict[str, Any]],
    ) -> dict[str, Any]:
        points = []
        skipped = 0
        for record in records:
            text = str(record.get("text") or "").strip()
            if not text:
                skipped += 1
                continue
            try:
                vector = self.embed(text)
            except VectorServiceUnavailable as exc:
                return {"available": False, "error": str(exc), "indexed": 0, "skipped": skipped}
            payload = dict(record)
            payload["text"] = text
            points.append({"id": record.get("id") or stable_id(collection, text), "vector": vector, "payload": payload})

        if not points:
            return {"available": True, "indexed": 0, "skipped": skipped}

        try:
            response = _json_request(
                "PUT",
                f"{self.qdrant_url}/collections/{collection}/points?wait=true",
                {"points": points},
                timeout=30,
            )
        except VectorServiceUnavailable as exc:
            return {"available": False, "error": str(exc), "indexed": 0, "skipped": skipped}
        return {"available": True, "indexed": len(points), "skipped": skipped, "response": response}

    def index_document(
        self,
        document_id: str,
        text: str,
        metadata: dict[str, Any] | None = None,
        chunk_size: int = 900,
        overlap: int = 120,
    ) -> dict[str, Any]:
        metadata = metadata or {}
        chunks = chunk_text(text, chunk_size=chunk_size, overlap=overlap)
        records = [
            {
                "id": stable_id(document_id, index, chunk),
                "document_id": document_id,
                "chunk_index": index,
                "text": chunk,
                **metadata,
            }
            for index, chunk in enumerate(chunks)
        ]
        return self.upsert_chunks(DOCUMENT_COLLECTION, records)

    def index_entity(self, entity_id: str, text: str, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        record = {"id": stable_id("entity", entity_id), "entity_id": entity_id, "text": text, **(metadata or {})}
        return self.upsert_chunks(ENTITY_COLLECTION, [record])

    def semantic_search(self, query: str, collection: str = DOCUMENT_COLLECTION, limit: int = 5) -> dict[str, Any]:
        try:
            vector = self.embed(query)
            response = _json_request(
                "POST",
                f"{self.qdrant_url}/collections/{collection}/points/search",
                {"vector": vector, "limit": limit, "with_payload": True},
                timeout=20,
            )
        except VectorServiceUnavailable as exc:
            return {"available": False, "error": str(exc), "results": []}

        results = []
        for item in response.get("result", []):
            payload = item.get("payload") or {}
            results.append(
                SearchResult(
                    id=str(item.get("id", "")),
                    score=float(item.get("score", 0.0)),
                    text=str(payload.get("text", "")),
                    payload=payload,
                )
            )
        return {"available": True, "results": results}

    def semantic_search_documents(self, query: str, top_k: int = 5) -> dict[str, Any]:
        return self.semantic_search(query, collection=DOCUMENT_COLLECTION, limit=top_k)

    def semantic_search_entities(self, query: str, top_k: int = 5) -> dict[str, Any]:
        return self.semantic_search(query, collection=ENTITY_COLLECTION, limit=top_k)


def _read_text_files(paths: Iterable[Path]) -> Iterable[tuple[Path, str]]:
    for path in paths:
        if path.is_file() and path.suffix.lower() in {".md", ".txt", ".json", ".csv"}:
            yield path, path.read_text(encoding="utf-8")


def index_startup_knowledge(
    root: str | Path = ".",
    service: QdrantService | None = None,
) -> dict[str, Any]:
    """Index sample/vault documents and lightweight entity files into Qdrant."""

    base = Path(root)
    service = service or QdrantService()
    status = {"collections": service.ensure_collections(), "documents": [], "entities": []}

    document_paths = list((base / "sample_data").glob("**/*")) + list((base / "vault" / "documents").glob("**/*"))
    for path, text in _read_text_files(document_paths):
        result = service.index_document(
            document_id=str(path.relative_to(base)),
            text=text,
            metadata={"source_path": str(path.relative_to(base)), "kind": "document"},
        )
        status["documents"].append({"path": str(path.relative_to(base)), **result})

    entity_paths = list((base / "vault" / "entities").glob("**/*"))
    for path, text in _read_text_files(entity_paths):
        result = service.index_entity(
            entity_id=str(path.relative_to(base)),
            text=text,
            metadata={"source_path": str(path.relative_to(base)), "kind": "entity"},
        )
        status["entities"].append({"path": str(path.relative_to(base)), **result})

    return status


def semantic_search(query: str, collection: str = DOCUMENT_COLLECTION, limit: int = 5) -> dict[str, Any]:
    return QdrantService().semantic_search(query, collection=collection, limit=limit)


def semantic_search_documents(query: str, top_k: int = 5) -> dict[str, Any]:
    return QdrantService().semantic_search_documents(query, top_k=top_k)


def semantic_search_entities(query: str, top_k: int = 5) -> dict[str, Any]:
    return QdrantService().semantic_search_entities(query, top_k=top_k)
