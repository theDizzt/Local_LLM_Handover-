from pathlib import Path
from typing import Protocol


class PdfInspector(Protocol):
    def page_count(self, path: Path) -> int:
        """PDF를 검증하고 페이지 수 반환. 잘못된 파일은 InvalidDocument 발생."""
        ...
