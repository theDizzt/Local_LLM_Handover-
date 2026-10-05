"""좌표 규칙 또는 향후 모델 기반 분석기를 교체하기 위한 계약."""

from typing import Protocol

from handover_ai.domain.layout import PageLayout
from handover_ai.domain.ocr import OcrPage


class LayoutAnalyzer(Protocol):
    version: str

    def analyze(self, page: OcrPage) -> PageLayout: ...
