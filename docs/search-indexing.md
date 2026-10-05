# 로컬 검색 색인

2026-10-05 구현. SQLite의 현재 OCR와 선택한 구조 버전 안에서 근거를 검색한다.
기본 페이지 순서와 시험용 배치 분석은 별도 색인을 사용한다.

## 실행

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[rag]"
.\.venv\Scripts\python.exe scripts/prepare_embedding_model.py
.\run.bat
```

공개 `intfloat/multilingual-e5-small` 모델은 준비 스크립트에서만 내려받는다.
스크립트는 다운로드할 저장소 revision을 고정하고 로컬 manifest에 기록한다.
서버 추론은 로컬 파일만 읽으며 문서와 검색어를 외부 서비스에 보내지 않는다.
기본 경로는 `data/models/multilingual-e5-small`, 변경 시 `EMBEDDING_MODEL_PATH`를 설정한다.
벡터 저장 경로는 `VECTOR_STORE_PATH` (기본 `data/chroma`)다. `.env` 자동 로드는 없다.
모델 파일/설정 교체 후에는 서버를 재시작하고 색인을 다시 준비한다.

화면 순서: PDF 등록 → OCR → 정리 방식 선택 → 근거 준비 → 검색 색인 준비·재구축
→ 검색어 입력 → 결과 선택 → 해당 페이지의 원문 영역 확인.
재구축 실패 시 이미 완료된 같은 버전의 색인은 계속 사용할 수 있다.
모델/패키지가 없으면 설치 안내를 표시하며 OCR 화면은 계속 사용할 수 있다.

## 저장·공개 경계

- `search_run`: queued/running/ready/failed 실행 이력과 완료 개수, 오류 코드.
- `search_member`: 해당 실행이 저장한 Chunk ID와 텍스트 해시.
- `search_active`: 구조별로 완성된 실행을 가리키는 포인터.
- Chroma: 실행별 독립 컬렉션에 ID와 벡터만 저장. 원문 사본은 저장하지 않는다.

32개씩 색인한 뒤 전체 개수와 현재 OCR 버전을 확인하고 한 트랜잭션으로 공개한다.
재구축 중·실패한 컬렉션은 검색하지 않는다. 재시작 시 중단된 작업은 failed로 바뀐다.
검색 전과 검색 후보를 원문으로 해석하는 시점에 현재 ingestion, 구조, 모델 버전,
공개 실행, 멤버십과 text_hash를 확인한다. 이전 OCR의 원문은 상세 조회로 보존하지만
검색에서는 제외한다. 벡터 저장소가 다른 문서/구조 ID를 반환해도 근거로 채택하지 않는다.

모델 파일 내용, 추론 패키지 버전, prefix·길이 처리 정책의 해시가 `index_version`이다.
E5에는 한국어도 `query: ` / `passage: ` 접두사를 사용한다. 긴 입력은 토크나이저 기준
512토큰 이하의 창으로 나눠 모두 임베딩한 뒤 평균·정규화한다. 원문과 bbox는 바꾸지 않는다.
이 평균 방식의 긴 문서 검색 품질은 실제 업무 자료로 추가 평가해야 한다.
모델 규칙은 [E5 모델 카드](https://huggingface.co/intfloat/multilingual-e5-small),
컬렉션 설정은 [Chroma 문서](https://docs.trychroma.com/docs/collections/configure)를 따른다.

## API

공통 접두사 `/api/v1/documents/{document_id}`:

| 메서드·경로 | 입력 | 결과 |
|---|---|---|
| POST `/search-index` | `structure_id` | 202, queued 실행; 백그라운드 구축 |
| GET `/search-index` | query `structure_id` | 최신 실행·사용 가능한 완료 실행 |
| POST `/search` | `structure_id`, `query`, `top_k` (1~20, 기본 5) | 순위별 Chunk·Evidence·Block 좌표·검토 사유 |

검색어는 공백 제외 1~2000자다. 잘못된 버전/미준비 색인은 409,
미등록 문서는 404, 모델 또는 벡터 읽기 장애는 503이다.
거리값은 순위용 수치이며 사실 정확도나 OCR 신뢰도 확률이 아니다.

## 검증과 한계

`python -m pytest -q`: 전체 76개 통과, 선택 OCR 실모델 테스트 1개 건너뜀.
`scripts/validate_search.py`: 실제 로컬 E5와 Chroma에서 합성 한국어 질문 2개의
최상위 근거 확인, 긴 입력의 전체 문자 보존과 토큰 제한 확인.
결과는 `data/search-validation/report.json`에 저장한다.
Edge 검증은 실제 검색 모델과 테스트 OCR을 사용하여 검색·근거 이동·재진입·버전 격리와
모바일 화면을 확인했다. 실제 업무 문서의 검색 정확도를 보장하는 평가가 아니다.

단일 서버 프로세스 운용을 전제로 한다. 여러 worker와 분산 작업 큐는 아직 지원하지 않는다.
오래된/실패한 컬렉션은 [이전 색인 정리](search-cleanup.md)에서 선택 삭제할 수 있다.
자동 정리 정책은 아직 없다. [사용자 검토 기록](layout-review.md)은 검색 결과에 연결했다.
다문서 통합 검색, 표 셀 복원, 검색 근거 기반 초안 생성은 후속 단계다.
