# 문서 구조·Chunk·원문 근거 연결

아래 설명은 기존 **페이지 순서** 방식이다. 2026-10-04부터 별도 버전의
**배치 분석 (시험)**도 제공한다. [배치 분석과 검토](layout-analysis.md)를 참고한다.

## 사용 방법

`run.bat` 실행 후 <http://127.0.0.1:8000/>에서 문서를 선택한다.
OCR 완료 후 **근거 준비**를 누르면 페이지별 근거 묶음을 저장한다.
하단 **이 페이지의 근거 묶음**에서 항목을 선택하면 해당 OCR 행의 원문 영역을 모두 강조한다.
한 페이지에 근거가 많으면 **근거 더 보기**로 추가 조회한다.

같은 버전에서 버튼을 다시 눌러도 중복 생성하지 않는다. 새로고침 후에도 결과가 남는다.
OCR 재처리에 성공하면 새 버전의 근거를 다시 준비한다. 재처리에 실패하면 이전 성공
버전의 근거를 계속 볼 수 있다. 다른 탭에서 OCR 버전이 바뀐 경우에는 새로고침 안내를 표시한다.

## 현재 규칙과 한계

- 규칙 버전: `page-lines-v1:chars=800`.
- 페이지마다 임시 Section을 만든다. 제목은 `1페이지`처럼 표시하며 실제 목차 추출 결과가 아니다.
- Section은 `title_source=inferred`, `structure_confidence=0`이다.
- OCR `reading_order`를 보존하고, 행 사이 줄바꿈을 포함하여 약 800자 단위로 묶는다.
- 페이지를 넘겨 묶지 않으며 원문 행을 중간에서 자르지 않는다. 단일 행이 800자를 넘으면
  해당 행만 하나의 긴 Chunk로 보존한다. 이는 소프트 제한이며 임베딩 토큰 제한은 아니다.
- 텍스트를 새로 쓰거나 요약하지 않는다. 공백/문장부호를 포함한 행 텍스트가 그대로 연결된다.
- 공백뿐인 행은 제외한다. 빈 페이지는 Section만 만들고 Chunk는 만들지 않는다.
- Chunk OCR 신뢰도는 포함된 행 중 최솟값이다. 평균이 저품질 행을 가리지 않도록 한다.
- 표의 셀 복원, 제목 판별, 다단 읽기 순서 교정은 후속 Layout 분석이다. 현재 순서는
  OCR 엔진이 저장한 순서이므로 복잡한 문서의 의미상 읽기 순서를 보장하지 않는다.
- 생성 Chunk의 `index_status`는 `pending`이다. 임베딩/검색은 아직 실행하지 않는다.

## 저장 구조와 버전 보존

```text
structure_run (문서 ID + ingestion_id + parser_version별 1개)
  └─ structure_section (Section ↔ 실행 버전 ↔ 원문 Page)
       ├─ section (페이지 기본 구조)
       └─ chunk
            └─ chunk_source → document_block → page → 원문 이미지
```

기존 `section`에 버전 열이 없으므로 별도 매핑 테이블로 버전을 명시한다. 기존 DB의
테이블/열을 바꾸지 않고 `CREATE TABLE IF NOT EXISTS`로 추가한다. 매핑이 없는 과거
Section은 신규 조회에서 제외하고 보존한다. `schema_version=1`은 기존 기반 스키마
버전이며, 새 구조 규칙은 `structure_run.parser_version`으로 관리한다.

`document_block.section_id`는 덮어쓰지 않는다. 같은 OCR 원문을 이후 다른 Layout
규칙으로 분석할 때 원문에 붙은 전역 Section 포인터를 바꾸면 과거 해석이 손상되기 때문이다.
새 Chunk의 Section과 매핑 테이블이 각 구조 버전의 소속을 나타낸다.

쓰기 예약(`BEGIN IMMEDIATE`) 후 현재 OCR 버전과 완료 상태를 다시 확인한다.
구조 실행 기록·Section·Chunk·원문 연결을 하나의 트랜잭션으로 저장한다. 중간 실패는
모두 롤백하고 반복/동시 요청은 같은 결과를 반환한다. 저장 중 외부 모델 호출은 없다.
원문 연결 시 실제 Block이 해당 Section의 페이지에 속하는지도 DB에서 확인한다.

ID는 문서/처리/규칙/페이지/순서에서 결정적으로 생성한다. 새 OCR 버전은 다른 ID를 얻는다.
OCR 재처리에 성공하면 기존 동작대로 예전 Chunk의 `index_status`는 `failed`가 되지만,
원문 근거 조회는 계속 허용한다. 이것은 과거 근거 보존이며 검색 가능하다는 뜻은 아니다.
생성 문서는 `evidence_id`, `chunk_id`, `ingestion_id`, `block_ids`를 고정하여 저장해야 한다.

## API

| 요청 | 의미 |
|---|---|
| `POST /api/v1/documents/{id}/structure` | 현재 OCR 버전의 구조/Chunk 생성 또는 기존 결과 반환 |
| `GET /api/v1/documents/{id}/structure` | 현재 OCR 버전의 구조 요약; 미생성이면 null |
| `GET /api/v1/documents/{id}/chunks` | 현재 OCR 버전의 Chunk 목록 |
| `GET /api/v1/documents/{id}/chunks/{chunk_id}` | 특정 Chunk와 원문 근거 조회; 과거 성공 버전도 허용 |

POST 본문은 `{"ingestion_id": "ING-..."}`이며 화면에서 확인한 버전을 필수로 전달한다.
OCR 미완료·버전 변경은 409, 없는 문서는 404, 잘못된 입력은 422다.
GET 구조/목록은 `ingestion_id`로 과거 버전을 지정할 수 있다. 다른 문서/실패 버전을
지정하면 구조는 null, Chunk는 빈 목록을 반환한다. 다른 문서 ID로 상세를 조회하면 404다.
목록은 `page_number`, `limit`(1~100), `offset`(0 이상)을 지원한다.

Chunk 응답에는 `evidence`, `sources`, `page_id`, `section_title`이 포함된다.
`sources`에는 원문 텍스트·신뢰도·0~1 bbox가 있고, 이미지는 기존 페이지 이미지 API로 조회한다.
`text_hash`는 저장 텍스트의 UTF-8 SHA-256이다.

## 코드와 검증

- `domain/chunks.py`: 순수 분할 규칙, 안정적인 ID, API 응답 계약.
- `ports/chunks.py`: 교체 가능한 저장소 계약.
- `services/chunks.py`: OCR 버전 고정, 페이지 읽기, 분할, 저장 조합.
- `adapters/chunk_repository.py`: 트랜잭션·버전 검증·조회·근거 조립.
- `api/routes/chunks.py`: 입력/응답과 오류를 HTTP로 변환.
- `web/review.js`: 준비 동작, 같은 버전의 페이지별 조회, 여러 원문 좌표 강조.

2026-10-04: 전체 테스트 49개 통과, 실제 OCR 선택 테스트 1개 건너뜀, Ruff 통과.
단위/API 테스트는 긴 행, 순서, 원문 보존, 빈 페이지, 중복/동시 요청, 저장 실패 롤백,
계산 도중 OCR 버전 변경, 다른 페이지 근거 차단, 과거 근거 보존, 기존 DB 추가 초기화를 검증한다.
Edge 브라우저에서 근거 준비/재진입, 복수 좌표 강조, OCR 실패/재처리 후 버전 분리,
원문 HTML 문자열의 안전한 표시와 데스크톱/모바일 레이아웃을 확인했다.
이번 기능은 저장된 OCR을 사용하므로 실제 엔진을 재실행하지 않았다.
