"""사용 중인 색인 보호, 삭제 선점/복구, 원문 보존과 실제 Chroma 정리를 검증한다."""

from concurrent.futures import ThreadPoolExecutor

import pytest
from test_ocr import setup as ocr_fixture
from test_search import ready

from handover_ai.adapters.chroma import ChromaVectors
from handover_ai.adapters.search_cleanup_repository import SQLiteSearchCleanupRepository
from handover_ai.api.dependencies import get_search_cleanup_service
from handover_ai.domain.search import SearchRequest
from handover_ai.services.search_cleanup import SearchCleanupService

setup = ocr_fixture


def prepare(setup):
    client, prefix, doc, structure, search = ready(setup)
    repo = SQLiteSearchCleanupRepository(search.repository.database)
    cleanup = SearchCleanupService(repo, search.vectors)
    client.app.dependency_overrides[get_search_cleanup_service] = lambda: cleanup
    first = search.enqueue(doc, structure)
    search.build(first)
    second = search.enqueue(doc, structure)
    search.build(second)
    return client, prefix, doc, structure, search, cleanup, first, second


def test_preview_explicit_delete_current_protection_and_evidence_retained(setup):
    client, prefix, doc, structure, search, cleanup, old, current = prepare(setup)
    endpoint = prefix + "/search-index/cleanup"
    chunks = client.get(prefix + "/chunks").json()
    candidates = client.get(endpoint).json()
    assert [row["run_id"] for row in candidates] == [old["run_id"]]
    result = client.post(endpoint, json={"run_ids": [old["run_id"], current["run_id"]]})
    assert [row["status"] for row in result.json()] == ["deleted", "protected"]
    assert old["run_id"] not in search.vectors.runs
    assert current["run_id"] in search.vectors.runs
    assert client.get(endpoint).json() == []
    assert client.get(prefix + "/chunks").json() == chunks
    assert search.search(doc, SearchRequest(structure_id=structure, query="DTC"))["hits"]
    assert cleanup.execute(doc, [old["run_id"]])[0]["status"] == "deleted"
    with setup[4].connect() as connection:
        assert connection.execute("SELECT count(*) FROM search_run").fetchone()[0] == 2
        assert connection.execute("SELECT count(*) FROM search_member").fetchone()[0] == 4


def test_other_document_unknown_and_running_ids_are_never_deleted(setup):
    client, prefix, doc, structure, search, cleanup, old, current = prepare(setup)
    pending = search.enqueue(doc, structure)
    assert cleanup.execute(doc, [pending["run_id"], "missing"])[0]["status"] == "protected"
    with pytest.raises(KeyError):
        cleanup.execute("missing-document", [old["run_id"]])
    # 다른 실제 문서 소속으로 요청해도 run_id만 보고 삭제하지 않는다.
    from test_ocr import scanned_pdf

    other = client.post(
        "/api/v1/documents", files={"file": ("other.pdf", scanned_pdf(0), "application/pdf")}
    ).json()["document"]["document_id"]
    assert cleanup.execute(other, [old["run_id"]])[0]["status"] == "protected"
    assert old["run_id"] in search.vectors.runs
    search.repository.start(pending["run_id"])
    assert cleanup.execute(doc, [pending["run_id"]])[0]["status"] == "protected"


def test_failed_delete_retry_and_interrupted_after_vector_delete(setup):
    _, _, doc, _, search, cleanup, old, _ = prepare(setup)
    delete = search.vectors.delete

    def failure(run_id):
        raise RuntimeError("private path")

    search.vectors.delete = failure
    assert cleanup.execute(doc, [old["run_id"]])[0]["status"] == "failed"
    candidate = cleanup.repository.candidates(doc, 20, 0)[0]
    assert candidate["cleanup_error"] == "delete_failed"
    # 실제 삭제 직후 프로세스 종료를 재현한다. SQLite 완료 전에 죽어도 복구 후
    # 없는 컬렉션을 다시 삭제하는 연산이 성공하므로 영원한 삭제 중 상태가 남지 않는다.
    search.vectors.delete = delete
    assert cleanup.repository.claim(doc, old["run_id"]) == "claimed"
    delete(old["run_id"])
    search.repository.recover_interrupted()
    assert cleanup.repository.candidates(doc, 20, 0)[0]["cleanup_error"] == "interrupted"
    assert cleanup.execute(doc, [old["run_id"]])[0]["status"] == "deleted"


def test_only_one_cleanup_claim_wins(setup):
    _, _, doc, _, _, cleanup, old, _ = prepare(setup)
    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(lambda _: cleanup.repository.claim(doc, old["run_id"]), range(2)))
    assert sorted(claims) == ["busy", "claimed"]
    assert cleanup.repository.candidates(doc, 20, 0) == []


def test_old_ocr_pointer_cleaned_but_historical_evidence_retained(setup):
    client, prefix, doc, _, search, cleanup, old, current = prepare(setup)
    chunk = client.get(prefix + "/chunks").json()[0]
    client.post(prefix + "/ocr")
    candidates = cleanup.repository.candidates(doc, 20, 0)
    assert {row["run_id"] for row in candidates} == {old["run_id"], current["run_id"]}
    assert all(row["reason"] == "old_ocr" for row in candidates)
    assert all(
        row["status"] == "deleted"
        for row in cleanup.execute(doc, [old["run_id"], current["run_id"]])
    )
    historical = client.get(prefix + "/chunks/" + chunk["chunk_id"]).json()
    assert historical["evidence"] == chunk["evidence"]
    with setup[4].connect() as connection:
        assert connection.execute("SELECT count(*) FROM search_active").fetchone()[0] == 0


def test_all_current_structures_and_model_versions_protected(setup):
    client, prefix, doc, _, search, cleanup, _, current = prepare(setup)
    version = client.get(prefix).json()["ingestion_id"]
    structure = client.post(prefix + "/layout", json={"ingestion_id": version}).json()[
        "structure_id"
    ]
    layout = search.enqueue(doc, structure)
    search.build(layout)
    search.embedder.version = "changed-model"
    results = cleanup.execute(doc, [current["run_id"], layout["run_id"]])
    assert all(item["status"] == "protected" for item in results)


def test_failed_run_and_request_validation(setup):
    client, prefix, doc, structure, search, cleanup, old, _ = prepare(setup)
    search.embedder.failing = True
    run = search.enqueue(doc, structure)
    search.build(run)
    assert any(row["reason"] == "failed_run" for row in cleanup.repository.candidates(doc, 20, 0))
    assert cleanup.execute(doc, [run["run_id"]])[0]["status"] == "deleted"
    endpoint = prefix + "/search-index/cleanup"
    for ids in ([], [old["run_id"]] * 2, [str(i) for i in range(21)], [""]):
        assert client.post(endpoint, json={"run_ids": ids}).status_code == 422
    assert client.get(endpoint + "?limit=0").status_code == 422
    assert client.get("/api/v1/documents/missing/search-index/cleanup").status_code == 404


def test_real_chroma_delete_isolated_and_idempotent(tmp_path):
    pytest.importorskip("chromadb")
    vectors = ChromaVectors(tmp_path / "vectors")
    for name in ("idx-old", "idx-current"):
        vectors.create(name, "v1")
        vectors.upsert(name, ["source"], [[1.0, 0.0]])
    vectors.delete("idx-old")
    vectors.delete("idx-old")
    assert vectors.count("idx-current") == 1
    assert vectors.search("idx-current", [1.0, 0.0], 1)[0][0] == "source"
