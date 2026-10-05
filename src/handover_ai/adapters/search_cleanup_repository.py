"""검색에 사용하지 않는 실행의 벡터만 정리한다. 원문·멤버십·실행 이력은 보존한다."""

from handover_ai.domain.search import SearchConflict


class SQLiteSearchCleanupRepository:
    def __init__(self, database):
        self.database = database

    @staticmethod
    def document_exists(connection, document_id):
        if not connection.execute(
            "SELECT 1 FROM document WHERE document_id=?", (document_id,)
        ).fetchone():
            raise KeyError(document_id)

    @staticmethod
    def selection():
        # 현재 OCR의 공개 포인터는 모델 버전/정리 방식과 무관하게 전부 보호한다.
        # 과거 OCR은 현재 검색에 쓰일 수 없으므로 포인터가 남아 있어도 정리 가능하다.
        return (
            "SELECT r.*,s.parser_version,c.status AS cleanup_status,c.error_code AS cleanup_error,"
            "CASE WHEN s.ingestion_id<>d.ingestion_id THEN 'old_ocr' "
            "WHEN r.status='failed' THEN 'failed_run' ELSE 'replaced_run' END AS reason "
            "FROM search_run r JOIN structure_run s USING(structure_id) "
            "JOIN document d ON d.document_id=r.document_id "
            "LEFT JOIN search_cleanup c USING(run_id) WHERE r.document_id=? "
            "AND r.status IN ('ready','failed') "
            "AND NOT EXISTS(SELECT 1 FROM search_active a WHERE a.run_id=r.run_id "
            "AND s.ingestion_id=d.ingestion_id) "
        )

    def candidates(self, document_id, limit, offset):
        with self.database.connect() as connection:
            self.document_exists(connection, document_id)
            rows = connection.execute(
                self.selection() + "AND (c.status IS NULL OR c.status='failed') "
                "ORDER BY r.rowid LIMIT ? OFFSET ?",
                (document_id, limit, offset),
            ).fetchall()
            return [dict(row) for row in rows]

    def claim(self, document_id, run_id):
        with self.database.connect() as connection:
            # 후보 조회와 실행 사이에 상태가 바뀔 수 있다. 쓰기 잠금 안에서 다시 검사하고
            # 삭제 권한을 선점한다. 공개 가능한 queued/running 실행은 절대로 선점하지 않는다.
            connection.execute("BEGIN IMMEDIATE")
            self.document_exists(connection, document_id)
            run = connection.execute(
                self.selection() + "AND r.run_id=?",
                (document_id, run_id),
            ).fetchone()
            if not run:
                raise SearchConflict(
                    "현재 사용 중이거나 작업 중인 색인, 다른 문서의 색인은 정리할 수 없습니다."
                )
            if run["cleanup_status"] == "deleted":
                return "deleted"
            if run["cleanup_status"] == "deleting":
                return "busy"
            connection.execute(
                "INSERT INTO search_cleanup(run_id,status) VALUES (?,'deleting') "
                "ON CONFLICT(run_id) DO UPDATE SET status='deleting',error_code=NULL,"
                "attempts=attempts+1,updated_at=CURRENT_TIMESTAMP",
                (run_id,),
            )
            # 과거 OCR 포인터만 여기까지 도달한다. 이력/Chunk를 지우지 않고 검색 포인터만
            # 제거한다. ready/failed 실행은 이후 다시 publish될 수 없어 삭제 중 되살아나지 않는다.
            connection.execute("DELETE FROM search_active WHERE run_id=?", (run_id,))
            return "claimed"

    def finish(self, run_id, *, success):
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE search_cleanup SET status=?,error_code=?,updated_at=CURRENT_TIMESTAMP "
                "WHERE run_id=? AND status='deleting'",
                ("deleted" if success else "failed", None if success else "delete_failed", run_id),
            )
