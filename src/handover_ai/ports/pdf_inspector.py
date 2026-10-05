# [읽기 안내] 문서 등록 단계의 최소 PDF 검사 계약이다. OCR/렌더링 계약과는 별개다.
# 파일 경로를 받고 유효한 페이지 수를 돌려주며, PDF 라이브러리 세부 예외를 숨긴다.
from pathlib import Path
from typing import Protocol


class PdfInspector(Protocol):
    def page_count(self, path: Path) -> int:
        """PDF를 검증하고 페이지 수 반환. 잘못된 파일은 InvalidDocument 발생."""
        ...
