r"""가상 업무·직원 정답 데이터를 앱 SQLite DB에 넣는다. 다시 실행하면 같은 ID를 갱신한다.

매뉴얼 파일명으로 이미 등록된 문서를 찾아 업무와 연결한다(task_document).

사용: .venv\Scripts\python.exe scripts\load_workforce.py [--dataset ...] [--database ...]
"""

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from handover_ai.adapters.sqlite import SQLiteDatabase  # noqa: E402
from handover_ai.adapters.workforce_repository import SQLiteWorkforceRepository  # noqa: E402
from handover_ai.domain.workforce import SyntheticDataset  # noqa: E402


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=ROOT / "datasets/records-v1/dataset.json")
    parser.add_argument(
        "--database",
        type=Path,
        default=Path(os.getenv("DATABASE_PATH", ROOT / "data/handover.db")),
    )
    args = parser.parse_args(argv)
    dataset = SyntheticDataset.model_validate(json.loads(args.dataset.read_text(encoding="utf-8")))
    database = SQLiteDatabase(args.database)
    database.initialize()
    repository = SQLiteWorkforceRepository(database)
    counts = repository.import_dataset(dataset)
    links = repository.link_documents(dataset)
    print(f"{args.database}: 업무 {counts['tasks']}개, 직원 {counts['employees']}명 저장")
    print(f"업무-문서 새 연결 {links['linked_documents']}건")
    if links["unmatched_documents"]:
        # 문항 생성은 연결된 문서에서만 근거를 찾는다. 등록 후 다시 실행하면 연결된다.
        print(f"아직 등록되지 않은 매뉴얼 {len(links['unmatched_documents'])}개:")
        for name in links["unmatched_documents"]:
            print(f"  - {name}")
    return {**counts, **links}


if __name__ == "__main__":
    main()
