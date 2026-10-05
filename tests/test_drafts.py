"""외부 모델 없이 실제 SQLite/API로 근거 제한·생성 상태·버전 경쟁을 검증한다."""

import asyncio
import json

import httpx
import pytest
from test_ocr import setup as ocr_fixture
from test_search import ready

from handover_ai.adapters.draft_repository import SQLiteDraftRepository
from handover_ai.adapters.layout_review_repository import SQLiteLayoutReviewRepository
from handover_ai.adapters.ollama import OllamaDraftGateway
from handover_ai.api.dependencies import get_draft_service
from handover_ai.domain.drafts import DraftFailure, DraftRequest, DraftSelection, materialize
from handover_ai.domain.layout_review import ReviewRequest
from handover_ai.domain.search import SearchConflict, SearchUnavailable
from handover_ai.services.drafts import DraftService

setup = ocr_fixture


class FakeDraftGateway:
    model_id = "fixture-model"
    calls = 0
    invalid = 0
    callback = None

    def validate_config(self):
        pass

    async def select(self, query, blocks, *, retry):
        self.calls += 1
        if self.callback:
            self.callback()
        return DraftSelection(
            items=[
                {
                    "block_id": "invented" if self.calls <= self.invalid else blocks[0]["block_id"],
                    "category": "overview",
                }
            ]
        )


def prepare(setup):
    client, prefix, doc, structure, search = ready(setup)
    search.build(search.enqueue(doc, structure))
    gateway = FakeDraftGateway()
    service = DraftService(SQLiteDraftRepository(setup[4]), search, gateway, 3)
    client.app.dependency_overrides[get_draft_service] = lambda: service
    body = {
        "structure_id": structure,
        "query": "DTC 점검",
        "title": "점검 인수인계",
        "reviewed_only": False,
    }
    return client, prefix, doc, service, gateway, body


def test_draft_roundtrip_metadata_original_text_and_history(setup):
    client, prefix, doc, service, gateway, body = prepare(setup)
    response = client.post(prefix + "/drafts", json=body)
    assert response.status_code == 202 and response.json()["status"] == "queued"
    draft_id = response.json()["draft_id"]
    job = client.get(prefix + "/drafts/" + draft_id).json()
    assert job["status"] == "ready" and job["result"]["status"] == "needs_review"
    assert job["result"]["items"][0]["block"]["text"] == "DTC를 확인한다."
    assert "procedure" in job["result"]["missing_categories"]
    assert client.get(prefix + "/drafts").json() == [job]
    with setup[4].connect() as connection:
        run = connection.execute(
            "SELECT * FROM generation_run WHERE generation_run_id=?", (job["generation_run_id"],)
        ).fetchone()
        metadata = json.loads(run["metadata_json"])
        assert run["model_id"] == gateway.model_id and run["prompt_version"] == "excerpt-draft-v1"
        assert metadata["attempts"] == 1 and metadata["source_block_ids"]
    # OCR가 새 버전으로 바뀌어도 완료된 초안과 과거 근거 이미지 조회는 유지한다.
    client.post(prefix + "/ocr")
    assert client.get(prefix + "/drafts/" + draft_id).json() == job
    item = job["result"]["items"][0]
    assert client.get(prefix + f"/pages/{item['page_id']}/image").status_code == 200
    assert client.get(f"/api/v1/documents/other/drafts/{draft_id}").status_code == 404


@pytest.mark.parametrize("invalid,expected,calls", [(1, "ready", 2), (2, "failed", 2)])
def test_unknown_grounding_retries_once_only(setup, invalid, expected, calls):
    client, prefix, _, _, gateway, body = prepare(setup)
    gateway.invalid = invalid
    client.post(prefix + "/drafts", json=body)
    job = client.get(prefix + "/drafts").json()[0]
    assert job["status"] == expected and gateway.calls == calls
    if expected == "failed":
        assert job["error_code"] == "invalid_model_output" and job["result"] is None


def test_default_review_policy_rejects_unreviewed_sources(setup):
    client, prefix, _, _, gateway, body = prepare(setup)
    del body["reviewed_only"]
    client.post(prefix + "/drafts", json=body)
    job = client.get(prefix + "/drafts").json()[0]
    assert job["error_code"] == "no_eligible_evidence" and gateway.calls == 0


def test_review_change_during_model_response_blocks_publication(setup):
    client, prefix, doc, service, gateway, body = prepare(setup)
    ingestion = client.get(prefix).json()["ingestion_id"]
    structure = client.post(prefix + "/layout", json={"ingestion_id": ingestion}).json()[
        "structure_id"
    ]
    service.search.build(service.search.enqueue(doc, structure))
    page = client.get(prefix + "/layout/pages").json()[0]["page_id"]
    reviews = SQLiteLayoutReviewRepository(setup[4])
    reviews.save(
        doc,
        structure,
        page,
        ReviewRequest(
            expected_revision=0,
            status="confirmed",
            text_checked=True,
            order_checked=True,
            regions_checked=True,
        ),
    )
    gateway.callback = lambda: reviews.save(
        doc,
        structure,
        page,
        ReviewRequest(expected_revision=1, status="needs_correction", note="숫자 확인 필요"),
    )
    client.post(prefix + "/drafts", json={**body, "structure_id": structure, "reviewed_only": True})
    job = client.get(prefix + "/drafts").json()[0]
    assert job["error_code"] == "review_changed" and job["result"] is None


def test_ocr_change_during_response_and_restart_recovery(setup):
    client, prefix, doc, service, gateway, body = prepare(setup)

    def change():
        with setup[4].connect() as connection:
            connection.execute(
                "UPDATE document SET ingestion_id=? WHERE document_id=?",
                (setup[3]["ingestion_id"], doc),
            )

    gateway.callback = change
    client.post(prefix + "/drafts", json=body)
    assert client.get(prefix + "/drafts").json()[0]["error_code"] == "source_changed"
    # 원래 완료 버전으로 복원하여 중단된 작업의 복구만 독립 검증한다.
    with setup[4].connect() as connection:
        connection.execute(
            "UPDATE document SET ingestion_id=(SELECT ingestion_id FROM structure_run "
            "WHERE structure_id=?) WHERE document_id=?",
            (body["structure_id"], doc),
        )
    pending = service.enqueue(doc, DraftRequest(**body))
    with pytest.raises(SearchConflict):
        service.enqueue(doc, DraftRequest(**body))
    service.repository.start(pending)
    service.repository.recover_interrupted()
    assert service.repository.get(doc, pending["draft_id"])["error_code"] == "interrupted"


def test_duplicate_selection_rejected():
    selected = DraftSelection(items=[{"block_id": "x", "category": "overview"}] * 2)
    with pytest.raises(DraftFailure):
        materialize(selected, {"x": {"block": {"text": "source"}}})


def test_ollama_structured_http_and_errors():
    def respond(request):
        body = json.loads(request.content)
        assert body["stream"] is False and body["format"]["additionalProperties"] is False
        assert body["options"]["temperature"] == 0
        return httpx.Response(
            200,
            json={
                "done": True,
                "message": {
                    "content": json.dumps({"items": [{"block_id": "b", "category": "overview"}]})
                },
            },
        )

    gateway = OllamaDraftGateway(
        "http://127.0.0.1:11434", "local-test", 3, httpx.MockTransport(respond)
    )
    assert (
        asyncio.run(gateway.select("q", [{"block_id": "b", "text": "source"}])).items[0].block_id
        == "b"
    )
    for status, code in [(404, "model_not_found"), (500, "model_unavailable")]:
        gateway.transport = httpx.MockTransport(
            lambda request, status=status: httpx.Response(status)
        )
        with pytest.raises(DraftFailure) as failure:
            asyncio.run(gateway.select("q", []))
        assert failure.value.code == code
    gateway.transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json={"done": True, "message": {"content": "bad JSON"}})
    )
    with pytest.raises(DraftFailure, match="invalid_model_output"):
        asyncio.run(gateway.select("q", []))


@pytest.mark.parametrize(
    "endpoint,model", [("https://external.example", "m"), ("http://localhost:11434", "")]
)
def test_ollama_requires_local_config(endpoint, model):
    with pytest.raises(SearchUnavailable):
        OllamaDraftGateway(endpoint, model, 3).validate_config()


def test_total_model_timeout_is_recorded_without_retry(setup):
    client, prefix, _, service, gateway, body = prepare(setup)
    service.timeout = 0.01

    async def slow(*args, **kwargs):
        gateway.calls += 1
        await asyncio.sleep(1)

    gateway.select = slow
    client.post(prefix + "/drafts", json=body)
    job = client.get(prefix + "/drafts").json()[0]
    assert job["error_code"] == "model_timeout" and gateway.calls == 1


def test_correction_required_excluded_even_with_unreviewed_enabled(setup):
    client, prefix, doc, service, gateway, body = prepare(setup)
    ingestion = client.get(prefix).json()["ingestion_id"]
    structure = client.post(prefix + "/layout", json={"ingestion_id": ingestion}).json()[
        "structure_id"
    ]
    service.search.build(service.search.enqueue(doc, structure))
    reviews = SQLiteLayoutReviewRepository(setup[4])
    for page in client.get(prefix + "/layout/pages").json():
        reviews.save(
            doc,
            structure,
            page["page_id"],
            ReviewRequest(expected_revision=0, status="needs_correction", note="원문 대조 필요"),
        )
    client.post(prefix + "/drafts", json={**body, "structure_id": structure})
    assert client.get(prefix + "/drafts").json()[0]["error_code"] == "no_eligible_evidence"
    assert gateway.calls == 0


def test_missing_model_configuration_returns_503_without_job(setup):
    client, prefix, _, service, _, body = prepare(setup)
    service.gateway = OllamaDraftGateway("http://localhost:11434", "", 3)
    response = client.post(prefix + "/drafts", json=body)
    assert response.status_code == 503 and "LLM_MODEL" in response.json()["detail"]
    assert client.get(prefix + "/drafts").json() == []


def test_empty_selection_is_not_published_as_a_complete_draft():
    with pytest.raises(DraftFailure, match="no_relevant_evidence"):
        materialize(DraftSelection(items=[]), {"x": {"block": {"text": "source"}}})


def test_ollama_rejects_truncated_or_oversized_output_and_timeout():
    cases = [
        (
            lambda request: httpx.Response(200, json={"done": True, "done_reason": "length"}),
            "invalid_model_output",
        ),
        (
            lambda request: httpx.Response(200, content=b"x" * (1024 * 1024 + 1)),
            "invalid_model_output",
        ),
    ]
    for handler, code in cases:
        gateway = OllamaDraftGateway(
            "http://localhost:11434", "test", 1, httpx.MockTransport(handler)
        )
        with pytest.raises(DraftFailure, match=code):
            asyncio.run(gateway.select("q", []))

    def timeout(request):
        raise httpx.ReadTimeout("private connection details")

    gateway = OllamaDraftGateway("http://localhost:11434", "test", 1, httpx.MockTransport(timeout))
    with pytest.raises(DraftFailure, match="model_timeout"):
        asyncio.run(gateway.select("q", []))
