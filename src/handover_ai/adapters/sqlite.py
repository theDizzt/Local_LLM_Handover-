import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

SCHEMA_VERSION = "1"

SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

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
"""


class SQLiteDatabase:
    def __init__(self, database_path: Path):
        self.database_path = database_path

    def initialize(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.executescript(SCHEMA_SQL)
            connection.execute(
                "INSERT OR IGNORE INTO schema_version(version) VALUES (?)",
                (SCHEMA_VERSION,),
            )

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
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
