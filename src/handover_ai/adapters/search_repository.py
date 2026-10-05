"""벡터 저장은 재생성 가능하다. 검색 공개 여부와 근거의 진실은 SQLite가 결정한다."""

import json
import sqlite3
from uuid import uuid4

from handover_ai.adapters.chunk_repository import SQLiteChunkRepository
from handover_ai.adapters.layout_review_repository import SQLiteLayoutReviewRepository
from handover_ai.domain.search import SearchConflict


class SQLiteSearchRepository:
    def __init__(self, database):
        self.database = database

    @staticmethod
    def current(connection, document_id, structure_id):
        if not connection.execute(
            "SELECT 1 FROM document WHERE document_id=?", (document_id,)
        ).fetchone():
            raise KeyError(document_id)
        row = connection.execute(
            "SELECT r.* FROM structure_run r JOIN document d USING(document_id) "
            "JOIN ingestion i ON i.ingestion_id=d.ingestion_id "
            "WHERE r.structure_id=? AND r.document_id=? "
            "AND r.ingestion_id=d.ingestion_id AND i.status='ready'",
            (structure_id, document_id),
        ).fetchone()
        if not row:
            raise SearchConflict("현재 OCR 버전의 근거를 먼저 준비해 주세요.")
        return row

    def enqueue(self, document_id, structure_id, version):
        try:
            with self.database.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                structure = self.current(connection, document_id, structure_id)
                if not structure["chunk_count"]:
                    raise SearchConflict("검색할 텍스트가 없습니다.")
                run_id = "idx-" + uuid4().hex
                connection.execute(
                    "INSERT INTO search_run(run_id,document_id,structure_id,index_version,"
                    "status,total) VALUES (?,?,?,?,'queued',?)",
                    (run_id, document_id, structure_id, version, structure["chunk_count"]),
                )
                return dict(
                    connection.execute(
                        "SELECT * FROM search_run WHERE run_id=?", (run_id,)
                    ).fetchone()
                )
        except sqlite3.IntegrityError as exc:
            raise SearchConflict("이미 이 문서의 색인을 준비하고 있습니다.") from exc

    def recover_interrupted(self):
        # OCR과 같은 단일 서버 프로세스 운용이다. 완성된 기존 색인은 유지한다.
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE search_cleanup SET status='failed',error_code='interrupted',"
                "updated_at=CURRENT_TIMESTAMP WHERE status='deleting'"
            )
            connection.execute(
                "UPDATE search_run SET status='failed',error_code='interrupted' "
                "WHERE status IN ('queued','running')"
            )

    def start(self, run_id):
        with self.database.connect() as connection:
            return (
                connection.execute(
                    "UPDATE search_run SET status='running' WHERE run_id=? AND status='queued'",
                    (run_id,),
                ).rowcount
                == 1
            )

    def batch(self, structure_id, offset):
        with self.database.connect() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    "SELECT c.chunk_id,c.text,c.text_hash FROM chunk c JOIN structure_section s "
                    "USING(section_id) WHERE s.structure_id=? "
                    "ORDER BY c.chunk_order LIMIT 32 OFFSET ?",
                    (structure_id, offset),
                ).fetchall()
            ]

    def progress(self, run_id, rows):
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if not connection.execute(
                "SELECT 1 FROM search_run WHERE run_id=? AND status='running'", (run_id,)
            ).fetchone():
                raise SearchConflict("중단된 작업입니다.")
            connection.executemany(
                "INSERT INTO search_member VALUES (?,?,?)",
                [(run_id, row["chunk_id"], row["text_hash"]) for row in rows],
            )
            connection.execute(
                "UPDATE search_run SET completed=completed+? WHERE run_id=?", (len(rows), run_id)
            )

    def finish(self, run):
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self.current(connection, run["document_id"], run["structure_id"])
            changed = connection.execute(
                "UPDATE search_run SET status='ready' WHERE run_id=? "
                "AND status='running' AND completed=total",
                (run["run_id"],),
            ).rowcount
            if not changed:
                raise SearchConflict("완료되지 않은 색인은 공개할 수 없습니다.")
            # 이 포인터 교체 전까지 재구축 중인 컬렉션은 보이지 않는다. 실패하면 이전
            # 포인터를 유지하므로 정상 검색을 계속할 수 있다. 구조 버전별로 독립한다.
            connection.execute(
                "INSERT INTO search_active VALUES (?,?) ON CONFLICT(structure_id) "
                "DO UPDATE SET run_id=excluded.run_id",
                (run["structure_id"], run["run_id"]),
            )
            connection.execute(
                "UPDATE chunk SET index_status='ready' WHERE chunk_id IN "
                "(SELECT chunk_id FROM search_member WHERE run_id=?)",
                (run["run_id"],),
            )

    def fail(self, run_id, code):
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE search_run SET status='failed',error_code=? "
                "WHERE run_id=? AND status IN ('queued','running')",
                (code, run_id),
            )

    @staticmethod
    def active(connection, structure_id, version):
        row = connection.execute(
            "SELECT r.* FROM search_run r JOIN search_active a USING(run_id) "
            "WHERE a.structure_id=? AND r.index_version=? AND r.status='ready'",
            (structure_id, version),
        ).fetchone()
        return dict(row) if row else None

    def state(self, document_id, structure_id, version):
        with self.database.connect() as connection:
            self.current(connection, document_id, structure_id)
            latest = connection.execute(
                "SELECT * FROM search_run WHERE structure_id=? ORDER BY rowid DESC LIMIT 1",
                (structure_id,),
            ).fetchone()
            return {
                "latest": dict(latest) if latest else None,
                "active": self.active(connection, structure_id, version),
            }

    def resolve(self, run, matches):
        with self.database.connect() as connection:
            # 읽기 트랜잭션의 한 스냅샷에서 현재 OCR, 공개 포인터, 근거를 검증한다.
            # 벡터 검색 중 OCR 재처리/색인 교체가 끝났다면 오래된 응답을 거절한다.
            connection.execute("BEGIN")
            self.current(connection, run["document_id"], run["structure_id"])
            active = self.active(connection, run["structure_id"], run["index_version"])
            if not active or active["run_id"] != run["run_id"]:
                raise SearchConflict("색인이 변경되었습니다. 다시 검색해 주세요.")
            hits, seen = [], set()
            for chunk_id, distance in matches:
                if chunk_id in seen:
                    continue
                seen.add(chunk_id)
                row = connection.execute(
                    SQLiteChunkRepository._select()
                    + " JOIN search_member sm ON sm.chunk_id=c.chunk_id "
                    "AND sm.text_hash=c.text_hash WHERE sm.run_id=? AND c.chunk_id=? "
                    "AND r.structure_id=? AND c.document_id=?",
                    (run["run_id"], chunk_id, run["structure_id"], run["document_id"]),
                ).fetchone()
                if row:
                    layout = connection.execute(
                        "SELECT result_json FROM layout_page WHERE structure_id=? AND page_id=?",
                        (run["structure_id"], row["page_id"]),
                    ).fetchone()
                    chunk = SQLiteChunkRepository._view(connection, row)
                    reasons = set()
                    if layout:
                        details = json.loads(layout[0])
                        reasons.update(details.get("warnings", []))
                        block_ids = set(chunk.evidence.block_ids)
                        for region in details.get("regions", []):
                            if block_ids.intersection(region["block_ids"]):
                                reasons.update(region.get("review_reasons", []))
                    hits.append(
                        {
                            "distance": distance,
                            "chunk": chunk,
                            "review_required": bool(reasons),
                            "review_reasons": sorted(reasons),
                            # 검토 저장 후 임베딩을 다시 만들 필요가 없다. 검색 시점의
                            # SQLite 상태를 읽고 자동 경고는 그대로 반환한다.
                            "human_review": SQLiteLayoutReviewRepository.latest(
                                connection, run["structure_id"], row["page_id"]
                            )
                            if layout
                            else None,
                        }
                    )
            return hits
