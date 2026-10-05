# 코드 읽기 안내

처음에는 아래 순서로 읽으면 됩니다. 소스의 `[읽기 안내]`와 함수 내부 `#` 주석은
학습용 설명이며 읽은 뒤 삭제해도 실행 동작에는 영향이 없습니다. 기존 docstring은
일부 API 설명에도 사용되므로 필요에 따라 남겨 두세요.

## 1. 전체 구조

`main.py → api/routes → services → domain + ports → adapters`

- `main.py`: 앱을 만들고 라우터와 시작 작업을 등록합니다.
- `config.py`, `api/dependencies.py`: 환경 설정과 실제 구현 객체를 조립합니다.
- `api/routes`: HTTP 요청/응답과 상태 코드를 다룹니다.
- `services`: 등록·OCR·평가처럼 한 번의 작업 순서를 조정합니다.
- `domain`: 데이터 형식, 검증 조건, 점수 계산 같은 업무 규칙입니다.
- `ports`: 서비스가 외부 기술에 요구하는 메서드 계약입니다.
- `adapters`: SQLite·PyMuPDF·PaddleOCR로 그 계약을 구현합니다.

## 2. PDF 등록 따라 읽기

1. `api/routes/documents.py`의 `register_document`: 업로드 파일을 받습니다.
2. `services/documents.py`의 `register`: 임시 파일 복사, 크기/해시 계산을 합니다.
3. `adapters/pdf.py`: PDF를 실제로 열어 유효성과 페이지 수를 확인합니다.
4. `adapters/document_repository.py`: 중복 조회와 메타데이터 저장을 합니다.
5. `adapters/sqlite.py`의 `connect`: 성공 시 commit, 실패 시 rollback합니다.
6. 서비스의 `finally`: 임시 파일과 실패한 요청의 원본을 정리합니다.

`tests/test_documents.py`를 나란히 보면 각 분기가 어떤 상황에서 실행되는지 확인할 수 있습니다.

## 3. OCR 따라 읽기

1. `api/routes/ocr.py`: 요청을 받아 202와 작업 ID를 반환합니다.
2. `services/ocr.py`의 `enqueue`: 새 작업과 처리 버전을 DB에 만듭니다.
3. 응답 뒤 `BackgroundTasks`가 같은 서비스의 `run`을 호출합니다.
4. `adapters/ocr.py`: PDF 페이지를 PNG로 바꾸고 OCR 결과를 표준 좌표로 변환합니다.
5. `adapters/ocr_repository.py`: 페이지·텍스트·진행률을 저장합니다.
6. 전체 성공 시 `complete`가 문서의 현재 처리 버전을 바꿉니다.
7. 실패 시 `fail`은 새 작업만 실패로 기록하고 이전 성공 결과를 유지합니다.

원본 파일 ID인 `document_id`, 처리 버전인 `ingestion_id`, 진행 상태 조회용 `job_id`는
서로 역할이 다릅니다. `domain/ocr.py`의 주석에서 좌표와 상태 표현을 확인하세요.

## 4. 평가 계산 따라 읽기

`api/routes/evaluations.py → services/evaluation.py → domain/scoring.py`

현재는 두 경로에서 전달받은 기준별 점수를 환산하고 비교합니다. LLM 자동 채점은 아직
연결되지 않았습니다. `None`은 0점이 아니라 계산 불가, `accepted_candidate`는 최종 승인이
아니라 자동 처리 후보입니다. `tests/test_scoring.py`의 작은 숫자 예제부터 읽으면 편합니다.

## 자주 나오는 문법

| 표현 | 이 프로젝트에서의 의미 |
|---|---|
| `Protocol` | 구현체가 제공해야 할 메서드 목록 |
| `BaseModel`, `Field` | 데이터 형식과 입력 제약 검증 |
| `Annotated[..., Depends(...)]` | FastAPI가 필요한 객체를 준비해 인자로 전달 |
| `with` | 파일/DB 연결을 정해진 범위에서 사용하고 정리 |
| `yield` | 연결을 with 본문에 전달하거나 페이지를 하나씩 생성 |
| `try/except/finally` | 처리 시도, 오류 대응, 성공 여부와 무관한 정리 |
| SQL의 `?` | 문자열 조립 대신 값을 안전하게 바인딩하는 자리 |
| `rowcount` | 조건부 UPDATE에 실제로 영향을 받은 행 수 |
| `@lru_cache` | 같은 인자의 객체 생성 결과 재사용 |
