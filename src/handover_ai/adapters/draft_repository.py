"""초안과 생성 이력을 함께 저장한다. 완료 직전에 OCR·검토 버전을 다시 확인한다."""

import json
import sqlite3
from uuid import uuid4

from handover_ai.adapters.chunk_repository import SQLiteChunkRepository
from handover_ai.adapters.layout_review_repository import SQLiteLayoutReviewRepository
from handover_ai.adapters.search_repository import SQLiteSearchRepository
from handover_ai.domain.drafts import PROMPT_VERSION, SCHEMA_VERSION, DraftFailure
from handover_ai.domain.search import SearchConflict


class SQLiteDraftRepository:
    def __init__(self, database):
        self.database = database

    @staticmethod
    def view(row):
        value = dict(row)
        value["request"] = json.loads(value.pop("request_json"))
        encoded = value.pop("result_json")
        value["result"] = json.loads(encoded) if encoded else None
        return value

    def enqueue(self, document_id, request, model_id):
        draft_id, run_id = "DRAFT-" + uuid4().hex, "GEN-" + uuid4().hex
        try:
            with self.database.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                SQLiteSearchRepository.current(connection, document_id, request.structure_id)
                connection.execute(
                    "INSERT INTO generation_run(generation_run_id,role,provider,model_id,"
                    "prompt_version,schema_version,status) "
                    "VALUES (?,'excerpt_draft','ollama',?,?,?,'queued')",
                    (run_id, model_id, PROMPT_VERSION, SCHEMA_VERSION),
                )
                connection.execute(
                    "INSERT INTO excerpt_draft(draft_id,generation_run_id,document_id,structure_id,"
                    "request_json,status) VALUES (?,?,?,?,?,'queued')",
                    (
                        draft_id,
                        run_id,
                        document_id,
                        request.structure_id,
                        request.model_dump_json(),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise SearchConflict("이 문서의 초안을 이미 생성 중입니다.") from exc
        return self.get(document_id, draft_id)

    def get(self, document_id, draft_id):
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM excerpt_draft WHERE document_id=? AND draft_id=?",
                (document_id, draft_id),
            ).fetchone()
            if not row:
                raise KeyError(draft_id)
            return self.view(row)

    def list(self, document_id, limit, offset):
        with self.database.connect() as connection:
            if not connection.execute(
                "SELECT 1 FROM document WHERE document_id=?", (document_id,)
            ).fetchone():
                raise KeyError(document_id)
            return [
                self.view(row)
                for row in connection.execute(
                    "SELECT * FROM excerpt_draft WHERE document_id=? "
                    "ORDER BY rowid DESC LIMIT ? OFFSET ?",
                    (document_id, limit, offset),
                ).fetchall()
            ]

    def start(self, job):
        with self.database.connect() as connection:
            changed = connection.execute(
                "UPDATE excerpt_draft SET status='running' WHERE draft_id=? AND status='queued'",
                (job["draft_id"],),
            ).rowcount
            if changed:
                connection.execute(
                    "UPDATE generation_run SET status='running' WHERE generation_run_id=?",
                    (job["generation_run_id"],),
                )
            return bool(changed)

    def finish(self, job, result, metadata):
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            SQLiteSearchRepository.current(connection, job["document_id"], job["structure_id"])
            # 검색 이후 모델 응답을 기다리는 동안 재처리나 사람 검토가 달라졌다면
            # 이전 판단으로 초안을 공개하지 않는다. 과거 완료 초안 자체는 그대로 남긴다.
            for item in result["items"]:
                row = connection.execute(
                    SQLiteChunkRepository._select() + " WHERE c.chunk_id=? AND r.structure_id=?",
                    (item["chunk_id"], job["structure_id"]),
                ).fetchone()
                if not row or row["text_hash"] != item["text_hash"]:
                    raise DraftFailure("source_changed")
                if item["human_review"] is not None:
                    latest = SQLiteLayoutReviewRepository.latest(
                        connection, job["structure_id"], row["page_id"]
                    )
                    if latest.revision != item["human_review"]["revision"]:
                        raise DraftFailure("review_changed")
            updated = connection.execute(
                "UPDATE excerpt_draft SET status='ready',result_json=?,"
                "finished_at=CURRENT_TIMESTAMP "
                "WHERE draft_id=? AND status='running'",
                (json.dumps(result, ensure_ascii=False), job["draft_id"]),
            ).rowcount
            if not updated:
                raise DraftFailure("interrupted")
            connection.execute(
                "UPDATE generation_run SET status='ready',metadata_json=? "
                "WHERE generation_run_id=?",
                (json.dumps(metadata), job["generation_run_id"]),
            )

    def fail(self, job, code, metadata):
        with self.database.connect() as connection:
            changed = connection.execute(
                "UPDATE excerpt_draft SET status='failed',error_code=?,"
                "finished_at=CURRENT_TIMESTAMP "
                "WHERE draft_id=? AND status IN ('queued','running')",
                (code, job["draft_id"]),
            ).rowcount
            if changed:
                connection.execute(
                    "UPDATE generation_run SET status='failed',metadata_json=? "
                    "WHERE generation_run_id=?",
                    (json.dumps({**metadata, "error_code": code}), job["generation_run_id"]),
                )

    def recover_interrupted(self):
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE generation_run SET status='failed',metadata_json=? "
                "WHERE generation_run_id IN (SELECT generation_run_id FROM excerpt_draft "
                "WHERE status IN ('queued','running'))",
                (json.dumps({"error_code": "interrupted"}),),
            )
            connection.execute(
                "UPDATE excerpt_draft SET status='failed',error_code='interrupted',"
                "finished_at=CURRENT_TIMESTAMP "
                "WHERE status IN ('queued','running')"
            )
