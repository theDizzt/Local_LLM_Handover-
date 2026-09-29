import hashlib
from datetime import UTC, datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import BinaryIO
from uuid import uuid4

from handover_ai.domain.documents import (
    DocumentRegistration,
    DocumentSummary,
    DocumentTooLarge,
    DuplicateDocument,
    InvalidDocument,
    StoredDocument,
)
from handover_ai.ports.pdf_inspector import PdfInspector
from handover_ai.ports.repositories import DocumentRepository


class DocumentService:
    def __init__(
        self,
        repository: DocumentRepository,
        inspector: PdfInspector,
        store_path: Path,
        max_upload_bytes: int,
    ):
        self.repository = repository
        self.inspector = inspector
        self.store_path = store_path.resolve()
        if max_upload_bytes <= 0:
            raise ValueError("max_upload_bytes must be positive")
        self.max_upload_bytes = max_upload_bytes

    @staticmethod
    def _result(document: StoredDocument, duplicate: bool) -> DocumentRegistration:
        # 공개용 모델로 명시적으로 변환하여 내부 저장 경로를 응답에서 제외한다.
        public = DocumentSummary.model_validate(document.model_dump())
        return DocumentRegistration(document=public, duplicate=duplicate)

    def register(self, filename: str | None, source: BinaryIO) -> DocumentRegistration:
        # Windows/Unix 경로 구분자를 모두 제거하고 파일명은 표시용으로만 쓴다.
        # 저장 경로에는 임의 ID를 사용하여 동명 파일 및 ../ 경로의 덮어쓰기를 막는다.
        name = (filename or "").replace("\\", "/").rsplit("/", 1)[-1]
        if not name or Path(name).suffix.lower() != ".pdf":
            raise InvalidDocument("확장자가 .pdf인 파일을 선택해 주세요.")
        if len(name) > 255 or any(ord(char) < 32 for char in name):
            raise InvalidDocument("파일명이 너무 길거나 제어 문자를 포함하고 있습니다.")

        self.store_path.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        final_path: Path | None = None
        committed = False
        try:
            # 전체 파일을 메모리에 올리지 않고 1 MiB씩 복사하며 크기와 해시를 계산한다.
            # 임시 파일도 같은 폴더에 두어 검증 후 같은 파일시스템에서 이름을 변경한다.
            digest = hashlib.sha256()
            size = 0
            with NamedTemporaryFile(dir=self.store_path, suffix=".upload", delete=False) as staged:
                temporary_path = Path(staged.name)
                while block := source.read(1024 * 1024):
                    size += len(block)
                    if size > self.max_upload_bytes:
                        raise DocumentTooLarge("파일 크기가 업로드 제한을 초과했습니다.")
                    digest.update(block)
                    staged.write(block)

            if size == 0:
                raise InvalidDocument("빈 파일은 등록할 수 없습니다.")
            page_count = self.inspector.page_count(temporary_path)
            file_hash = digest.hexdigest()
            existing = self.repository.get_by_hash(file_hash)
            if existing is not None:
                return self._result(existing, duplicate=True)

            document_id = f"DOC-{uuid4().hex}"
            final_path = self.store_path / f"{document_id}.pdf"
            document = StoredDocument(
                document_id=document_id,
                file_name=name,
                page_count=page_count,
                file_sha256=file_hash,
                source_uri=str(final_path),
                document_version=1,
                ingestion_id=f"ING-{uuid4().hex}",
                status="pending",
                created_at=datetime.now(UTC).isoformat(),
            )
            temporary_path.replace(final_path)
            try:
                self.repository.save_document(document)
            except DuplicateDocument:
                # 동시 등록에서 늦은 요청은 자신의 파일만 정리하고 먼저 등록된 결과를 반환한다.
                existing = self.repository.get_by_hash(file_hash)
                if existing is None:
                    raise
                return self._result(existing, duplicate=True)
            committed = True
            return self._result(document, duplicate=False)
        finally:
            # 파일과 SQLite는 공통 트랜잭션이 없으므로 실패 시 파일을 보상 삭제한다.
            # 강제 종료/정전으로 남는 고아 파일 회수는 별도 운영 기능이 필요하다.
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
            if final_path is not None and not committed:
                final_path.unlink(missing_ok=True)
