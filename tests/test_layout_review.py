"""검토 기록의 영속성, 동시 갱신 방지, 버전 격리와 검색 반영을 검증한다."""

from concurrent.futures import ThreadPoolExecutor

import pytest
from test_ocr import setup as ocr_fixture
from test_search import FakeEmbedder, MemoryVectors

from handover_ai.adapters.layout_review_repository import SQLiteLayoutReviewRepository
from handover_ai.adapters.search_repository import SQLiteSearchRepository
from handover_ai.domain.chunks import StructureConflict
from handover_ai.domain.layout_review import ReviewRequest
from handover_ai.domain.search import SearchRequest
from handover_ai.services.search import SearchService

setup = ocr_fixture


def ready(setup):
    client, _, _, document, database, _ = setup
    document_id = document["document_id"]
    prefix = f"/api/v1/documents/{document_id}"
    ingestion = client.post(prefix + "/ocr").json()["ingestion_id"]
    structure = client.post(prefix + "/layout", json={"ingestion_id": ingestion}).json()[
        "structure_id"
    ]
    page = client.get(prefix + "/layout/pages").json()[0]["page_id"]
    path = f"{prefix}/layout/{structure}"
    return client, document_id, structure, page, path, SQLiteLayoutReviewRepository(database)


def confirmed(revision=0):
    return {
        "expected_revision": revision,
        "status": "confirmed",
        "note": "원문 대조 완료",
        "text_checked": True,
        "order_checked": True,
        "regions_checked": True,
    }


def test_review_history_summary_and_reopen(setup):
    client, doc, structure, page, path, repo = ready(setup)
    endpoint = f"{path}/pages/{page}/review"
    assert client.get(endpoint).json()["status"] == "unreviewed"
    assert client.get(path + "/reviews").json()["unreviewed"] == 2
    first = client.put(endpoint, json=confirmed())
    assert first.status_code == 200 and first.json()["revision"] == 1
    # 새 Repository 객체로 조회해도 기록이 복원된다. 프로세스 메모리에 의존하지 않는다.
    assert (
        SQLiteLayoutReviewRepository(repo.database).get(doc, structure, page).status == "confirmed"
    )
    summary = client.get(path + "/reviews").json()
    assert summary["confirmed"] == 1 and summary["unreviewed"] == 1
    response = client.put(
        endpoint,
        json={"expected_revision": 1, "status": "needs_correction", "note": "  전화번호 재확인  "},
    )
    assert response.json()["note"] == "전화번호 재확인"
    assert client.get(path + "/reviews").json()["needs_correction"] == 1
    client.put(endpoint, json={"expected_revision": 2, "status": "unreviewed"})
    history = client.get(endpoint + "/history").json()
    assert [item["revision"] for item in history] == [3, 2, 1]
    assert [item["status"] for item in history] == ["unreviewed", "needs_correction", "confirmed"]
    assert client.get(endpoint + "/history?limit=1&offset=1").json() == history[1:2]
    assert client.get(path + "/reviews").json()["unreviewed"] == 2


@pytest.mark.parametrize(
    "body",
    [
        {"expected_revision": 0, "status": "confirmed"},
        {"expected_revision": 0, "status": "needs_correction", "note": "   "},
        {"expected_revision": -1, "status": "unreviewed"},
        {"expected_revision": 0, "status": "unreviewed", "note": "x" * 2001},
    ],
)
def test_invalid_review_never_written(setup, body):
    client, _, _, page, path, _ = ready(setup)
    endpoint = f"{path}/pages/{page}/review"
    assert client.put(endpoint, json=body).status_code == 422
    assert client.get(endpoint + "/history").json() == []


def test_concurrent_review_one_winner(setup):
    client, doc, structure, page, path, repo = ready(setup)

    def save():
        try:
            repo.save(doc, structure, page, ReviewRequest(**confirmed()))
            return "saved"
        except StructureConflict:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(lambda _: save(), range(2))) == ["conflict", "saved"]
    endpoint = f"{path}/pages/{page}/review"
    assert client.put(endpoint, json=confirmed()).status_code == 409
    assert len(client.get(endpoint + "/history").json()) == 1


def test_new_ocr_requires_new_review_and_preserves_old_history(setup):
    client, doc, _, page, path, _ = ready(setup)
    endpoint = f"{path}/pages/{page}/review"
    client.put(endpoint, json=confirmed())
    prefix = f"/api/v1/documents/{doc}"
    ingestion = client.post(prefix + "/ocr").json()["ingestion_id"]
    assert client.put(endpoint, json=confirmed(1)).status_code == 409
    assert client.get(endpoint).json()["status"] == "confirmed"
    structure = client.post(prefix + "/layout", json={"ingestion_id": ingestion}).json()[
        "structure_id"
    ]
    summary = client.get(f"{prefix}/layout/{structure}/reviews").json()
    assert summary["confirmed"] == 0 and summary["unreviewed"] == 2


def test_wrong_document_page_and_base_structure_rejected(setup):
    client, doc, _, page, path, _ = ready(setup)
    endpoint = f"{path}/pages/{page}/review"
    for wrong in (endpoint.replace(doc, "other"), endpoint.replace(page, "missing")):
        assert client.get(wrong).status_code == 404
        assert client.put(wrong, json=confirmed()).status_code == 404
    prefix = f"/api/v1/documents/{doc}"
    ingestion = client.get(prefix).json()["ingestion_id"]
    base = client.post(prefix + "/structure", json={"ingestion_id": ingestion}).json()[
        "structure_id"
    ]
    assert (
        client.put(f"{prefix}/layout/{base}/pages/{page}/review", json=confirmed()).status_code
        == 404
    )


def test_search_reflects_review_without_reindexing_and_keeps_warnings(setup):
    _, doc, structure, page, _, repo = ready(setup)
    service = SearchService(SQLiteSearchRepository(repo.database), FakeEmbedder(), MemoryVectors())
    run = service.enqueue(doc, structure)
    service.build(run)
    request = SearchRequest(structure_id=structure, query="DTC")
    before = service.search(doc, request)
    assert all(hit["human_review"].status == "unreviewed" for hit in before["hits"])
    repo.save(doc, structure, page, ReviewRequest(**confirmed()))
    after = service.search(doc, request)
    assert after["run_id"] == before["run_id"]
    hit = next(hit for hit in after["hits"] if hit["chunk"].page_id == page)
    assert hit["human_review"].status == "confirmed"
    assert hit["review_required"] and "heuristic_layout" in hit["review_reasons"]
