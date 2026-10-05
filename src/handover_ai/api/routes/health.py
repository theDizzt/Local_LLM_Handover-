# [읽기 안내] 시스템 확인 API다. /health는 DB 연결을 확인하고,
# /architecture는 설계 정보를 알려준다. 후자는 외부 모델이 준비됐다는 뜻이 아니다.
from typing import Annotated

from fastapi import APIRouter, Depends

from handover_ai.adapters.sqlite import SQLiteDatabase
from handover_ai.api.dependencies import get_database, get_settings
from handover_ai.config import Settings

router = APIRouter(tags=["system"])
SettingsDependency = Annotated[Settings, Depends(get_settings)]
DatabaseDependency = Annotated[SQLiteDatabase, Depends(get_database)]


@router.get("/health")
def health(
    settings: SettingsDependency,
    database: DatabaseDependency,
):
    # 설정과 DB는 Depends를 통해 들어오므로 테스트 DB로 쉽게 바꿀 수 있다.
    return {
        "status": "ready",
        "service": settings.app_name,
        "environment": settings.app_env,
        "database": database.get_health(),
    }


@router.get("/architecture")
def architecture():
    return {
        "api_version": "v1",
        "source_of_truth": "sqlite",
        "layers": [
            "api",
            "services",
            "domain",
            "ports",
            "adapters",
        ],
        "replaceable_adapters": [
            "ocr",
            "layout_analyzer",
            "vector_store",
            "model_gateway",
        ],
        "llm_roles": [
            "document_extractor",
            "question_generator",
            "answer_evaluator",
            "difference_explainer",
        ],
    }
