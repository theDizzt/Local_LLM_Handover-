"""Section/Chunk/원문 연결을 한 트랜잭션으로 저장하고 원문 근거를 조립한다.

OCR 원문은 변경하지 않는다. 과거 Chunk 역시 삭제하지 않아 생성 문서가 예전 근거를
다시 열 수 있다. 기본 목록만 현재 OCR 버전을 사용하고, ID 상세 조회는 과거 버전도 허용한다.
"""

import json
import sqlite3

from handover_ai.adapters.sqlite import SQLiteDatabase
from handover_ai.domain.chunks import (
    PARSER_VERSION,
    ChunkDraft,
    ChunkView,
    StructureConflict,
    StructureSummary,
    stable_id,
)
from handover_ai.domain.layout import LAYOUT_VERSION, PageLayout
from handover_ai.domain.models import Evidence
from handover_ai.domain.ocr import OcrBlock, OcrPage


class SQLiteChunkRepository:
    def __init__(self, database: SQLiteDatabase, parser_version: str = PARSER_VERSION):
        self.database = database
        self.parser_version = parser_version

    def get_structure(self, document_id: str, ingestion_id: str) -> StructureSummary | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT r.* FROM structure_run r JOIN ingestion i USING (ingestion_id) "
                "WHERE r.document_id = ? AND r.ingestion_id = ? "
                "AND r.parser_version = ? AND i.status = 'ready'",
                (document_id, ingestion_id, self.parser_version),
            ).fetchone()
            return self._summary(connection, row) if row else None

    def save_structure(
        self,
        document_id: str,
        ingestion_id: str,
        pages: list[OcrPage],
        drafts: list[ChunkDraft],
        layouts: list[PageLayout] | None = None,
    ) -> StructureSummary:
        structure_id = stable_id("ST", document_id, ingestion_id, self.parser_version)
        by_page = {layout.page_id: layout for layout in layouts or []}
        if layouts is not None:
            if (
                len(by_page) != len(layouts)
                or set(by_page) != {p.page_id for p in pages}
                or self.parser_version != LAYOUT_VERSION
            ):
                raise ValueError("Layout 페이지 집합/규칙 버전이 일치하지 않습니다.")
            for page in pages:
                result = by_page[page.page_id]
                if result.parser_version != self.parser_version:
                    raise ValueError("Layout 규칙 버전이 일치하지 않습니다.")
                result.validate_sources(page)
        elif self.parser_version != PARSER_VERSION:
            raise ValueError("배치 분석 버전에는 Layout 결과가 필요합니다.")
        with self.database.connect() as connection:
            # 이 잠금 이후 OCR 완료나 다른 구조 요청은 대기한다. 먼저 최신 버전을 검증한 후
            # 중복을 확인해야, 이미 만들어진 과거 버전의 재요청도 409로 정확히 거절한다.
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT d.ingestion_id, i.status FROM document d "
                "JOIN ingestion i USING (ingestion_id) WHERE d.document_id = ?",
                (document_id,),
            ).fetchone()
            if current is None:
                raise KeyError(document_id)
            if current["ingestion_id"] != ingestion_id or current["status"] != "ready":
                raise StructureConflict(
                    "OCR 버전이 변경되었습니다. 새로고침 후 다시 요청해 주세요."
                )
            existing = connection.execute(
                "SELECT * FROM structure_run WHERE structure_id = ?", (structure_id,)
            ).fetchone()
            if existing:
                return self._summary(connection, existing)
            connection.execute(
                "INSERT INTO structure_run(structure_id, document_id, ingestion_id, "
                "parser_version, "
                "section_count, chunk_count) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    structure_id,
                    document_id,
                    ingestion_id,
                    self.parser_version,
                    len(pages),
                    len(drafts),
                ),
            )
            for page in pages:
                section_id = stable_id("SEC", structure_id, page.page_id)
                result = by_page.get(page.page_id)
                title = f"{page.pdf_page_number}페이지"
                if result and result.regions and result.regions[0].kind == "heading":
                    title_blocks = set(result.regions[0].block_ids)
                    title = " ".join(b.text for b in page.blocks if b.block_id in title_blocks)
                # 기본 방식은 페이지 번호, 배치 분석은 첫 제목 후보를 표시한다.
                # 검증된 제목으로 오해하지 않도록 title_source/inferred와 신뢰도 0은 유지한다.
                # 원문 block.section_id는 갱신하지 않아 다른 구조 버전의 해석을 덮지 않는다.
                connection.execute(
                    "INSERT INTO section(section_id, document_id, title, level, page_start, "
                    "page_end, section_path, title_source, structure_confidence) "
                    "VALUES (?, ?, ?, 1, ?, ?, ?, 'inferred', 0)",
                    (
                        section_id,
                        document_id,
                        title,
                        page.pdf_page_number,
                        page.pdf_page_number,
                        f"page/{page.pdf_page_number}",
                    ),
                )
                cursor = connection.execute(
                    "INSERT INTO structure_section(section_id, structure_id, page_id) "
                    "SELECT ?, ?, page_id FROM page WHERE page_id = ? "
                    "AND document_id = ? AND ingestion_id = ?",
                    (section_id, structure_id, page.page_id, document_id, ingestion_id),
                )
                if cursor.rowcount != 1:
                    raise ValueError("Section 원문 페이지의 문서/처리 버전이 일치하지 않습니다.")
                if result:
                    connection.execute(
                        "INSERT INTO layout_page VALUES (?, ?, ?, ?, ?)",
                        (
                            structure_id,
                            page.page_id,
                            page.pdf_page_number,
                            int(result.needs_review),
                            result.model_dump_json(),
                        ),
                    )
            for draft in drafts:
                connection.execute(
                    "INSERT INTO chunk(chunk_id, document_id, section_id, ingestion_id, "
                    "chunk_order, text, content_type, ocr_confidence, chunking_version, "
                    "text_hash, index_status) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')",
                    (
                        draft.chunk_id,
                        document_id,
                        draft.section_id,
                        ingestion_id,
                        draft.order,
                        draft.text,
                        draft.content_type,
                        draft.confidence,
                        self.parser_version,
                        draft.text_hash,
                    ),
                )
                for order, block_id in enumerate(draft.block_ids):
                    # source가 해당 Section의 페이지에 속하는지 DB에서 재검증한다.
                    # 단순 FK만으로는 다른 문서의 유효한 block_id 연결을 막지 못한다.
                    cursor = connection.execute(
                        "INSERT INTO chunk_source(chunk_id, block_id, source_order) "
                        "SELECT ?, b.block_id, ? FROM document_block b "
                        "JOIN structure_section s ON s.page_id = b.page_id "
                        "WHERE b.block_id = ? AND s.section_id = ? AND s.structure_id = ?",
                        (draft.chunk_id, order, block_id, draft.section_id, structure_id),
                    )
                    if cursor.rowcount != 1:
                        raise ValueError("Chunk 원문 Block의 페이지/처리 버전이 일치하지 않습니다.")
            row = connection.execute(
                "SELECT * FROM structure_run WHERE structure_id = ?", (structure_id,)
            ).fetchone()
            # 빈 페이지뿐인 문서도 section_count>0, chunk_count=0인 완료 결과로 저장한다.
            # 중간 INSERT가 실패하면 실행 기록까지 전부 롤백되므로 부분 완료는 보이지 않는다.
            return self._summary(connection, row)

    def list_chunks(
        self, document_id: str, ingestion_id: str, page_number: int | None, limit: int, offset: int
    ) -> list[ChunkView]:
        with self.database.connect() as connection:
            rows = connection.execute(
                self._select() + " WHERE c.document_id = ? AND c.ingestion_id = ? "
                "AND r.parser_version = ? AND i.status = 'ready' "
                "AND (? IS NULL OR p.pdf_page_number = ?) "
                "ORDER BY c.chunk_order LIMIT ? OFFSET ?",
                (
                    document_id,
                    ingestion_id,
                    self.parser_version,
                    page_number,
                    page_number,
                    limit,
                    offset,
                ),
            ).fetchall()
            return [self._view(connection, row) for row in rows]

    def get_chunk(self, document_id: str, chunk_id: str) -> ChunkView | None:
        with self.database.connect() as connection:
            row = connection.execute(
                self._select()
                + " WHERE c.document_id = ? AND c.chunk_id = ? AND i.status = 'ready'",
                (document_id, chunk_id),
            ).fetchone()
            return self._view(connection, row) if row else None

    def list_layouts(
        self, document_id: str, ingestion_id: str, limit: int, offset: int
    ) -> list[PageLayout]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT l.result_json FROM layout_page l "
                "JOIN structure_run r USING (structure_id) JOIN ingestion i USING (ingestion_id) "
                "WHERE r.document_id = ? AND r.ingestion_id = ? AND r.parser_version = ? "
                "AND i.status = 'ready' ORDER BY l.page_number LIMIT ? OFFSET ?",
                (document_id, ingestion_id, self.parser_version, limit, offset),
            ).fetchall()
            return [PageLayout.model_validate_json(row[0]) for row in rows]

    @staticmethod
    def _summary(connection: sqlite3.Connection, row: sqlite3.Row) -> StructureSummary:
        count = connection.execute(
            "SELECT count(*) FROM layout_page WHERE structure_id = ? AND needs_review = 1",
            (row["structure_id"],),
        ).fetchone()[0]
        return StructureSummary(
            **dict(row),
            review_page_count=count,
            layout_mode="geometry_v1"
            if row["parser_version"] == LAYOUT_VERSION
            else "page_fallback",
        )

    @staticmethod
    def _select() -> str:
        # 원문 버전과 구조 버전 모두 같은 연결만 조회한다. 과거 Chunk의 index_status가
        # failed여도 원문 확인은 가능하다. 검색 후보 선별은 향후 색인 서비스의 별도 책임이다.
        return (
            "SELECT c.*, r.structure_id, s.title AS section_title, p.page_id, "
            "p.pdf_page_number, d.document_version FROM chunk c "
            "JOIN structure_section m ON m.section_id = c.section_id "
            "JOIN structure_run r ON r.structure_id = m.structure_id "
            "AND r.document_id = c.document_id AND r.ingestion_id = c.ingestion_id "
            "JOIN section s ON s.section_id = c.section_id "
            "JOIN page p ON p.page_id = m.page_id "
            "AND p.document_id = c.document_id AND p.ingestion_id = c.ingestion_id "
            "JOIN ingestion i ON i.ingestion_id = c.ingestion_id "
            "JOIN document d ON d.document_id = c.document_id"
        )

    @staticmethod
    def _view(connection: sqlite3.Connection, row: sqlite3.Row) -> ChunkView:
        blocks = connection.execute(
            "SELECT b.* FROM chunk_source cs JOIN document_block b USING(block_id) "
            "WHERE cs.chunk_id = ? ORDER BY cs.source_order",
            (row["chunk_id"],),
        ).fetchall()
        return ChunkView(
            **{
                key: row[key]
                for key in (
                    "chunk_id",
                    "structure_id",
                    "section_id",
                    "section_title",
                    "text",
                    "text_hash",
                    "chunk_order",
                    "ocr_confidence",
                    "index_status",
                    "page_id",
                    "content_type",
                )
            },
            evidence=Evidence(
                evidence_id=stable_id("EV", row["chunk_id"]),
                document_id=row["document_id"],
                document_version=row["document_version"],
                ingestion_id=row["ingestion_id"],
                chunk_id=row["chunk_id"],
                block_ids=[block["block_id"] for block in blocks],
                pdf_page_number=row["pdf_page_number"],
            ),
            sources=[
                OcrBlock(
                    block_id=block["block_id"],
                    reading_order=block["reading_order"],
                    text=block["text"],
                    bbox=json.loads(block["bbox_json"]),
                    confidence=block["ocr_confidence"],
                )
                for block in blocks
            ],
        )
