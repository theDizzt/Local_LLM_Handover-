import sqlite3
import tempfile
import unittest
from pathlib import Path

from handover_ai.adapters.sqlite import SQLiteDatabase


class SQLiteDatabaseTest(unittest.TestCase):
    def test_initializes_schema_idempotently(self):
        with tempfile.TemporaryDirectory() as directory:
            database = SQLiteDatabase(Path(directory) / "handover.db")
            database.initialize()
            database.initialize()

            health = database.get_health()
            self.assertEqual(health["status"], "ready")
            self.assertEqual(health["schema_version"], "1")

    def test_records_generation_history(self):
        with tempfile.TemporaryDirectory() as directory:
            database = SQLiteDatabase(Path(directory) / "handover.db")
            database.initialize()
            database.record_generation_run(
                generation_run_id="RUN-001",
                role="document_extractor",
                provider="ollama",
                model_id="example-model",
                prompt_version="1.0",
                schema_version="1.0",
                status="completed",
                metadata={"temperature": 0},
            )

            with database.connect() as connection:
                count = connection.execute(
                    "SELECT COUNT(*) AS count FROM generation_run"
                ).fetchone()["count"]
            self.assertEqual(count, 1)

    def test_rejects_parent_section_from_another_document(self):
        with tempfile.TemporaryDirectory() as directory:
            database = SQLiteDatabase(Path(directory) / "handover.db")
            database.initialize()

            with database.connect() as connection:
                connection.execute(
                    "INSERT INTO ingestion VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)",
                    ("ING-001", "1.0", "1.0", "ready"),
                )
                for document_id in ("DOC-001", "DOC-002"):
                    connection.execute(
                        """
                        INSERT INTO document (
                            document_id, file_name, page_count, file_sha256,
                            source_uri, document_version, ingestion_id
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            document_id,
                            f"{document_id}.pdf",
                            1,
                            document_id,
                            f"file:///{document_id}.pdf",
                            1,
                            "ING-001",
                        ),
                    )
                connection.execute(
                    """
                    INSERT INTO section VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "SEC-001",
                        "DOC-001",
                        None,
                        "제목",
                        1,
                        1,
                        1,
                        "제목",
                        "body_heading",
                        1,
                    ),
                )

            with self.assertRaises(sqlite3.IntegrityError):
                with database.connect() as connection:
                    connection.execute(
                        """
                        INSERT INTO section VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            "SEC-002",
                            "DOC-002",
                            "SEC-001",
                            "잘못된 하위 제목",
                            2,
                            1,
                            1,
                            "제목 > 잘못된 하위 제목",
                            "body_heading",
                            1,
                        ),
                    )


if __name__ == "__main__":
    unittest.main()
