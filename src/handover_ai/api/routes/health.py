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
