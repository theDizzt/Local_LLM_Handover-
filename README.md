# Local LLM Handover

로컬 LLM을 활용한 인수인계서 작성 및 평가 프로그램입니다. 현재 버전은 개발
계획서의 핵심 계약을 코드로 고정한 아키텍처 기반 단계입니다.

## 현재 구현 범위

- FastAPI 기반 버전 API와 상태 확인
- Pydantic 기반 인수인계서·근거·채점 Schema
- LLM, Repository, Vector Store 교체를 위한 Protocol
- SQLite 문서 구조·Chunk·실행 이력 Schema
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
python -m unittest discover -s tests -v
```

## 주요 경로

```text
src/handover_ai/
├─ api/          FastAPI 요청·응답 경계
├─ services/     유스케이스 조합
├─ domain/       Schema와 순수 계산 규칙
├─ ports/        교체 가능한 기술 계약
└─ adapters/     SQLite 등 외부 기술 구현
```
