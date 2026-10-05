# [읽기 안내] SQLite 스키마 정의와 연결 수명 관리다. 업무별 SQL은 Repository에 둔다.
# 연결마다 foreign_keys를 켜야 REFERENCES가 실제로 잘못된 참조를 거부한다.
# CREATE ... IF NOT EXISTS는 기존 테이블을 보존한다. 기존 열/제약 변경은 별도 migration이
# 필요하며, 현재 schema_version=1은 기반 스키마 버전이다. OCR 테이블은 추가 생성한다.
import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

SCHEMA_VERSION = "1"

SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS excerpt_draft (
    draft_id TEXT PRIMARY KEY,
    generation_run_id TEXT NOT NULL UNIQUE REFERENCES generation_run(generation_run_id),
    document_id TEXT NOT NULL REFERENCES document(document_id),
    structure_id TEXT NOT NULL REFERENCES structure_run(structure_id),
    request_json TEXT NOT NULL,
    result_json TEXT,
    status TEXT NOT NULL CHECK(status IN ('queued','running','ready','failed')),
    error_code TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    finished_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS draft_running_document ON excerpt_draft(document_id)
WHERE status IN ('queued','running');

-- 실제 벡터 삭제와 SQLite는 같은 트랜잭션을 공유하지 않는다. 삭제 의도를 먼저
-- 기록하고 완료 여부를 나중에 저장하여, 중간에 서버가 종료돼도 안전하게 재시도한다.
CREATE TABLE IF NOT EXISTS search_cleanup (
    run_id TEXT PRIMARY KEY REFERENCES search_run(run_id),
    status TEXT NOT NULL CHECK(status IN ('deleting','deleted','failed')),
    error_code TEXT,
    attempts INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- 검토는 (구조 버전, 페이지)에 귀속된다. OCR/분석을 다시 만들면 새 키가 되므로
-- 이전 확정이 새 결과에 자동 적용되지 않는다. revision별 이력은 삭제하지 않는다.
CREATE TABLE IF NOT EXISTS layout_review (
    structure_id TEXT NOT NULL,
    page_id TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK(revision > 0),
    status TEXT NOT NULL CHECK(status IN ('unreviewed','confirmed','needs_correction')),
    note TEXT NOT NULL,
    text_checked INTEGER NOT NULL CHECK(text_checked IN (0,1)),
    order_checked INTEGER NOT NULL CHECK(order_checked IN (0,1)),
    regions_checked INTEGER NOT NULL CHECK(regions_checked IN (0,1)),
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(structure_id,page_id,revision),
    FOREIGN KEY(structure_id,page_id) REFERENCES layout_page(structure_id,page_id)
);

CREATE TABLE IF NOT EXISTS search_run (
    run_id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL REFERENCES document(document_id),
    structure_id TEXT NOT NULL REFERENCES structure_run(structure_id),
    index_version TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('queued','running','ready','failed')),
    total INTEGER NOT NULL,
    completed INTEGER NOT NULL DEFAULT 0,
    error_code TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE UNIQUE INDEX IF NOT EXISTS search_running_document
ON search_run(document_id) WHERE status IN ('queued','running');
CREATE TABLE IF NOT EXISTS search_active (
    structure_id TEXT PRIMARY KEY REFERENCES structure_run(structure_id),
    run_id TEXT NOT NULL REFERENCES search_run(run_id)
);
CREATE TABLE IF NOT EXISTS search_member (
    run_id TEXT NOT NULL REFERENCES search_run(run_id),
    chunk_id TEXT NOT NULL REFERENCES chunk(chunk_id),
    text_hash TEXT NOT NULL,
    PRIMARY KEY(run_id, chunk_id)
);

CREATE TABLE IF NOT EXISTS schema_version (
    version TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS ingestion (
    ingestion_id TEXT PRIMARY KEY,
    ocr_version TEXT NOT NULL,
    parser_version TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'ready', 'failed')),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- document는 원본 파일의 메타데이터, ingestion은 해당 원본을 처리한 한 번의 실행이다.
-- OCR 재시도는 원본을 복제하지 않고 새 ingestion을 만들며 성공 시 document가 가리킨다.
CREATE TABLE IF NOT EXISTS document (
    document_id TEXT PRIMARY KEY,
    file_name TEXT NOT NULL,
    page_count INTEGER NOT NULL CHECK (page_count > 0),
    file_sha256 TEXT NOT NULL,
    source_uri TEXT NOT NULL,
    document_version INTEGER NOT NULL CHECK (document_version > 0),
    ingestion_id TEXT NOT NULL REFERENCES ingestion(ingestion_id),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (file_sha256, document_version)
);

CREATE TABLE IF NOT EXISTS section (
    section_id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL REFERENCES document(document_id) ON DELETE CASCADE,
    parent_section_id TEXT REFERENCES section(section_id),
    title TEXT NOT NULL,
    level INTEGER NOT NULL CHECK (level >= 1),
    page_start INTEGER NOT NULL CHECK (page_start >= 1),
    page_end INTEGER NOT NULL CHECK (page_end >= page_start),
    section_path TEXT NOT NULL,
    title_source TEXT NOT NULL CHECK (title_source IN ('toc', 'body_heading', 'inferred')),
    structure_confidence REAL NOT NULL CHECK (structure_confidence BETWEEN 0 AND 1)
);

-- 외래 키만으로는 부모가 같은 문서에 속하는지 검사할 수 없어 트리거로 보완한다.
CREATE TRIGGER IF NOT EXISTS validate_section_parent_insert
BEFORE INSERT ON section
WHEN NEW.parent_section_id IS NOT NULL
BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM section AS parent
        WHERE parent.section_id = NEW.parent_section_id
          AND parent.document_id = NEW.document_id
    ) THEN RAISE(ABORT, 'parent section must belong to the same document') END;
END;

CREATE TRIGGER IF NOT EXISTS validate_section_parent_update
BEFORE UPDATE OF parent_section_id, document_id ON section
WHEN NEW.parent_section_id IS NOT NULL
BEGIN
    -- 자기 자신 또는 자신의 자손을 부모로 삼으면 순환이 생긴다.
    -- WITH RECURSIVE는 자식을 따라 내려가 모든 자손 ID를 구한다.
    SELECT CASE WHEN NEW.parent_section_id = NEW.section_id
        THEN RAISE(ABORT, 'section hierarchy cannot contain a cycle') END;
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM section AS parent
        WHERE parent.section_id = NEW.parent_section_id
          AND parent.document_id = NEW.document_id
    ) THEN RAISE(ABORT, 'parent section must belong to the same document') END;
    SELECT CASE WHEN EXISTS (
        WITH RECURSIVE descendants(section_id) AS (
            SELECT section_id FROM section WHERE parent_section_id = NEW.section_id
            UNION ALL
            SELECT child.section_id
            FROM section AS child
            JOIN descendants ON child.parent_section_id = descendants.section_id
        )
        SELECT 1 FROM descendants WHERE section_id = NEW.parent_section_id
    ) THEN RAISE(ABORT, 'section hierarchy cannot contain a cycle') END;
END;

CREATE TABLE IF NOT EXISTS page (
    page_id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL REFERENCES document(document_id) ON DELETE CASCADE,
    ingestion_id TEXT NOT NULL REFERENCES ingestion(ingestion_id),
    pdf_page_number INTEGER NOT NULL CHECK (pdf_page_number >= 1),
    printed_page_label TEXT,
    image_uri TEXT NOT NULL,
    width_px INTEGER NOT NULL CHECK (width_px > 0),
    height_px INTEGER NOT NULL CHECK (height_px > 0),
    render_dpi INTEGER NOT NULL CHECK (render_dpi > 0),
    rotation INTEGER NOT NULL DEFAULT 0,
    UNIQUE (document_id, ingestion_id, pdf_page_number)
);

-- block은 원문에서 인식한 최소 텍스트 단위다. bbox_json은 저장 PNG 기준 좌표다.
-- page가 삭제되면 종속 block도 삭제하지만, 과거 근거 보존을 위해 재처리 때 삭제하지 않는다.
CREATE TABLE IF NOT EXISTS document_block (
    block_id TEXT PRIMARY KEY,
    page_id TEXT NOT NULL REFERENCES page(page_id) ON DELETE CASCADE,
    section_id TEXT REFERENCES section(section_id),
    block_type TEXT NOT NULL,
    reading_order INTEGER NOT NULL CHECK (reading_order >= 0),
    text TEXT NOT NULL,
    bbox_json TEXT NOT NULL,
    ocr_confidence REAL NOT NULL CHECK (ocr_confidence BETWEEN 0 AND 1),
    structure_confidence REAL NOT NULL CHECK (structure_confidence BETWEEN 0 AND 1)
);

CREATE TABLE IF NOT EXISTS chunk (
    chunk_id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL REFERENCES document(document_id) ON DELETE CASCADE,
    section_id TEXT REFERENCES section(section_id),
    ingestion_id TEXT NOT NULL REFERENCES ingestion(ingestion_id),
    chunk_order INTEGER NOT NULL CHECK (chunk_order >= 0),
    text TEXT NOT NULL,
    content_type TEXT NOT NULL,
    ocr_confidence REAL NOT NULL CHECK (ocr_confidence BETWEEN 0 AND 1),
    chunking_version TEXT NOT NULL,
    text_hash TEXT NOT NULL,
    index_status TEXT NOT NULL CHECK (index_status IN ('pending', 'ready', 'failed'))
);

-- 하나의 검색 Chunk가 여러 OCR Block을 포함할 수 있어 별도 연결 테이블을 둔다.
CREATE TABLE IF NOT EXISTS chunk_source (
    chunk_id TEXT NOT NULL REFERENCES chunk(chunk_id) ON DELETE CASCADE,
    block_id TEXT NOT NULL REFERENCES document_block(block_id),
    source_order INTEGER NOT NULL CHECK (source_order >= 0),
    PRIMARY KEY (chunk_id, block_id)
);

CREATE TABLE IF NOT EXISTS generation_run (
    generation_run_id TEXT PRIMARY KEY,
    role TEXT NOT NULL,
    provider TEXT NOT NULL,
    model_id TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    status TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- 이하 테이블은 업무/인수자/출제/평가/생성 문서의 저장 기반이다.
-- 테이블이 있다고 모든 사용자 기능이나 Repository가 구현된 것은 아니다.
CREATE TABLE IF NOT EXISTS employee (
    employee_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    skills_json TEXT NOT NULL DEFAULT '{}',
    experience_months INTEGER CHECK (experience_months >= 0),
    available_minutes INTEGER CHECK (available_minutes >= 0),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS task (
    task_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    criticality REAL CHECK (criticality BETWEEN 0 AND 100),
    failure_impact REAL CHECK (failure_impact BETWEEN 0 AND 100),
    dependency_score REAL CHECK (dependency_score BETWEEN 0 AND 100),
    frequency REAL CHECK (frequency BETWEEN 0 AND 100),
    urgency REAL CHECK (urgency BETWEEN 0 AND 100),
    required_skills_json TEXT NOT NULL DEFAULT '{}',
    expected_minutes INTEGER CHECK (expected_minutes > 0),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS task_document (
    task_id TEXT NOT NULL REFERENCES task(task_id) ON DELETE CASCADE,
    document_id TEXT NOT NULL REFERENCES document(document_id) ON DELETE CASCADE,
    PRIMARY KEY (task_id, document_id)
);

CREATE TABLE IF NOT EXISTS question (
    question_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES task(task_id),
    question_type TEXT NOT NULL,
    prompt TEXT NOT NULL,
    answer_json TEXT NOT NULL,
    difficulty TEXT NOT NULL,
    rubric_json TEXT NOT NULL,
    max_score REAL NOT NULL CHECK (max_score > 0),
    evidence_json TEXT NOT NULL,
    rubric_version TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS evaluation (
    evaluation_id TEXT PRIMARY KEY,
    question_id TEXT NOT NULL REFERENCES question(question_id),
    employee_id TEXT NOT NULL REFERENCES employee(employee_id),
    answer TEXT NOT NULL,
    python_score REAL CHECK (python_score BETWEEN 0 AND 100),
    llm_score REAL CHECK (llm_score BETWEEN 0 AND 100),
    hybrid_score REAL CHECK (hybrid_score BETWEEN 0 AND 100),
    human_score REAL CHECK (human_score BETWEEN 0 AND 100),
    label TEXT CHECK (label IN ('PASS', 'RETRAIN', 'INCOMPLETE')),
    python_score_mode TEXT,
    status TEXT NOT NULL,
    evidence_json TEXT NOT NULL DEFAULT '[]',
    scoring_policy_version TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS handover_document (
    handover_id TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision > 0),
    status TEXT NOT NULL CHECK (status IN ('draft', 'needs_review', 'approved')),
    document_json TEXT NOT NULL,
    generation_run_id TEXT NOT NULL REFERENCES generation_run(generation_run_id),
    template_version TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (handover_id, revision)
);

CREATE INDEX IF NOT EXISTS idx_section_document ON section(document_id);
CREATE INDEX IF NOT EXISTS idx_chunk_document ON chunk(document_id, index_status);
CREATE INDEX IF NOT EXISTS idx_block_page ON document_block(page_id, reading_order);
CREATE INDEX IF NOT EXISTS idx_question_task ON question(task_id);
CREATE INDEX IF NOT EXISTS idx_evaluation_employee ON evaluation(employee_id, created_at);

-- 기존 DB에도 적용되는 추가 테이블이다. 기존 ingestion의 CHECK 제약은 유지한다.
-- 실행 중/대기 상태는 작업 테이블이 맡고, ingestion은 결과의 유효성만 표현한다.
CREATE TABLE IF NOT EXISTS ocr_job (
    job_id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL REFERENCES document(document_id),
    ingestion_id TEXT NOT NULL UNIQUE REFERENCES ingestion(ingestion_id),
    status TEXT NOT NULL CHECK (status IN ('queued', 'running', 'completed', 'failed')),
    completed_pages INTEGER NOT NULL DEFAULT 0,
    total_pages INTEGER NOT NULL,
    error_code TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    finished_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_ocr_active_document ON ocr_job(document_id)
    WHERE status IN ('queued', 'running');

-- 기존 section 열이나 원문 block을 덮어쓰지 않는 추가 스키마다. section의 처리 버전은
-- structure_section → structure_run으로 명시한다. 기존 section에는 매핑이 없으므로
-- 신규 구조 조회에 섞이지 않는다. 같은 OCR에 다른 Layout 규칙을 적용해도 병존 가능하다.
CREATE TABLE IF NOT EXISTS structure_run (
    structure_id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL REFERENCES document(document_id),
    ingestion_id TEXT NOT NULL REFERENCES ingestion(ingestion_id),
    parser_version TEXT NOT NULL,
    section_count INTEGER NOT NULL CHECK (section_count >= 0),
    chunk_count INTEGER NOT NULL CHECK (chunk_count >= 0),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(document_id, ingestion_id, parser_version)
);
CREATE TABLE IF NOT EXISTS structure_section (
    section_id TEXT PRIMARY KEY REFERENCES section(section_id),
    structure_id TEXT NOT NULL REFERENCES structure_run(structure_id),
    page_id TEXT NOT NULL REFERENCES page(page_id),
    UNIQUE(structure_id, page_id)
);
CREATE INDEX IF NOT EXISTS idx_structure_sections ON structure_section(structure_id);
CREATE INDEX IF NOT EXISTS idx_chunk_section ON chunk(section_id, chunk_order);

-- Layout 해석은 원문 Block의 열을 수정하지 않고 구조 버전별로 보관한다.
-- JSON에는 영역 종류/읽기 순서/좌표/검토 사유가 포함되고, 기존 기본 구조는 행이 없다.
CREATE TABLE IF NOT EXISTS layout_page (
    structure_id TEXT NOT NULL REFERENCES structure_run(structure_id),
    page_id TEXT NOT NULL REFERENCES page(page_id),
    page_number INTEGER NOT NULL,
    needs_review INTEGER NOT NULL CHECK (needs_review IN (0, 1)),
    result_json TEXT NOT NULL,
    PRIMARY KEY(structure_id, page_id)
);
"""


class SQLiteDatabase:
    def __init__(self, database_path: Path):
        # 경로만 저장한다. 객체 생성 자체는 디스크에 DB를 만들거나 연결하지 않는다.
        self.database_path = database_path

    def initialize(self) -> None:
        # 앱 시작 시 호출한다. 여러 번 실행해도 기존 데이터를 삭제하지 않는다.
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.executescript(SCHEMA_SQL)
            connection.execute(
                "INSERT OR IGNORE INTO schema_version(version) VALUES (?)",
                (SCHEMA_VERSION,),
            )

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        # with database.connect() as connection: 구문에서 yield한 연결을 사용한다.
        # 본문이 정상 종료되면 commit, 예외가 전파되면 rollback, 모든 경우 close한다.
        # row_factory 덕분에 row[0]뿐 아니라 row['document_id'] 형태로 접근할 수 있다.
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def get_health(self) -> dict[str, str | int]:
        # 외부 LLM/OCR 상태가 아니라 SQLite 연결과 기반 스키마 버전만 확인한다.
        with self.connect() as connection:
            version = connection.execute(
                "SELECT version FROM schema_version ORDER BY applied_at DESC LIMIT 1"
            ).fetchone()
            connection.execute("SELECT 1").fetchone()
        return {
            "status": "ready",
            "schema_version": version["version"] if version else "unknown",
        }

    def record_generation_run(
        self,
        generation_run_id: str,
        role: str,
        provider: str,
        model_id: str,
        prompt_version: str,
        schema_version: str,
        status: str,
        metadata: dict,
    ) -> None:
        # SQL의 ?는 값 바인딩 자리다. 문자열 조립으로 SQL에 사용자 값을 넣지 않는다.
        # metadata는 JSON 문자열로 저장하며 ensure_ascii=False로 한글 가독성을 유지한다.
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO generation_run (
                    generation_run_id, role, provider, model_id,
                    prompt_version, schema_version, status, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    generation_run_id,
                    role,
                    provider,
                    model_id,
                    prompt_version,
                    schema_version,
                    status,
                    json.dumps(metadata, ensure_ascii=False),
                ),
            )
