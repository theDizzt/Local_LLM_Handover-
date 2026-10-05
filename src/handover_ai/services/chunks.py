"""OCR 읽기 → 순수 Chunk 계산 → 원자적 저장의 순서를 조정한다."""

from handover_ai.domain.chunks import (
    PARSER_VERSION,
    StructureConflict,
    StructureSummary,
    page_chunks,
    stable_id,
)
from handover_ai.domain.layout import layout_chunks
from handover_ai.ports.chunks import ChunkRepository
from handover_ai.ports.layout import LayoutAnalyzer
from handover_ai.ports.ocr import OcrRepository
from handover_ai.ports.repositories import DocumentRepository


class ChunkService:
    def __init__(
        self,
        documents: DocumentRepository,
        ocr: OcrRepository,
        chunks: ChunkRepository,
        analyzer: LayoutAnalyzer | None = None,
    ):
        self.documents, self.ocr, self.chunks = documents, ocr, chunks
        self.analyzer = analyzer

    def build(self, document_id: str, ingestion_id: str) -> StructureSummary:
        document = self.documents.get_document(document_id)
        if document is None:
            raise KeyError(document_id)
        if document.status != "ready" or document.ingestion_id != ingestion_id:
            raise StructureConflict(
                "현재 OCR 완료 버전만 처리할 수 있습니다. 문서를 새로고침해 주세요."
            )
        version = self.analyzer.version if self.analyzer else PARSER_VERSION
        structure_id = stable_id("ST", document_id, ingestion_id, version)
        pages, drafts = [], []
        layouts = []
        offset = 0
        while True:
            # 현재 문서 포인터가 중간에 바뀌어도 읽는 원문은 같은 ingestion으로 고정한다.
            batch = self.ocr.pages(document_id, ingestion_id, 50, offset)
            for page in batch:
                pages.append(page)
                if self.analyzer:
                    result = self.analyzer.analyze(page)
                    layouts.append(result)
                    drafts.extend(layout_chunks(structure_id, page, result, len(drafts)))
                else:
                    drafts.extend(page_chunks(structure_id, page, len(drafts)))
            if len(batch) < 50:
                break
            offset += len(batch)
        if [page.pdf_page_number for page in pages] != list(range(1, document.page_count + 1)):
            raise StructureConflict("완료된 OCR 페이지가 모두 존재하는지 확인해 주세요.")
        # 계산 중 재처리가 성공했을 수도 있으므로 저장소가 쓰기 트랜잭션 안에서
        # 현재 버전을 다시 검증한다. 반복 요청도 이 경계에서 기존 결과로 합쳐진다.
        if self.analyzer:
            return self.chunks.save_structure(document_id, ingestion_id, pages, drafts, layouts)
        return self.chunks.save_structure(document_id, ingestion_id, pages, drafts)
