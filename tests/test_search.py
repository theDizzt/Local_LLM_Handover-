"""색인 공개 경계·재처리 격리·실제 Chroma 영속성을 검증한다."""

import pytest
from test_ocr import setup as ocr_fixture

from handover_ai.adapters.chroma import ChromaVectors
from handover_ai.adapters.search_repository import SQLiteSearchRepository
from handover_ai.api.dependencies import get_search_service
from handover_ai.domain.search import SearchConflict, SearchRequest
from handover_ai.services.search import SearchService

setup = ocr_fixture


class FakeEmbedder:
    version = "fixture-model-v1"
    failing = False

    def encode(self, texts, *, query=False):
        if self.failing:
            raise ValueError("private text")
        return [[1.0, 0.0, 0.0] for _ in texts]


class MemoryVectors:
    def __init__(self):
        self.runs = {}

    def create(self, run_id, version):
        self.runs[run_id] = {}

    def upsert(self, run_id, ids, vectors):
        self.runs[run_id].update(zip(ids, vectors, strict=True))

    def count(self, run_id):
        return len(self.runs[run_id])

    def delete(self, run_id):
        self.runs.pop(run_id, None)

    def search(self, run_id, vector, limit):
        return [(key, 0.0) for key in list(self.runs[run_id])[:limit]]


def ready(setup):
    client, _, _, document, database, _ = setup
    doc = document["document_id"]
    prefix = f"/api/v1/documents/{doc}"
    ingestion = client.post(prefix + "/ocr").json()["ingestion_id"]
    structure = client.post(prefix + "/structure", json={"ingestion_id": ingestion}).json()
    service = SearchService(SQLiteSearchRepository(database), FakeEmbedder(), MemoryVectors())
    client.app.dependency_overrides[get_search_service] = lambda: service
    return client, prefix, doc, structure["structure_id"], service


def test_api_roundtrip_and_source_evidence(setup):
    client, prefix, _, structure, _ = ready(setup)
    body = {"structure_id": structure}
    assert client.post(prefix + "/search", json={**body, "query": "DTC"}).status_code == 409
    assert client.post(prefix + "/search-index", json=body).status_code == 202
    state = client.get(prefix + "/search-index", params=body).json()
    assert state["active"]["completed"] == 2
    result = client.post(prefix + "/search", json={**body, "query": "DTC"})
    assert result.status_code == 200
    assert len(result.json()["hits"]) == 2
    for hit in result.json()["hits"]:
        chunk = hit["chunk"]
        assert chunk == client.get(prefix + "/chunks/" + chunk["chunk_id"]).json()
    assert client.post(prefix + "/search", json={**body, "query": "  "}).status_code == 422
    assert (
        client.post(prefix + "/search", json={**body, "query": "x", "top_k": 21}).status_code == 422
    )


def test_failed_rebuild_and_restart_keep_active(setup):
    _, _, doc, structure, service = ready(setup)
    first = service.enqueue(doc, structure)
    service.build(first)
    second = service.enqueue(doc, structure)
    assert service.state(doc, structure)["active"]["run_id"] == first["run_id"]
    with pytest.raises(SearchConflict):
        service.enqueue(doc, structure)
    service.embedder.failing = True
    service.build(second)
    assert service.state(doc, structure)["latest"]["status"] == "failed"
    assert service.state(doc, structure)["active"]["run_id"] == first["run_id"]
    service.enqueue(doc, structure)
    service.repository.recover_interrupted()
    state = service.state(doc, structure)
    assert state["latest"]["error_code"] == "interrupted"
    assert state["active"]["run_id"] == first["run_id"]


def test_ocr_change_blocks_pending_publication_and_old_search(setup):
    client, prefix, doc, structure, service = ready(setup)
    first = service.enqueue(doc, structure)
    service.build(first)
    pending = service.enqueue(doc, structure)
    client.post(prefix + "/ocr")
    service.build(pending)
    with pytest.raises(SearchConflict):
        service.search(doc, SearchRequest(structure_id=structure, query="DTC"))
    with setup[4].connect() as connection:
        row = connection.execute(
            "SELECT * FROM search_run WHERE run_id=?", (pending["run_id"],)
        ).fetchone()
        assert row["status"] == "failed" and row["error_code"] == "version_changed"


def test_model_and_structure_isolation_and_untrusted_vector_ids(setup):
    client, prefix, doc, structure, service = ready(setup)
    run = service.enqueue(doc, structure)
    service.build(run)
    service.embedder.version = "different-model"
    assert service.state(doc, structure)["active"] is None
    service.embedder.version = "fixture-model-v1"
    ingestion = client.get(prefix).json()["ingestion_id"]
    layout = client.post(prefix + "/layout", json={"ingestion_id": ingestion}).json()[
        "structure_id"
    ]
    assert service.state(doc, layout)["active"] is None
    foreign = client.get(prefix + "/layout/chunks").json()[0]["chunk_id"]
    good = client.get(prefix + "/chunks").json()[0]["chunk_id"]
    hits = service.repository.resolve(run, [(foreign, 0.0), (good, 0.1), (good, 0.2)])
    assert len(hits) == 1
    assert hits[0]["chunk"].chunk_id == good
    second = service.enqueue(doc, structure)
    service.build(second)
    with pytest.raises(SearchConflict):
        service.repository.resolve(run, [(good, 0.0)])


def test_real_chroma_persistence_and_collection_isolation(tmp_path):
    pytest.importorskip("chromadb")
    vectors = ChromaVectors(tmp_path / "vectors")
    vectors.create("idx-first", "v1")
    vectors.upsert("idx-first", ["phone", "backup"], [[1.0, 0.0], [0.0, 1.0]])
    vectors.create("idx-second", "v2")
    vectors.upsert("idx-second", ["other"], [[1.0, 0.0]])
    reopened = ChromaVectors(tmp_path / "vectors")
    assert reopened.count("idx-first") == 2
    assert reopened.search("idx-first", [0.0, 1.0], 1)[0][0] == "backup"
    assert reopened.search("idx-second", [0.0, 1.0], 1)[0][0] == "other"


def test_partial_index_never_published(setup):
    _, _, doc, structure, service = ready(setup)
    run = service.enqueue(doc, structure)
    assert service.repository.start(run["run_id"])
    rows = service.repository.batch(structure, 0)
    service.repository.progress(run["run_id"], rows[:1])
    with pytest.raises(SearchConflict):
        service.repository.finish(run)
    assert service.state(doc, structure)["active"] is None
    service.repository.recover_interrupted()
    assert service.state(doc, structure)["latest"]["status"] == "failed"


def test_invalid_vectors_and_missing_collection_do_not_leak_errors(setup):
    client, prefix, doc, structure, service = ready(setup)
    run = service.enqueue(doc, structure)
    service.build(run)
    service.vectors.runs.clear()
    response = client.post(prefix + "/search", json={"structure_id": structure, "query": "DTC"})
    assert response.status_code == 503
    assert run["run_id"] not in response.text
    service.embedder.encode = lambda texts, **kwargs: [[float("nan"), 0, 0] for _ in texts]
    second = service.enqueue(doc, structure)
    service.build(second)
    assert service.state(doc, structure)["latest"]["error_code"] == "index_failed"


def test_ocr_changes_during_vector_query_rejected(setup):
    client, prefix, doc, structure, service = ready(setup)
    run = service.enqueue(doc, structure)
    service.build(run)
    original = service.vectors.search

    def changed(*args):
        hits = original(*args)
        client.post(prefix + "/ocr")
        return hits

    service.vectors.search = changed
    with pytest.raises(SearchConflict):
        service.search(doc, SearchRequest(structure_id=structure, query="DTC"))


def test_layout_search_preserves_review_reasons(setup):
    client, prefix, doc, _, service = ready(setup)
    ingestion = client.get(prefix).json()["ingestion_id"]
    structure = client.post(prefix + "/layout", json={"ingestion_id": ingestion}).json()[
        "structure_id"
    ]
    service.build(service.enqueue(doc, structure))
    hits = service.search(doc, SearchRequest(structure_id=structure, query="DTC"))["hits"]
    assert hits and all("heuristic_layout" in hit["review_reasons"] for hit in hits)


def test_missing_local_model_has_actionable_error(tmp_path):
    from handover_ai.adapters.embedding import LocalE5Embedder
    from handover_ai.domain.search import SearchUnavailable

    with pytest.raises(SearchUnavailable, match="prepare_embedding_model"):
        _ = LocalE5Embedder(tmp_path).version
