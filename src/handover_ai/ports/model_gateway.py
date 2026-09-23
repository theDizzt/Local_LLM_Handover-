from typing import Any, Protocol

from handover_ai.domain.models import CriterionScore, HandoverDocument


class ModelGateway(Protocol):
    async def extract_document(self, ocr_result: dict[str, Any]) -> dict[str, Any]: ...

    async def generate_report(
        self,
        request: dict[str, Any],
        evidence: list[dict[str, Any]],
    ) -> HandoverDocument: ...

    async def generate_question(
        self,
        task_id: str,
        evidence: list[dict[str, Any]],
    ) -> dict[str, Any]: ...

    async def evaluate_answer(
        self,
        answer: str,
        rubric: list[dict[str, Any]],
        evidence: list[dict[str, Any]],
    ) -> list[CriterionScore]: ...

    async def explain_difference(
        self,
        python_score: float,
        llm_score: float,
    ) -> str: ...
