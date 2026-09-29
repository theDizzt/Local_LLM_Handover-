# 문서 등록 구현

## 요청 처리 흐름

`API → DocumentService → PdfInspector / DocumentRepository`

1. API가 multipart/form-data의 `file`을 받는다.
2. 서비스가 표시용 파일명을 정리하고 임시 파일에 1 MiB씩 복사한다. 이때 실제 파일 크기와 SHA-256 해시를 계산한다.
3. PyMuPDF 어댑터가 실제 PDF인지, 암호가 필요한지, 페이지가 있는지 검사한다.
4. 같은 해시가 이미 등록되어 있으면 기존 문서를 반환하고 임시 파일을 삭제한다.
5. 새 문서이면 UUID 기반 파일명으로 원본을 저장하고 ingestion/document를 하나의 SQLite 트랜잭션으로 저장한다.
6. 신규 문서는 201, 중복 문서는 200으로 반환한다. 두 응답 모두 Location 헤더로 문서 상세 API 경로를 제공한다.

## 파일별 역할과 주석

| 파일 | 역할 |
|---|---|
| `domain/documents.py` | 공개 응답 모델, 내부 저장 모델, 등록 오류 종류 |
| `ports/repositories.py` | 저장소 교체를 위한 타입이 명시된 문서 계약 |
| `ports/pdf_inspector.py` | PDF 검사 라이브러리 교체 계약 |
| `adapters/pdf.py` | PyMuPDF로 파일 확인 및 페이지 수 조회 |
| `adapters/document_repository.py` | 매개변수화 SQL, 등록 트랜잭션, 목록 · 상세 조회 |
| `services/documents.py` | 파일 복사, 해시, 중복 판별, 저장 및 실패 시 정리 |
| `api/routes/documents.py` | HTTP 입력 · 출력과 오류 상태 코드 |
| `api/dependencies.py` | 서비스에 실제 어댑터를 주입하는 조립 지점 |

코드의 주석은 특히 파일 경로를 UUID로 만드는 이유, 동기 라우트를 사용하는 이유, 동시 중복 요청에서 UNIQUE 제약이 필요한 이유, 실패 시 정리 순서를 설명한다.

## 저장 규칙

- 원본은 로컬 파일 저장소에 메타데이터는 SQLite에 보관한다.
- 원본 경로는 내부 `StoredDocument`에만 포함되며 API 응답에서 제외한다.
- 신규 등록은 `document_version=1`, `status=pending`, OCR/parser 버전은 `not-run`으로 시작한다. 업로드 성공이 OCR 완료를 의미하지 않는다.
- 동일 이름의 다른 PDF는 별도 문서로 등록한다. 동일 내용은 다른 이름이어도 중복이다.
- 재처리/개정 기능은 이번 API에 포함되지 않는다. 향후 별도 동작에서 처리 버전을 만든다.
- DB 스키마는 기존 테이블과 UNIQUE(file_sha256, document_version)를 사용한다.

## 실패와 동시 요청

ingestion 및 document 삽입은 같은 트랜잭션으로 처리한다. 후자가 실패하면 전자도 롤백한다. 동시 업로드가 모두 사전 중복 조회를 통과하더라도 DB UNIQUE 제약이 한 요청만 저장하게 하며, 나머지는 기존 결과를 반환한다.

원본 파일은 DB 저장 전에 최종 경로로 이동한다. DB 저장 실패 시 이번 요청의 원본과 임시 파일을 정리한다. 따라서 정상 예외 처리에서는 DB가 없는 파일을 참조하지 않는다. 다만 파일시스템과 SQLite는 하나의 트랜잭션이 아니므로 프로세스 강제 종료/정전 시 DB에서 참조하지 않는 파일이 남을 수 있다. 자동 고아 파일 회수는 아직 구현하지 않았다.

파일 크기 제한은 multipart 파싱 후 서비스에서 적용된다. 이는 원본 등록 크기 제한이며 HTTP 요청 전체의 수신량 제한은 아니다. 배포 시 요청 본문 제한은 별도로 설정해야 한다.

## 검증

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check src tests
```

테스트는 임시 디렉터리와 실제 PDF를 사용한다. 등록 · 재시작 · 중복 · 목록 페이지 처리, 손상/빈/암호화 파일 거부, 크기 제한, DB 및 파일 이동 실패, 동시 중복 등록을 검증한다.
