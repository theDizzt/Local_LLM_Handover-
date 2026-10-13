# 문서 안내

목적에 맞는 문서 하나만 열면 되도록 주제별로 정리했다.

| 이런 때 | 문서 |
|---|---|
| 프로그램을 실행하고 문서를 검토하거나 평가 자료를 준비할 때 | [사용자 안내](user-guide.md) |
| 지금 어디까지 왔고 다음에 무엇을 할지 볼 때 | [로드맵](roadmap.md) |
| 전체 구조·계층·저장소 책임과 코드 읽는 순서를 알고 싶을 때 | [아키텍처](architecture.md) |
| 등록·OCR·구조·배치 분석·검토 기록의 동작, API, 설정 | [문서 처리 파이프라인](pipeline.md) |
| 검색 색인·색인 정리·LLM 초안 생성, 모델 설정과 프롬프트 버전 이력 | [검색과 초안 생성](search-and-draft.md) |
| OCR·배치 분석·실제 문서 평가 방법과 결과 | [평가](evaluation.md) |
| 날짜별로 무엇을 했는지 | [작업 기록](work-log.md) |
| 최초 기획 의도와 요구사항 | [개발 계획서](plan/develop.md), [UI/UX 계획서](plan/ui-ux-development.md) |

`plan/`의 계획서는 착수 시점의 기획 문서로, 구현 결과와 다를 수 있다. 실제 동작은 위 문서들이 기준이다.

## 이전 문서 위치

| 이전 파일 | 옮겨간 곳 |
|---|---|
| `user-actions.md` | [user-guide.md](user-guide.md) |
| `next-steps.md` | [roadmap.md](roadmap.md) |
| `code-reading-guide.md` | [architecture.md › 코드 읽기 안내](architecture.md#코드-읽기-안내) |
| `document-registration.md`, `ocr-processing.md`, `ocr-review.md`, `document-structure.md`, `layout-analysis.md`, `layout-review.md` | [pipeline.md](pipeline.md) §1~5 |
| `search-indexing.md`, `search-cleanup.md`, `draft-generation.md` | [search-and-draft.md](search-and-draft.md) §1~3 |
| `layout-evaluation.md`, `real-document-evaluation.md`, `real-evaluation-running.md`, `real-evaluation-results-20261013.md` | [evaluation.md](evaluation.md) §2~4 (OCR 문자 정확도는 §1) |
| `develop.md`, `ui-ux-development.md` | [plan/](plan/) |
