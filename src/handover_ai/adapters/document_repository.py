import sqlite3

from handover_ai.adapters.sqlite import SQLiteDatabase
from handover_ai.domain.documents import DuplicateDocument, StoredDocument

# 처리 상태는 document에 복제하지 않고 ingestion에서 읽어 불일치를 방지한다.
DOCUMENT_SELECT = """
    SELECT d.*, i.status
    FROM document AS d JOIN ingestion AS i ON i.ingestion_id = d.ingestion_id
"""


class SQLiteDocumentRepository:
    def __init__(self, database: SQLiteDatabase):
        self.database = database

    def save_document(self, document: StoredDocument) -> None:
        try:
            # connect()는 정상 종료 시 commit, 예외 시 rollback한다.
            # ingestion과 document를 함께 저장하므로 고아 ingestion이 남지 않는다.
            with self.database.connect() as connection:
                connection.execute(
                    """INSERT INTO ingestion
                       (ingestion_id, ocr_version, parser_version, status)
                       VALUES (?, ?, ?, ?)""",
                    (document.ingestion_id, "not-run", "not-run", document.status),
                )
                connection.execute(
                    """INSERT INTO document
                       (document_id, file_name, page_count, file_sha256, source_uri,
                        document_version, ingestion_id, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        document.document_id,
                        document.file_name,
                        document.page_count,
                        document.file_sha256,
                        document.source_uri,
                        document.document_version,
                        document.ingestion_id,
                        document.created_at,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            # 사전 조회만으로는 동시 업로드를 막지 못한다. 기존 UNIQUE 제약을
            # 최종 방어선으로 사용하고, 실제 중복일 때만 중복 예외로 변환한다.
            if (
                exc.sqlite_errorname == "SQLITE_CONSTRAINT_UNIQUE"
                and self.get_by_hash(document.file_sha256) is not None
            ):
                raise DuplicateDocument from exc
            raise

    def get_document(self, document_id: str) -> StoredDocument | None:
        with self.database.connect() as connection:
            row = connection.execute(
                DOCUMENT_SELECT + " WHERE d.document_id = ?", (document_id,)
            ).fetchone()
        return StoredDocument.model_validate(dict(row)) if row else None

    def get_by_hash(self, file_sha256: str) -> StoredDocument | None:
        with self.database.connect() as connection:
            row = connection.execute(
                DOCUMENT_SELECT
                + " WHERE d.file_sha256 = ? ORDER BY d.document_version DESC LIMIT 1",
                (file_sha256,),
            ).fetchone()
        return StoredDocument.model_validate(dict(row)) if row else None

    def list_documents(self, limit: int, offset: int) -> list[StoredDocument]:
        with self.database.connect() as connection:
            rows = connection.execute(
                DOCUMENT_SELECT + " ORDER BY d.created_at DESC, d.document_id LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        return [StoredDocument.model_validate(dict(row)) for row in rows]
