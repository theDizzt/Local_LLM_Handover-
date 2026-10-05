"""브라우저 회귀 테스트 전용 서버. 운영 DB와 OCR 모델을 사용하지 않는다.

python -m uvicorn review_server:app --app-dir tests --port 8765
테스트 제어 API는 이 모듈에만 정의하므로 운영 앱에는 포함되지 않는다.
"""

import asyncio
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from time import sleep
from typing import Annotated

from fastapi import Depends

from handover_ai.adapters.document_repository import SQLiteDocumentRepository
from handover_ai.adapters.draft_repository import SQLiteDraftRepository
from handover_ai.adapters.ocr import PyMuPdfRenderer
from handover_ai.adapters.ocr_repository import SQLiteOcrRepository
from handover_ai.adapters.sqlite import SQLiteDatabase
from handover_ai.api.dependencies import get_draft_service, get_ocr_service, get_search_service
from handover_ai.config import load_settings
from handover_ai.domain.drafts import DraftSelection
from handover_ai.domain.ocr import OcrLine
from handover_ai.main import create_app
from handover_ai.services.drafts import DraftService
from handover_ai.services.ocr import OcrService
from handover_ai.services.search import SearchService

temporary = TemporaryDirectory(prefix="handover-browser-")
root = Path(temporary.name)
settings = replace(
    load_settings(),
    database_path=root / "db.sqlite",
    document_store_path=root / "documents",
    page_store_path=root / "pages",
    vector_store_path=root / "chroma",
)
database = SQLiteDatabase(settings.database_path)


class BrowserEngine:
    version = "browser-fixture"
    failing = False

    def recognize(self, page):
        # 실제 HTTP에서 queued/running 화면이 나타나는 시간을 확보한다.
        sleep(0.5)
        if self.failing:
            raise ValueError("fixture failure")
        return [
            OcrLine(
                text=f"{page.number}페이지 업무 인수인계",
                bbox=(0.05, 0.1, 0.8, 0.2),
                confidence=0.97,
            ),
            OcrLine(text="전화번호 02-1234-5678 확인", bbox=(0.05, 0.4, 0.8, 0.5), confidence=0.65),
            OcrLine(
                text="<img src=x onerror=alert(1)>", bbox=(0.05, 0.7, 0.8, 0.8), confidence=0.9
            ),
        ]


engine = BrowserEngine()
service = OcrService(
    SQLiteDocumentRepository(database),
    SQLiteOcrRepository(database),
    PyMuPdfRenderer(72),
    engine,
    settings.page_store_path,
)
app = create_app(settings, database)
app.dependency_overrides[get_ocr_service] = lambda: service


class BrowserDraftGateway:
    # 화면 검증용 응답이며 실제 Ollama 추론 품질을 검증하는 대체물이 아니다.
    model_id = "browser-fixture"

    def validate_config(self):
        pass

    async def select(self, query, blocks, *, retry):
        await asyncio.sleep(0.5)
        return DraftSelection(
            items=[{"block_id": block["block_id"], "category": "overview"} for block in blocks[:3]]
        )


def browser_drafts(search: Annotated[SearchService, Depends(get_search_service)]):
    return DraftService(SQLiteDraftRepository(database), search, BrowserDraftGateway(), 5)


app.dependency_overrides[get_draft_service] = browser_drafts


@app.post("/__test/failure/{enabled}")
def set_failure(enabled: bool):
    engine.failing = enabled
    return {"enabled": enabled}
