import unittest

from pydantic import ValidationError

from handover_ai.domain.models import HandoverDocument


def create_handover(evidence_id: str = "EV-001") -> dict:
    return {
        "schema_version": "1.0",
        "handover_id": "HO-001",
        "revision": 1,
        "title": "CAN 장애 대응 인수인계",
        "status": "needs_review",
        "owner_employee_id": None,
        "recipient_employee_id": "EMP-003",
        "tasks": [
            {
                "task_id": "TASK-021",
                "overview": {
                    "text": "CAN 오류 발생 시 진단과 복구를 수행한다.",
                    "evidence_ids": [evidence_id],
                },
                "prerequisites": [],
                "steps": [
                    {
                        "order": 1,
                        "text": "DTC를 확인한다.",
                        "evidence_ids": [evidence_id],
                    }
                ],
                "cautions": [],
                "troubleshooting": [],
            }
        ],
        "evidence": [
            {
                "evidence_id": "EV-001",
                "document_id": "DOC-001",
                "document_version": 1,
                "ingestion_id": "ING-001",
                "chunk_id": "CH-00231",
                "block_ids": ["BL-001"],
                "pdf_page_number": 12,
            }
        ],
        "missing_fields": ["owner_employee_id"],
        "conflicts": [],
        "generation_run_id": "RUN-001",
        "template_version": "1.0",
    }


class HandoverDocumentTest(unittest.TestCase):
    def test_accepts_valid_evidence_reference(self):
        document = HandoverDocument.model_validate(create_handover())
        self.assertEqual(document.tasks[0].steps[0].evidence_ids, ["EV-001"])

    def test_rejects_unknown_evidence_reference(self):
        with self.assertRaises(ValidationError):
            HandoverDocument.model_validate(create_handover("EV-404"))


if __name__ == "__main__":
    unittest.main()
