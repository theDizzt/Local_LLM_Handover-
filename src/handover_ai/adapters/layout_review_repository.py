"""버전에 묶인 검토 이력을 추가 저장한다. 현재 상태는 가장 큰 revision이다."""

from handover_ai.adapters.sqlite import SQLiteDatabase
from handover_ai.domain.chunks import StructureConflict
from handover_ai.domain.layout_review import PageReview, ReviewRequest, ReviewSummary


class SQLiteLayoutReviewRepository:
    def __init__(self, database: SQLiteDatabase):
        self.database = database

    @staticmethod
    def validate_scope(connection, document_id, structure_id, page_id=None, *, write=False):
        row = connection.execute(
            "SELECT r.ingestion_id,d.ingestion_id AS current_ingestion,i.status "
            "FROM structure_run r JOIN document d USING(document_id) "
            "JOIN ingestion i ON i.ingestion_id=r.ingestion_id "
            "WHERE r.structure_id=? AND r.document_id=? "
            "AND EXISTS(SELECT 1 FROM layout_page l WHERE l.structure_id=r.structure_id "
            "AND (? IS NULL OR l.page_id=?))",
            (structure_id, document_id, page_id, page_id),
        ).fetchone()
        if not row:
            raise KeyError("배치 분석 페이지를 찾을 수 없습니다.")
        if write and (row["ingestion_id"] != row["current_ingestion"] or row["status"] != "ready"):
            raise StructureConflict("OCR 버전이 변경되었습니다. 새 페이지를 검토해 주세요.")

    @staticmethod
    def latest(connection, structure_id, page_id) -> PageReview:
        row = connection.execute(
            "SELECT * FROM layout_review WHERE structure_id=? AND page_id=? "
            "ORDER BY revision DESC LIMIT 1",
            (structure_id, page_id),
        ).fetchone()
        return (
            PageReview(**dict(row))
            if row
            else PageReview(structure_id=structure_id, page_id=page_id)
        )

    def get(self, document_id, structure_id, page_id) -> PageReview:
        with self.database.connect() as connection:
            self.validate_scope(connection, document_id, structure_id, page_id)
            return self.latest(connection, structure_id, page_id)

    def save(self, document_id, structure_id, page_id, request: ReviewRequest) -> PageReview:
        with self.database.connect() as connection:
            # 현재 OCR 확인·revision 비교·새 이력 INSERT를 하나의 쓰기 잠금 안에서
            # 수행한다. 재처리 완료나 다른 검토 저장이 끼어드는 경쟁 조건을 막는다.
            connection.execute("BEGIN IMMEDIATE")
            self.validate_scope(connection, document_id, structure_id, page_id, write=True)
            current = self.latest(connection, structure_id, page_id)
            if current.revision != request.expected_revision:
                raise StructureConflict(
                    "다른 검토가 먼저 저장되었습니다. 최신 상태를 불러와 주세요."
                )
            connection.execute(
                "INSERT INTO layout_review(structure_id,page_id,revision,status,note,"
                "text_checked,order_checked,regions_checked) VALUES (?,?,?,?,?,?,?,?)",
                (
                    structure_id,
                    page_id,
                    current.revision + 1,
                    request.status,
                    request.note,
                    request.text_checked,
                    request.order_checked,
                    request.regions_checked,
                ),
            )
            return self.latest(connection, structure_id, page_id)

    def history(self, document_id, structure_id, page_id, limit, offset) -> list[PageReview]:
        with self.database.connect() as connection:
            self.validate_scope(connection, document_id, structure_id, page_id)
            rows = connection.execute(
                "SELECT * FROM layout_review WHERE structure_id=? AND page_id=? "
                "ORDER BY revision DESC LIMIT ? OFFSET ?",
                (structure_id, page_id, limit, offset),
            ).fetchall()
            return [PageReview(**dict(row)) for row in rows]

    def summary(self, document_id, structure_id) -> ReviewSummary:
        with self.database.connect() as connection:
            self.validate_scope(connection, document_id, structure_id)
            rows = connection.execute(
                "SELECT coalesce(r.status,'unreviewed') AS status,count(*) AS count "
                "FROM layout_page l LEFT JOIN layout_review r ON r.structure_id=l.structure_id "
                "AND r.page_id=l.page_id AND r.revision=(SELECT max(h.revision) "
                "FROM layout_review h WHERE h.structure_id=l.structure_id AND h.page_id=l.page_id) "
                "WHERE l.structure_id=? GROUP BY coalesce(r.status,'unreviewed')",
                (structure_id,),
            ).fetchall()
            counts = {row["status"]: row["count"] for row in rows}
            return ReviewSummary(
                structure_id=structure_id,
                total=sum(counts.values()),
                **{
                    key: counts.get(key, 0)
                    for key in ("confirmed", "needs_correction", "unreviewed")
                },
            )
