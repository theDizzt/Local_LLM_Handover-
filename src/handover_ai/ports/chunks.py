"""구조 생성 서비스가 필요한 저장소 동작. 실제 SQLite 테이블은 어댑터에만 둔다."""

from typing import Protocol

from handover_ai.domain.chunks import ChunkDraft, ChunkView, StructureSummary
from handover_ai.domain.layout import PageLayout
from handover_ai.domain.ocr import OcrPage


class ChunkRepository(Protocol):
    def get_structure(self, document_id: str, ingestion_id: str) -> StructureSummary | None: ...

    def save_structure(
        self,
        document_id: str,
        ingestion_id: str,
        pages: list[OcrPage],
        drafts: list[ChunkDraft],
        layouts: list[PageLayout] | None = None,
    ) -> StructureSummary: ...

    def list_chunks(
        self, document_id: str, ingestion_id: str, page_number: int | None, limit: int, offset: int
    ) -> list[ChunkView]: ...

    def get_chunk(self, document_id: str, chunk_id: str) -> ChunkView | None: ...

    def list_layouts(
        self, document_id: str, ingestion_id: str, limit: int, offset: int
    ) -> list[PageLayout]: ...
