# Local LLM Handover

로컬 LLM을 활용한 인수인계서 작성 및 평가 프로그램입니다. 현재 버전은 개발
계획서의 핵심 계약을 코드로 고정한 아키텍처 기반 단계입니다.

## 현재 구현 범위

- FastAPI 기반 버전 API와 상태 확인
- Pydantic 기반 인수인계서·근거·채점 Schema
- LLM, Repository, Vector Store 교체를 위한 Protocol
- SQLite 문서 구조·Chunk·실행 이력 Schema
- PDF 등록·원본 저장·SHA-256 중복 확인·문서 목록/상세 API
- Python 점수 정규화, Label 비교, Hybrid Score 계산
- 업무 영향도와 기술 적합도 계산

OCR, Layout 분석, ChromaDB, Ollama 연동은 각 포트의 실제 어댑터로 다음
단계에서 연결합니다. 상세 구조와 구현 순서는
[`docs/architecture.md`](docs/architecture.md)를 참고합니다.

## 개발 환경 실행

Windows에서는 프로젝트 루트의 `run.bat`을 더블클릭하거나 다음과 같이
실행하면 된다. 가상환경과 필수 패키지가 없을 경우 최초 실행 시 자동으로
준비한다.

```powershell
.\run.bat
```

개발용 자동 새로고침 옵션도 그대로 전달할 수 있다.

```powershell
.\run.bat --reload
```

직접 환경을 구성하려면 다음 명령을 사용한다.

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
uvicorn handover_ai.main:app --reload
```

API 문서는 서버 실행 후 `http://127.0.0.1:8000/docs`에서 확인할 수 있습니다.

```powershell
python -m pytest -q
```

## 문서 등록 API

서버 실행 후 `/docs`의 `documents`에서 PDF 파일을 업로드할 수 있습니다.
아래 예제의 파일 경로는 실제 PDF 경로로 바꿉니다.

```powershell
curl.exe -X POST "http://127.0.0.1:8000/api/v1/documents" -F "file=@C:/samples/handover.pdf"
curl.exe "http://127.0.0.1:8000/api/v1/documents?limit=20&offset=0"
curl.exe "http://127.0.0.1:8000/api/v1/documents/DOC-실제문서ID"
```

- 신규 등록: `201`, 응답은 `{"document": {...}, "duplicate": false}`입니다.
- 동일 내용 재등록: 파일명과 관계없이 기존 문서를 `200`, `duplicate: true`로 반환합니다.
  기존 파일명과 문서 ID를 유지하며 새 처리 버전을 만들지 않습니다.
- 목록: 최신 등록순 배열이며 `limit`은 1~100, `offset`은 0 이상입니다.
- 잘못된 PDF·암호화 PDF: `422`, 크기 초과: `413`, 없는 문서 조회: `404`입니다.
- 원본 저장 위치는 `DOCUMENT_STORE_PATH`(기본 `data/documents`), 등록 크기 제한은
  `MAX_UPLOAD_BYTES`(기본 50 MiB) 환경 변수로 지정합니다. `.env.example`은 참고용이며
  현재 실행 코드는 `.env` 파일을 자동으로 읽지 않습니다.
- 등록 결과는 OCR 실행 전인 `pending` 상태입니다. 이번 단계에서는 페이지 수만
  검사하며 OCR 작업을 자동 실행하지 않습니다.

등록 처리와 실패 복구의 설계 설명은 [문서 등록 구현](docs/document-registration.md)을 참고하세요.

## 주요 경로

```text
src/handover_ai/
├─ api/          FastAPI 요청·응답 경계
├─ services/     유스케이스 조합
├─ domain/       Schema와 순수 계산 규칙
├─ ports/        교체 가능한 기술 계약
└─ adapters/     SQLite 등 외부 기술 구현
```
