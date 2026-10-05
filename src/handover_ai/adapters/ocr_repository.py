# [읽기 안내] OCR 작업/페이지/Block의 영속 저장을 맡는다. 각 메서드의 with가 트랜잭션 경계다.
# 페이지+Block+진행률은 함께 저장하고, 완료 시에는 작업+ingestion+문서 포인터를 함께 바꾼다.
# 실패한 버전의 부분 결과는 보관하되 ready 필터를 통해 공개 조회에서 제외한다.
import json
import sqlite3
from pathlib import Path
from uuid import uuid4

from handover_ai.adapters.sqlite import SQLiteDatabase
from handover_ai.domain.ocr import OcrBlock, OcrConflict, OcrJob, OcrLine, OcrPage, RenderedPage


class SQLiteOcrRepository:
    def __init__(self, database: SQLiteDatabase):
        self.database = database

    def create_job(self, document_id: str, engine: str, renderer: str) -> OcrJob:
        job_id, ingestion_id = f"OCR-{uuid4().hex}", f"ING-{uuid4().hex}"
        try:
            with self.database.connect() as connection:
                # 쓰기 예약 후 문서를 확인하여 동시에 두 작업이 등록되는 경쟁을 막는다.
                connection.execute("BEGIN IMMEDIATE")
                document = connection.execute(
                    "SELECT page_count FROM document WHERE document_id = ?", (document_id,)
                ).fetchone()
                if document is None:
                    raise KeyError(document_id)
                connection.execute(
                    "INSERT INTO ingestion (ingestion_id, ocr_version, parser_version, status) "
                    "VALUES (?, ?, ?, 'pending')",
                    (ingestion_id, engine, renderer),
                )
                connection.execute(
                    "INSERT INTO ocr_job(job_id, document_id, ingestion_id, status, total_pages) "
                    "VALUES (?, ?, ?, 'queued', ?)",
                    (job_id, document_id, ingestion_id, document["page_count"]),
                )
        except sqlite3.IntegrityError as exc:
            if exc.sqlite_errorname == "SQLITE_CONSTRAINT_UNIQUE":
                raise OcrConflict("이미 이 문서의 OCR 작업이 진행 중입니다.") from exc
            raise
        return self.get_job(job_id)

    def get_job(self, job_id: str) -> OcrJob | None:
        with self.database.connect() as connection:
            row = connection.execute("SELECT * FROM ocr_job WHERE job_id = ?", (job_id,)).fetchone()
        return OcrJob.model_validate(dict(row)) if row else None

    def start(self, job_id: str) -> bool:
        # 조건부 UPDATE의 rowcount가 1일 때만 이 호출자가 실행 권한을 얻는다.
        with self.database.connect() as connection:
            return (
                connection.execute(
                    "UPDATE ocr_job SET status = 'running' WHERE job_id = ? AND status = 'queued'",
                    (job_id,),
                ).rowcount
                == 1
            )

    def latest_job(self, document_id: str) -> OcrJob | None:
        # created_at은 초 단위이므로 같은 초에 재실행한 작업도 구분해야 한다.
        # 삭제하지 않는 작업 테이블의 rowid를 동률 정렬에 사용한다. UUID의 문자열
        # 순서는 생성 순서가 아니므로 job_id로 최신 작업을 고르면 안 된다.
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM ocr_job WHERE document_id = ? "
                "ORDER BY created_at DESC, rowid DESC LIMIT 1",
                (document_id,),
            ).fetchone()
        return OcrJob.model_validate(dict(row)) if row else None

    def save_page(self, job: OcrJob, page: RenderedPage, lines: list[OcrLine]) -> None:
        page_id = f"PAGE-{uuid4().hex}"
        with self.database.connect() as connection:
            connection.execute(
                """INSERT INTO page(page_id, document_id, ingestion_id, pdf_page_number,
                   image_uri, width_px, height_px, render_dpi, rotation)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    page_id,
                    job.document_id,
                    job.ingestion_id,
                    page.number,
                    str(page.image_path),
                    page.width,
                    page.height,
                    page.dpi,
                    page.rotation,
                ),
            )
            for order, line in enumerate(lines):
                # 아직 Layout 분석을 하지 않았으므로 문단 의미를 추정하지 않는다.
                # OCR 순서를 보존하되 구조 신뢰도는 0으로 별도 표시한다.
                connection.execute(
                    """INSERT INTO document_block(block_id, page_id, block_type, reading_order,
                       text, bbox_json, ocr_confidence, structure_confidence)
                       VALUES (?, ?, 'paragraph', ?, ?, ?, ?, 0)""",
                    (
                        f"BL-{uuid4().hex}",
                        page_id,
                        order,
                        line.text,
                        json.dumps(line.bbox),
                        line.confidence,
                    ),
                )
            connection.execute(
                "UPDATE ocr_job SET completed_pages = completed_pages + 1 WHERE job_id = ?",
                (job.job_id,),
            )

    def complete(self, job: OcrJob) -> None:
        with self.database.connect() as connection:
            cursor = connection.execute(
                """UPDATE ocr_job SET status = 'completed', finished_at = CURRENT_TIMESTAMP
                   WHERE job_id = ? AND status = 'running' AND completed_pages = total_pages""",
                (job.job_id,),
            )
            if cursor.rowcount != 1:
                raise ValueError("모든 페이지가 저장된 실행 중 작업만 완료할 수 있습니다.")
            connection.execute(
                "UPDATE ingestion SET status = 'ready' WHERE ingestion_id = ?", (job.ingestion_id,)
            )
            # 성공했을 때만 현재 버전을 교체한다. 기존 Page/Block은 과거 근거로 남긴다.
            connection.execute(
                "UPDATE document SET ingestion_id = ? WHERE document_id = ?",
                (job.ingestion_id, job.document_id),
            )
            connection.execute(
                "UPDATE chunk SET index_status = 'failed' WHERE document_id = ? "
                "AND ingestion_id != ?",
                (job.document_id, job.ingestion_id),
            )

    def fail(self, job: OcrJob, error_code: str) -> None:
        # 현재 문서가 가리키는 성공 버전은 변경하지 않는다. 실패는 이 작업 버전에만 남긴다.
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE ocr_job SET status = 'failed', error_code = ?, "
                "finished_at = CURRENT_TIMESTAMP WHERE job_id = ? "
                "AND status IN ('queued', 'running')",
                (error_code, job.job_id),
            )
            connection.execute(
                "UPDATE ingestion SET status = 'failed' WHERE ingestion_id = ? "
                "AND status = 'pending'",
                (job.ingestion_id,),
            )

    def recover_interrupted(self) -> None:
        # MVP는 단일 서버 프로세스다. 재시작 시 남은 작업을 실패로 확정하여
        # 영구적인 처리 중 상태를 없애고 사용자가 새 버전으로 재시도할 수 있게 한다.
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE ingestion SET status = 'failed' WHERE ingestion_id IN "
                "(SELECT ingestion_id FROM ocr_job WHERE status IN ('queued', 'running'))"
            )
            connection.execute(
                "UPDATE ocr_job SET status = 'failed', error_code = 'interrupted', "
                "finished_at = CURRENT_TIMESTAMP WHERE status IN ('queued', 'running')"
            )

    def pages(self, document_id: str, ingestion_id: str, limit: int, offset: int) -> list[OcrPage]:
        # document_id도 함께 필터링하여 다른 문서의 처리 ID로 조회할 수 없게 한다.
        with self.database.connect() as connection:
            rows = connection.execute(
                """SELECT p.* FROM page p JOIN ingestion i USING (ingestion_id)
                   WHERE p.document_id = ? AND p.ingestion_id = ? AND i.status = 'ready'
                   ORDER BY p.pdf_page_number LIMIT ? OFFSET ?""",
                (document_id, ingestion_id, limit, offset),
            ).fetchall()
            pages = []
            for row in rows:
                blocks = connection.execute(
                    "SELECT * FROM document_block WHERE page_id = ? ORDER BY reading_order",
                    (row["page_id"],),
                ).fetchall()
                pages.append(
                    OcrPage(
                        **dict(row),
                        blocks=[
                            OcrBlock(
                                block_id=b["block_id"],
                                reading_order=b["reading_order"],
                                text=b["text"],
                                bbox=json.loads(b["bbox_json"]),
                                confidence=b["ocr_confidence"],
                            )
                            for b in blocks
                        ],
                    )
                )
        return pages

    def image_path(self, document_id: str, page_id: str) -> Path | None:
        # 사용자에게 파일 경로를 받지 않고 DB가 보유한 경로만 조회한다.
        with self.database.connect() as connection:
            row = connection.execute(
                """SELECT p.image_uri FROM page p JOIN ingestion i USING (ingestion_id)
                   WHERE p.document_id = ? AND p.page_id = ? AND i.status = 'ready'""",
                (document_id, page_id),
            ).fetchone()
        return Path(row["image_uri"]) if row else None
