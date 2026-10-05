# [읽기 안내] 외부 환경 변수를 Python 설정 객체로 변환하는 곳이다.
# 다른 계층은 getenv()를 직접 호출하지 않고 이 객체를 전달받아 사용한다.
# 환경 변수는 문자열이므로 숫자와 경로는 여기서 int/Path로 바꾼다.
from dataclasses import dataclass
from os import getenv
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Settings:
    # frozen은 생성 후 설정 변경을 막고, slots는 선언하지 않은 속성 추가를 막는다.
    # 기본값이 있는 필드는 기존 테스트/호출 코드에서 생략해도 된다.
    app_name: str
    app_env: str
    database_path: Path
    llm_provider: str
    llm_endpoint: str
    llm_timeout_seconds: int
    vector_store: str
    vector_store_path: Path
    # 원본 파일과 DB는 서로 다른 저장소다. 경로는 서버 설정으로만 결정한다.
    document_store_path: Path = Path("data/documents")
    max_upload_bytes: int = 50 * 1024 * 1024
    page_store_path: Path = Path("data/pages")
    ocr_dpi: int = 150
    ocr_max_pixels: int = 25_000_000
    ocr_language: str = "korean"
    ocr_detection_model_dir: str | None = None
    ocr_recognition_model_dir: str | None = None
    embedding_model_path: Path = Path("data/models/multilingual-e5-small")
    llm_model: str = ""


def load_settings() -> Settings:
    # 두 번째 인자는 환경 변수가 없을 때의 기본값이다. .env 파일 자동 로드는 하지 않는다.
    return Settings(
        app_name=getenv("APP_NAME", "Local LLM Handover"),
        app_env=getenv("APP_ENV", "development"),
        database_path=Path(getenv("DATABASE_PATH", "data/handover.db")),
        llm_provider=getenv("LLM_PROVIDER", "ollama"),
        llm_model=getenv("LLM_MODEL", ""),
        llm_endpoint=getenv("LLM_ENDPOINT", "http://127.0.0.1:11434"),
        llm_timeout_seconds=int(getenv("LLM_TIMEOUT_SECONDS", "60")),
        vector_store=getenv("VECTOR_STORE", "chroma"),
        vector_store_path=Path(getenv("VECTOR_STORE_PATH", "data/chroma")),
        embedding_model_path=Path(
            getenv("EMBEDDING_MODEL_PATH", "data/models/multilingual-e5-small")
        ),
        document_store_path=Path(getenv("DOCUMENT_STORE_PATH", "data/documents")),
        max_upload_bytes=int(getenv("MAX_UPLOAD_BYTES", str(50 * 1024 * 1024))),
        page_store_path=Path(getenv("PAGE_STORE_PATH", "data/pages")),
        ocr_dpi=int(getenv("OCR_DPI", "150")),
        ocr_max_pixels=int(getenv("OCR_MAX_PIXELS", "25000000")),
        ocr_language=getenv("OCR_LANGUAGE", "korean"),
        ocr_detection_model_dir=getenv("OCR_DETECTION_MODEL_DIR") or None,
        ocr_recognition_model_dir=getenv("OCR_RECOGNITION_MODEL_DIR") or None,
    )
