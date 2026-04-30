from app.services.qdrant_service import QdrantService, chunk_text, stable_id


class FakeService(QdrantService):
    def __init__(self):
        super().__init__(qdrant_url="http://qdrant.invalid", ollama_url="http://ollama.invalid", vector_size=3)
        self.points = []

    def embed(self, text):
        return [1.0, 0.0, 0.0]

    def upsert_chunks(self, collection, records):
        records = list(records)
        self.points.extend(records)
        return {"available": True, "indexed": len(records), "skipped": 0}


def test_chunk_text_overlaps_long_text():
    chunks = chunk_text("alpha " * 300, chunk_size=120, overlap=20)

    assert len(chunks) > 3
    assert all(chunk.strip() for chunk in chunks)
    assert all(len(chunk) <= 140 for chunk in chunks)


def test_stable_id_is_deterministic():
    assert stable_id("doc", 1, "text") == stable_id("doc", 1, "text")
    assert stable_id("doc", 1, "text") != stable_id("doc", 2, "text")


def test_index_document_chunks_records():
    service = FakeService()

    result = service.index_document("doc-1", "Sentence one. " * 80, {"source": "unit"})

    assert result["available"] is True
    assert result["indexed"] == len(service.points)
    assert service.points[0]["document_id"] == "doc-1"
    assert service.points[0]["source"] == "unit"


def test_semantic_search_graceful_when_unavailable():
    service = QdrantService(qdrant_url="http://127.0.0.1:1", ollama_url="http://127.0.0.1:1")

    result = service.semantic_search("market risk")

    assert result["available"] is False
    assert result["results"] == []
    assert "error" in result
