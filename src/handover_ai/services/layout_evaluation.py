"""사람이 작성한 정답과 분석 결과를 비교한다. OCR 문자 정확도는 평가하지 않는다."""

from collections import Counter

from handover_ai.domain.layout_evaluation import LayoutDataset
from handover_ai.ports.layout import LayoutAnalyzer


def ratio(numerator: int, denominator: int) -> float | None:
    # 정답/비교 쌍이 없을 때 0점이나 100점 대신 null을 사용한다.
    return numerator / denominator if denominator else None


def inversions(order: list[int]) -> int:
    # 모든 두 Block의 선후 관계를 이중 반복으로 세면 큰 페이지에서 O(n²)이 된다.
    # Fenwick tree의 누적합으로 앞서 등장한 더 큰 순위를 세어 O(n log n)에 계산한다.
    tree = [0] * (len(order) + 1)
    errors = 0
    for seen, rank in enumerate(order):
        index, smaller = rank + 1, 0
        while index:
            smaller += tree[index]
            index -= index & -index
        errors += seen - smaller
        index = rank + 1
        while index < len(tree):
            tree[index] += 1
            index += index & -index
    return errors


def evaluate_layout(dataset: LayoutDataset, analyzer: LayoutAnalyzer) -> dict:
    totals = Counter()
    confusion = Counter()
    details = []
    for annotated in dataset.pages:
        page = annotated.page
        count = sum(bool(block.text.strip()) for block in page.blocks)
        totals["blocks"] += count
        if annotated.annotation_status != "reviewed":
            totals["draft_pages"] += 1
            details.append({"page_id": page.page_id, "status": "unscored"})
            continue
        prediction = analyzer.analyze(page)
        # 누락·중복·외부 ID·잘못된 bbox를 조용히 점수 계산에서 제외하지 않는다.
        # 이 불변 조건을 위반하면 평가 전체를 실패시켜 잘못된 고득점을 막는다.
        prediction.validate_sources(page)
        actual_order = [key for region in prediction.regions for key in region.block_ids]
        actual_kinds = {
            key: region.kind for region in prediction.regions for key in region.block_ids
        }
        totals["reviewed_pages"] += 1
        totals["reviewed_blocks"] += count
        pairs = reversed_pairs = 0
        exact = None
        if annotated.expected_order is not None:
            totals["order_annotated_pages"] += 1
            expected = annotated.expected_order
            if expected:
                exact = expected == actual_order
                totals["order_nonempty_pages"] += 1
                totals["order_exact_pages"] += int(exact)
            ranks = {key: rank for rank, key in enumerate(expected)}
            pairs = count * (count - 1) // 2
            reversed_pairs = inversions([ranks[key] for key in actual_order])
            totals["order_pairs"] += pairs
            totals["order_reversed_pairs"] += reversed_pairs
        mismatches = []
        for key, expected_kind in annotated.expected_kinds.items():
            actual_kind = actual_kinds[key]
            confusion[(expected_kind, actual_kind)] += 1
            totals["classified_blocks"] += 1
            totals["correct_blocks"] += int(expected_kind == actual_kind)
            if expected_kind != actual_kind:
                mismatches.append(
                    {"block_id": key, "expected": expected_kind, "actual": actual_kind}
                )
        details.append(
            {
                "page_id": page.page_id,
                "pdf_page_number": page.pdf_page_number,
                "status": "scored",
                "order_exact": exact,
                "order_pairs": pairs,
                "order_reversed_pairs": reversed_pairs,
                "expected_order": annotated.expected_order,
                "predicted_order": actual_order,
                "classification_mismatches": mismatches,
                "classified_blocks": len(annotated.expected_kinds),
            }
        )
    per_kind = {}
    for kind in ("heading", "paragraph", "table_candidate", "unknown"):
        true_positive = confusion[(kind, kind)]
        expected_count = sum(
            value for (expected, _), value in confusion.items() if expected == kind
        )
        predicted_count = sum(value for (_, actual), value in confusion.items() if actual == kind)
        per_kind[kind] = {
            "support": expected_count,
            "predicted": predicted_count,
            "precision": ratio(true_positive, predicted_count),
            "recall": ratio(true_positive, expected_count),
            "f1": ratio(2 * true_positive, expected_count + predicted_count),
        }
    return {
        "schema_version": "1.0",
        "dataset_id": dataset.dataset_id,
        "source_kind": dataset.source_kind,
        "document_id": dataset.document_id,
        "ingestion_id": dataset.ingestion_id,
        "parser_version": analyzer.version,
        "note": "OCR 출력의 배치 분류·읽기 순서 평가입니다. OCR 문자·표 셀 복원 점수가 아닙니다.",
        "coverage": {
            "pages": len(dataset.pages),
            "reviewed_pages": totals["reviewed_pages"],
            "draft_pages": totals["draft_pages"],
            "blocks": totals["blocks"],
            "classified_blocks": totals["classified_blocks"],
            "classification_coverage": ratio(totals["classified_blocks"], totals["blocks"]),
            "order_annotated_pages": totals["order_annotated_pages"],
        },
        "reading_order": {
            "nonempty_pages": totals["order_nonempty_pages"],
            "exact_pages": totals["order_exact_pages"],
            "exact_page_accuracy": ratio(
                totals["order_exact_pages"], totals["order_nonempty_pages"]
            ),
            "pairs": totals["order_pairs"],
            "reversed_pairs": totals["order_reversed_pairs"],
            "pair_accuracy": ratio(
                totals["order_pairs"] - totals["order_reversed_pairs"], totals["order_pairs"]
            ),
        },
        "classification": {
            "accuracy": ratio(totals["correct_blocks"], totals["classified_blocks"]),
            "per_kind": per_kind,
            "confusion": [
                {"expected": a, "actual": b, "count": count}
                for (a, b), count in sorted(confusion.items())
                if count
            ],
        },
        "pages": details,
    }
