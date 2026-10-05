# [읽기 안내] 완성형 문서/평가 생성의 장기 계약이다. 현재 원문 발췌 초안은
# domain.drafts.DraftGateway와 adapters.ollama의 별도 좁은 계약으로 먼저 연결했다.
# async는 모델 응답을 기다리는 동안 다른 요청을 처리할 수 있도록 하는 함수 형태다.
# 역할별 메서드가 있다고 해서 물리적으로 서로 다른 모델 다섯 개가 필요한 것은 아니다.
from typing import Any, Protocol

from handover_ai.domain.models import CriterionScore, HandoverDocument


class ModelGateway(Protocol):
    # OCR 결과 구조화 → 근거 기반 문서/문제 생성 → 답안 평가 → 점수 차이 설명.
    # 반환 타입이 dict인 계약도 실제 어댑터를 연결할 때 Schema/근거 검증이 필요하다.
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
