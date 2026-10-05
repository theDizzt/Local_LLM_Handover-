# [읽기 안내] 실제 PDF 렌더링/SQLite/API를 연결한 테스트이며 OCR 추론만 fixture로 대체한다.
# TestClient는 응답 후 BackgroundTasks 완료까지 기다리므로 시작 응답은 queued여도
# 바로 다음 상태 조회에서는 completed/failed가 보인다. 실제 HTTP는 별도 폴링이 필요하다.
from dataclasses import replace
from pathlib import Path

import pymupdf
import pytest
from fastapi.testclient import TestClient

from handover_ai.adapters.document_repository import SQLiteDocumentRepository
from handover_ai.adapters.ocr import PaddleOcrEngine, PyMuPdfRenderer
from handover_ai.adapters.ocr_repository import SQLiteOcrRepository
from handover_ai.adapters.sqlite import SQLiteDatabase
from handover_ai.api.dependencies import get_ocr_service
from handover_ai.config import load_settings
from handover_ai.domain.ocr import OcrConflict, OcrLine, OcrUnavailable, RenderedPage
from handover_ai.main import create_app
from handover_ai.services.ocr import OcrService


class FixtureEngine:
    version = "fixture-1"
    fail_on = None

    def recognize(self, page):
        if page.number == self.fail_on:
            raise RuntimeError("private document content must not appear in response")
        assert page.image_path.read_bytes().startswith(b"\x89PNG")
        return [OcrLine(text="DTC를 확인한다.", bbox=(0.1, 0.1, 0.8, 0.2), confidence=0.93)]


def scanned_pdf(rotation=0):
    # 텍스트 레이어 없는 이미지 PDF를 만든다. 서비스 테스트는 OCR 인식 정확도
    # 대신 실제 렌더링/저장/좌표 연결을 검증하며 엔진 응답만 fixture로 대체한다.
    with pymupdf.open() as original:
        page = original.new_page(width=240, height=120)
        page.insert_text((20, 50), "Check DTC before restart", fontsize=12)
        image = page.get_pixmap().tobytes("png")
    with pymupdf.open() as pdf:
        for _ in range(2):
            page = pdf.new_page(width=240, height=120)
            page.insert_image(page.rect, stream=image)
            page.set_rotation(rotation)
        return pdf.tobytes()


@pytest.fixture
def setup(tmp_path):
    settings = replace(
        load_settings(),
        database_path=tmp_path / "db.sqlite",
        document_store_path=tmp_path / "documents",
        page_store_path=tmp_path / "pages",
        ocr_dpi=72,
    )
    database = SQLiteDatabase(settings.database_path)
    engine = FixtureEngine()
    service = OcrService(
        SQLiteDocumentRepository(database),
        SQLiteOcrRepository(database),
        PyMuPdfRenderer(72),
        engine,
        settings.page_store_path,
    )
    app = create_app(settings, database)
    app.dependency_overrides[get_ocr_service] = lambda: service
    with TestClient(app) as client:
        document = client.post(
            "/api/v1/documents", files={"file": ("scan.pdf", scanned_pdf(90), "application/pdf")}
        ).json()["document"]
        yield client, service, engine, document, database, settings


def test_ocr_api_preserves_pages_and_versions(setup):
    client, service, _, document, _, settings = setup
    prefix = f"/api/v1/documents/{document['document_id']}"
    response = client.post(prefix + "/ocr")
    assert response.status_code == 202
    assert response.json()["status"] == "queued"
    job = client.get(response.headers["Location"]).json()
    assert job["status"] == "completed"
    assert job["completed_pages"] == job["total_pages"] == 2
    pages = client.get(prefix + "/pages").json()
    assert [p["pdf_page_number"] for p in pages] == [1, 2]
    assert pages[0]["rotation"] == 90
    assert pages[0]["width_px"] == 120 and pages[0]["height_px"] == 240
    assert pages[0]["blocks"][0]["bbox"] == [0.1, 0.1, 0.8, 0.2]
    assert "image_uri" not in pages[0]
    image = client.get(prefix + f"/pages/{pages[0]['page_id']}/image")
    assert image.status_code == 200 and image.content.startswith(b"\x89PNG")
    assert client.get(prefix + "/pages?limit=1&offset=1").json() == pages[1:]
    assert client.get(prefix + "/pages?limit=0").status_code == 422
    next_job = client.post(prefix + "/ocr").json()
    assert next_job["ingestion_id"] != job["ingestion_id"]
    assert client.get(prefix).json()["ingestion_id"] == next_job["ingestion_id"]
    assert client.get(prefix + f"/pages?ingestion_id={job['ingestion_id']}").json() == pages
    # 새로운 Repository에서도 완료 상태가 유지되며 원본 PNG도 남는다.
    assert service.repository.get_job(job["job_id"]).status == "completed"
    assert len(list(settings.page_store_path.glob("*/*.png"))) == 4


def test_failed_retry_does_not_replace_successful_evidence(setup):
    client, service, engine, document, database, _ = setup
    prefix = f"/api/v1/documents/{document['document_id']}"
    client.post(prefix + "/ocr")
    before = client.get(prefix + "/pages").json()
    engine.fail_on = 2
    response = client.post(prefix + "/ocr")
    failed = client.get(response.headers["Location"]).json()
    assert failed["status"] == "failed" and failed["completed_pages"] == 1
    assert failed["error_code"] == "processing_failed"
    assert "private" not in str(failed)
    assert client.get(prefix + "/pages").json() == before
    assert client.get(prefix + f"/pages?ingestion_id={failed['ingestion_id']}").json() == []
    with database.connect() as connection:
        page_id = connection.execute(
            "SELECT page_id FROM page WHERE ingestion_id = ?", (failed["ingestion_id"],)
        ).fetchone()[0]
    assert client.get(prefix + f"/pages/{page_id}/image").status_code == 404
    engine.fail_on = None
    retry = client.post(prefix + "/ocr")
    assert client.get(retry.headers["Location"]).json()["status"] == "completed"
    # 중복 실행 콜백이 와도 이미 완료된 작업에 페이지를 다시 삽입하지 않는다.
    service.run(retry.json()["job_id"])
    assert len(client.get(prefix + "/pages").json()) == 2


def test_duplicate_job_and_restart_recovery(setup):
    client, service, _, document, database, settings = setup
    pending = service.enqueue(document["document_id"])
    latest_url = f"/api/v1/documents/{document['document_id']}/ocr/latest"
    assert client.get(latest_url).json()["status"] == "queued"
    with pytest.raises(OcrConflict):
        service.enqueue(document["document_id"])
    assert client.post(f"/api/v1/documents/{document['document_id']}/ocr").status_code == 409
    service.repository.start(pending.job_id)
    assert client.get(latest_url).json()["status"] == "running"
    with TestClient(create_app(settings, database)) as restarted:
        job = restarted.get(f"/api/v1/ocr-jobs/{pending.job_id}").json()
        assert job["status"] == "failed" and job["error_code"] == "interrupted"
    assert service.enqueue(document["document_id"]).ingestion_id != pending.ingestion_id
    with database.connect() as connection:
        # 중복 요청의 ingestion INSERT는 같은 트랜잭션에서 롤백되어야 한다.
        assert connection.execute("SELECT count(*) FROM ingestion").fetchone()[0] == 3


def test_missing_resources(setup):
    client = setup[0]
    assert client.post("/api/v1/documents/missing/ocr").status_code == 404
    assert client.get("/api/v1/ocr-jobs/missing").status_code == 404
    assert client.get("/api/v1/documents/missing/pages").status_code == 404
    assert client.get("/api/v1/documents/missing/pages/missing/image").status_code == 404
    assert client.get("/api/v1/documents/missing/ocr/latest").status_code == 404


def test_latest_job_restores_state_and_retains_successful_pages(setup):
    client, _, engine, document, database, _ = setup
    prefix = f"/api/v1/documents/{document['document_id']}"
    assert client.get(prefix + "/ocr/latest").json() is None
    first = client.post(prefix + "/ocr").json()
    assert client.get(prefix + "/ocr/latest").json()["status"] == "completed"
    engine.fail_on = 1
    second = client.post(prefix + "/ocr").json()
    # 초 단위 생성 시각이 같아도 마지막에 생성된 실패 작업을 반환해야 한다.
    with database.connect() as connection:
        connection.execute("UPDATE ocr_job SET created_at = '2026-10-03 00:00:00'")
    latest = client.get(prefix + "/ocr/latest").json()
    assert latest["job_id"] == second["job_id"]
    assert latest["status"] == "failed"
    assert client.get(prefix).json()["ingestion_id"] == first["ingestion_id"]
    assert len(client.get(prefix + "/pages").json()) == 2


def test_renderer_rejects_excessive_pixels(tmp_path):
    source = tmp_path / "scan.pdf"
    source.write_bytes(scanned_pdf())
    with pytest.raises(ValueError, match="픽셀"):
        list(PyMuPdfRenderer(150, max_pixels=100).render(source, tmp_path / "rendered"))


@pytest.mark.parametrize("score", [0.98, 0.65, 0.2])
def test_paddle_response_contract(score):
    class Predictor:
        def predict(self, path):
            return [
                {
                    "rec_texts": ["test"],
                    "rec_scores": [score],
                    "rec_polys": [[[10, 20], [80, 20], [80, 40], [10, 40]]],
                }
            ]

    engine = PaddleOcrEngine()
    engine._engine = Predictor()
    page = RenderedPage(
        number=1, image_path=Path("fixture.png"), width=100, height=200, dpi=72, rotation=0
    )
    line = engine.recognize(page)[0]
    assert line.bbox == (0.1, 0.1, 0.8, 0.2)
    assert line.confidence == score


def test_ocr_box_validation():
    for bbox in [(0, 0, 2, 1), (0.5, 0, 0.1, 1), (0, 0, float("nan"), 1)]:
        with pytest.raises(ValueError):
            OcrLine(text="test", bbox=bbox, confidence=0.9)


def test_missing_engine_does_not_enqueue(setup):
    client, service, _, document, database, _ = setup

    class UnavailableEngine:
        @property
        def version(self):
            raise OcrUnavailable("OCR 패키지가 없습니다.")

    service.engine = UnavailableEngine()
    assert client.post(f"/api/v1/documents/{document['document_id']}/ocr").status_code == 503
    with database.connect() as connection:
        assert connection.execute("SELECT count(*) FROM ocr_job").fetchone()[0] == 0


def test_block_save_failure_rolls_back_page_and_progress(setup):
    client, _, _, document, database, _ = setup
    # 페이지 INSERT 후 Block INSERT를 실패시켜 같은 페이지의 부분 저장을 막는지 확인한다.
    with database.connect() as connection:
        connection.execute("""CREATE TRIGGER fail_block BEFORE INSERT ON document_block
                              BEGIN SELECT RAISE(ABORT, 'fixture error'); END""")
    response = client.post(f"/api/v1/documents/{document['document_id']}/ocr")
    job = client.get(response.headers["Location"]).json()
    assert job["status"] == "failed" and job["completed_pages"] == 0
    with database.connect() as connection:
        assert connection.execute("SELECT count(*) FROM page").fetchone()[0] == 0


def test_incomplete_job_cannot_be_published(setup):
    _, service, _, document, _, _ = setup
    job = service.enqueue(document["document_id"])
    service.repository.start(job.job_id)
    with pytest.raises(ValueError, match="모든 페이지"):
        service.repository.complete(job)
    assert service.repository.get_job(job.job_id).status == "running"
    assert (
        service.documents.get_document(document["document_id"]).ingestion_id
        == document["ingestion_id"]
    )
