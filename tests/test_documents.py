# [읽기 안내] 정상 업로드뿐 아니라 중복·재시작·동시 요청·저장 실패를 검증한다.
# pytest의 settings fixture는 테스트마다 격리된 임시 폴더와 DB를 만든다.
# assert는 기대 결과이며 monkeypatch/트리거는 실제로 발생하기 어려운 실패를 재현한다.
import hashlib
import io
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Barrier

import pymupdf
import pytest
from fastapi.testclient import TestClient

from handover_ai.adapters.document_repository import SQLiteDocumentRepository
from handover_ai.adapters.pdf import PyMuPdfInspector
from handover_ai.adapters.sqlite import SQLiteDatabase
from handover_ai.config import load_settings
from handover_ai.main import create_app
from handover_ai.services.documents import DocumentService


def make_pdf(pages=2, encrypted=False):
    # 외부 샘플 다운로드 없이 실제 파서로 읽을 수 있는 PDF를 만든다.
    with pymupdf.open() as pdf:
        for _ in range(pages):
            pdf.new_page()
        if encrypted:
            return pdf.tobytes(
                encryption=pymupdf.PDF_ENCRYPT_AES_256, owner_pw="owner", user_pw="secret"
            )
        return pdf.tobytes()


@pytest.fixture
def settings(tmp_path):
    return replace(
        load_settings(),
        database_path=tmp_path / "test.db",
        document_store_path=tmp_path / "documents",
        max_upload_bytes=1024 * 1024,
    )


def upload(client, content, name="handover.pdf"):
    return client.post("/api/v1/documents", files={"file": (name, content, "application/pdf")})


def test_registration_duplicate_and_restart(settings):
    content = make_pdf()
    with TestClient(create_app(settings=settings)) as client:
        response = upload(client, content, "../../handover.pdf")
        assert response.status_code == 201
        body = response.json()
        document = body["document"]
        assert body["duplicate"] is False
        assert document["file_name"] == "handover.pdf"
        assert document["page_count"] == 2
        assert document["status"] == "pending"
        assert document["file_sha256"] == hashlib.sha256(content).hexdigest()
        assert "source_uri" not in document
        location = response.headers["Location"]
        duplicate = upload(client, content, "another-name.pdf")
        assert duplicate.status_code == 200
        assert duplicate.json() == {"document": document, "duplicate": True}

    # 캐시가 아닌 실제 SQLite/원본 저장 결과를 새 앱 인스턴스에서 읽는다.
    with TestClient(create_app(settings=settings)) as client:
        assert client.get(location).json() == document
        assert client.get("/api/v1/documents").json() == [document]
        assert client.get("/api/v1/documents?offset=1").json() == []
        assert client.get("/api/v1/documents/missing").status_code == 404
        assert client.get("/api/v1/documents?limit=101").status_code == 422
        assert client.get("/api/v1/documents?offset=-1").status_code == 422
    files = list(settings.document_store_path.iterdir())
    assert len(files) == 1
    assert files[0].read_bytes() == content
    with SQLiteDatabase(settings.database_path).connect() as connection:
        assert connection.execute("SELECT count(*) FROM ingestion").fetchone()[0] == 1


@pytest.mark.parametrize(
    ("content", "filename"),
    [
        (b"", "empty.pdf"),
        (b"not a pdf", "fake.pdf"),
        (b"%PDF-1.7\nbroken", "bad.pdf"),
        (b"hello", "notes.txt"),
    ],
)
def test_rejects_invalid_files_without_persisting(settings, content, filename):
    with TestClient(create_app(settings=settings)) as client:
        assert upload(client, content, filename).status_code == 422
        assert client.get("/api/v1/documents").json() == []
    assert list(settings.document_store_path.glob("*")) == []


def test_rejects_encryption_and_size_limit(settings):
    with TestClient(create_app(settings=settings)) as client:
        assert upload(client, make_pdf(encrypted=True)).status_code == 422
        assert upload(client, b"x" * (settings.max_upload_bytes + 1)).status_code == 413
        assert client.get("/api/v1/documents").json() == []
    assert list(settings.document_store_path.iterdir()) == []


def test_same_name_different_contents_and_pagination(settings):
    with TestClient(create_app(settings=settings)) as client:
        first = upload(client, make_pdf(1)).json()["document"]
        second = upload(client, make_pdf(3)).json()["document"]
        assert first["document_id"] != second["document_id"]
        assert client.get("/api/v1/documents?limit=1").json() == [second]
        assert client.get("/api/v1/documents?limit=1&offset=1").json() == [first]
    assert len(list(settings.document_store_path.iterdir())) == 2


def test_database_failure_rolls_back_both_rows_and_removes_file(settings):
    database = SQLiteDatabase(settings.database_path)
    database.initialize()
    # ingestion 삽입 후 document 삽입에서 실패시켜 실제 트랜잭션 롤백을 검증한다.
    with database.connect() as connection:
        connection.execute("""CREATE TRIGGER fail_insert BEFORE INSERT ON document
                            BEGIN SELECT RAISE(ABORT, 'injected failure'); END""")
    service = DocumentService(
        SQLiteDocumentRepository(database),
        PyMuPdfInspector(),
        settings.document_store_path,
        settings.max_upload_bytes,
    )
    with pytest.raises(sqlite3.IntegrityError, match="injected failure"):
        service.register("valid.pdf", io.BytesIO(make_pdf()))
    with database.connect() as connection:
        for table in ("document", "ingestion"):
            assert connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
    assert list(settings.document_store_path.iterdir()) == []


def test_concurrent_duplicate_registration(settings):
    database = SQLiteDatabase(settings.database_path)
    database.initialize()
    barrier = Barrier(2)

    class RacingRepository(SQLiteDocumentRepository):
        first_lookup = True

        def get_by_hash(self, file_sha256):
            result = super().get_by_hash(file_sha256)
            if self.first_lookup:
                self.first_lookup = False
                # 두 요청의 사전 조회를 모두 미등록으로 맞춰 UNIQUE 제약 충돌을 재현한다.
                barrier.wait(timeout=10)
            return result

    content = make_pdf()

    def register():
        service = DocumentService(
            RacingRepository(database),
            PyMuPdfInspector(),
            settings.document_store_path,
            settings.max_upload_bytes,
        )
        return service.register("same.pdf", io.BytesIO(content))

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(register) for _ in range(2)]
        results = [future.result(timeout=15) for future in futures]
    assert sorted(result.duplicate for result in results) == [False, True]
    assert results[0].document.document_id == results[1].document.document_id
    assert len(list(settings.document_store_path.iterdir())) == 1
    with database.connect() as connection:
        assert connection.execute("SELECT count(*) FROM document").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM ingestion").fetchone()[0] == 1


def test_file_move_failure_does_not_create_database_rows(settings, monkeypatch):
    database = SQLiteDatabase(settings.database_path)
    database.initialize()

    def fail_move(self, target):
        raise OSError("injected disk failure")

    monkeypatch.setattr(Path, "replace", fail_move)
    service = DocumentService(
        SQLiteDocumentRepository(database),
        PyMuPdfInspector(),
        settings.document_store_path,
        settings.max_upload_bytes,
    )
    with pytest.raises(OSError, match="injected disk failure"):
        service.register("valid.pdf", io.BytesIO(make_pdf()))
    assert service.repository.list_documents(20, 0) == []
    assert list(settings.document_store_path.iterdir()) == []
