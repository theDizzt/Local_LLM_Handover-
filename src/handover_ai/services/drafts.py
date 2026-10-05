"""검색 → 검토 정책 → 모델의 원문 선택 → 근거 재검증 → 초안 저장을 조정한다."""

import asyncio
from time import monotonic

from handover_ai.domain.drafts import (
    CATEGORIES,
    PROMPT_VERSION,
    SCHEMA_VERSION,
    DraftFailure,
    DraftGateway,
    DraftRequest,
    materialize,
)
from handover_ai.domain.search import SearchConflict, SearchRequest, SearchUnavailable


class DraftService:
    def __init__(self, repository, search, gateway: DraftGateway, timeout):
        self.repository, self.search, self.gateway, self.timeout = (
            repository,
            search,
            gateway,
            timeout,
        )

    def enqueue(self, document_id, request):
        self.gateway.validate_config()
        # 색인이 없으면 작업을 만들기 전에 안내한다. 생성 작업 안에서도 다시 조회한다.
        if not self.search.state(document_id, request.structure_id)["active"]:
            raise SearchConflict("선택한 정리 방식의 검색 색인을 먼저 준비해 주세요.")
        return self.repository.enqueue(document_id, request, self.gateway.model_id)

    async def generate(self, job):
        if not await asyncio.to_thread(self.repository.start, job):
            return
        started, metadata = monotonic(), {"attempts": 0}
        try:
            request = DraftRequest.model_validate(job["request"])
            found = await asyncio.to_thread(
                self.search.search,
                job["document_id"],
                SearchRequest(
                    structure_id=request.structure_id,
                    query=request.query,
                    top_k=request.top_k,
                ),
            )
            sources, length = {}, 0
            for hit in found["hits"]:
                review = hit.get("human_review")
                if review and review.status == "needs_correction":
                    continue
                if request.reviewed_only and (not review or review.status != "confirmed"):
                    continue
                chunk = hit["chunk"]
                for block in chunk.sources:
                    if block.block_id in sources or not block.text.strip():
                        continue
                    # 입력 크기를 제한하되 한 행을 중간에서 잘라 문맥을 바꾸지 않는다.
                    if (
                        len(block.text) > 2000
                        or length + len(block.text) > 12000
                        or len(sources) >= 60
                    ):
                        continue
                    sources[block.block_id] = {
                        "block": block.model_dump(mode="json"),
                        "chunk_id": chunk.chunk_id,
                        "text_hash": chunk.text_hash,
                        "page_id": chunk.page_id,
                        "evidence": chunk.evidence.model_dump(mode="json"),
                        "human_review": review.model_dump(mode="json") if review else None,
                        "review_reasons": hit.get("review_reasons", []),
                    }
                    length += len(block.text)
            if not sources:
                raise DraftFailure("no_eligible_evidence")
            metadata.update(search_run_id=found["run_id"], source_block_ids=list(sources))
            prompt_blocks = [
                {"block_id": key, "text": value["block"]["text"]} for key, value in sources.items()
            ]
            # 두 요청을 합한 모델 대기 시간을 제한한다. JSON/근거 오류만 한 번 교정하고
            # 연결 오류·타임아웃은 중복 추론을 피하기 위해 즉시 실패시킨다.
            async with asyncio.timeout(self.timeout):
                for attempt in range(2):
                    metadata["attempts"] = attempt + 1
                    try:
                        selection = await self.gateway.select(
                            request.query, prompt_blocks, retry=bool(attempt)
                        )
                        items = materialize(selection, sources)
                        break
                    except DraftFailure as exc:
                        if exc.code != "invalid_model_output" or attempt == 1:
                            raise
            result = {
                "schema_version": SCHEMA_VERSION,
                "prompt_version": PROMPT_VERSION,
                "title": request.title,
                "status": "needs_review",
                "items": items,
                "missing_categories": [
                    key for key in CATEGORIES if not any(i["category"] == key for i in items)
                ],
                "notice": "원문 발췌형 초안입니다. 분류·순서·누락과 OCR 정확도를 검토해 주세요.",
            }
            metadata["elapsed_ms"] = int((monotonic() - started) * 1000)
            await asyncio.to_thread(self.repository.finish, job, result, metadata)
        except Exception as exc:
            code = (
                exc.code
                if isinstance(exc, DraftFailure)
                else (
                    "model_timeout"
                    if isinstance(exc, TimeoutError)
                    else "source_changed"
                    if isinstance(exc, SearchConflict)
                    else "search_unavailable"
                    if isinstance(exc, SearchUnavailable)
                    else "generation_failed"
                )
            )
            metadata["elapsed_ms"] = int((monotonic() - started) * 1000)
            await asyncio.to_thread(self.repository.fail, job, code, metadata)
