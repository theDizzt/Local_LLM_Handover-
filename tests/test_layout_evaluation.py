"""미작성 정답의 고득점 방지, 비교 계산, 원문 버전 검증과 읽기 전용 내보내기."""

import importlib.util
import json
from pathlib import Path

import pytest
from test_ocr import setup as ocr_fixture

from handover_ai.adapters.layout import GeometryLayoutAnalyzer
from handover_ai.domain.layout import LayoutRegion, PageLayout, enclosing_box
from handover_ai.domain.layout_evaluation import AnnotatedPage, LayoutDataset, page_fingerprint
from handover_ai.domain.ocr import OcrBlock, OcrPage
from handover_ai.services.layout_evaluation import evaluate_layout, inversions

setup = ocr_fixture
spec = importlib.util.spec_from_file_location("evaluate_layout_cli", "scripts/evaluate_layout.py")
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)


def dataset(*, status="reviewed", order=None, kinds=None):
    page = OcrPage(
        page_id="P1",
        ingestion_id="I1",
        pdf_page_number=1,
        width_px=1000,
        height_px=1000,
        render_dpi=150,
        rotation=0,
        blocks=[
            OcrBlock(
                block_id=key,
                text="업무 원문",
                bbox=(0.1, y, 0.9, y + 0.02),
                confidence=0.9,
                reading_order=index,
            )
            for index, (key, y) in enumerate([("a", 0.1), ("b", 0.2), ("c", 0.3)])
        ],
    )
    annotation = AnnotatedPage(
        page=page,
        source_sha256=page_fingerprint(page),
        annotation_status=status,
        expected_order=order,
        expected_kinds=kinds or {},
    )
    return LayoutDataset(
        dataset_id="test",
        source_kind="synthetic",
        document_id="D1",
        ingestion_id="I1",
        pages=[annotation],
    )


class Predicted:
    version = "test-v1"

    def analyze(self, page):
        by_id = {block.block_id: block for block in page.blocks}
        return PageLayout(
            page_id=page.page_id,
            ingestion_id=page.ingestion_id,
            pdf_page_number=page.pdf_page_number,
            parser_version=self.version,
            regions=[
                LayoutRegion(kind="paragraph", block_ids=[key], bbox=enclosing_box([by_id[key]]))
                for key in ("b", "a", "c")
            ],
        )


def test_partial_annotation_coverage_and_error_metrics():
    data = dataset(order=["a", "b", "c"], kinds={"a": "heading", "b": "paragraph"})
    result = evaluate_layout(data, Predicted())
    assert result["coverage"]["classification_coverage"] == 2 / 3
    assert result["reading_order"]["pair_accuracy"] == 2 / 3
    assert result["reading_order"]["exact_page_accuracy"] == 0
    assert result["classification"]["accuracy"] == 0.5
    assert result["classification"]["per_kind"]["heading"]["recall"] == 0
    assert result["classification"]["per_kind"]["heading"]["precision"] is None
    assert result["classification"]["per_kind"]["paragraph"]["precision"] == 0.5
    assert result["classification"]["per_kind"]["table_candidate"]["f1"] is None
    assert result["pages"][0]["classification_mismatches"] == [
        {"block_id": "a", "expected": "heading", "actual": "paragraph"}
    ]


def test_draft_annotations_not_scored_even_when_populated():
    data = dataset(status="draft", order=["a", "b", "c"], kinds={"a": "paragraph"})
    result = evaluate_layout(data, Predicted())
    assert result["coverage"]["reviewed_pages"] == 0
    assert result["classification"]["accuracy"] is None
    assert result["reading_order"]["pair_accuracy"] is None
    assert result["pages"][0]["status"] == "unscored"


@pytest.mark.parametrize("order", [["a", "a", "c"], ["a", "b"], ["a", "b", "foreign"]])
def test_invalid_order_rejected(order):
    with pytest.raises(ValueError, match="정확히 한 번"):
        dataset(order=order)


def test_invalid_labels_missing_gold_and_mutated_source_rejected():
    with pytest.raises(ValueError, match="없는 Block"):
        dataset(kinds={"foreign": "heading"})
    with pytest.raises(ValueError, match="정답이 없는"):
        dataset()
    raw = dataset(order=["a", "b", "c"]).model_dump()
    raw["pages"][0]["page"]["blocks"][0]["text"] = "변경된 원문"
    with pytest.raises(ValueError, match="해시"):
        LayoutDataset.model_validate(raw)


def test_mixed_versions_and_duplicate_pages_rejected():
    raw = dataset(order=["a", "b", "c"]).model_dump()
    raw["ingestion_id"] = "different"
    with pytest.raises(ValueError, match="서로 다른"):
        LayoutDataset.model_validate(raw)
    raw["ingestion_id"] = "I1"
    raw["pages"].append(raw["pages"][0])
    with pytest.raises(ValueError, match="중복"):
        LayoutDataset.model_validate(raw)


def test_empty_page_does_not_inflate_order_accuracy():
    data = dataset(status="draft")
    raw = data.model_dump()
    page = data.pages[0].page.model_copy(update={"blocks": []})
    raw["pages"] = [
        AnnotatedPage(
            page=page,
            source_sha256=page_fingerprint(page),
            annotation_status="reviewed",
            expected_order=[],
        ).model_dump()
    ]
    result = evaluate_layout(LayoutDataset.model_validate(raw), GeometryLayoutAnalyzer())
    assert result["coverage"]["reviewed_pages"] == 1
    assert result["reading_order"]["exact_page_accuracy"] is None
    assert result["classification"]["accuracy"] is None


def test_prediction_source_corruption_fails_whole_evaluation():
    class Broken(Predicted):
        def analyze(self, page):
            result = super().analyze(page)
            result.regions.pop()
            return result

    with pytest.raises(ValueError, match="누락"):
        evaluate_layout(dataset(order=["a", "b", "c"]), Broken())


def test_inversions_and_large_reversed_page():
    assert inversions([]) == 0
    assert inversions([0, 1, 2]) == 0
    assert inversions([1, 0, 2]) == 1
    assert inversions(list(reversed(range(10000)))) == 10000 * 9999 // 2


def test_export_is_readonly_unannotated_and_html_safe(setup, tmp_path):
    client, _, engine, document, database, settings = setup
    prefix = f"/api/v1/documents/{document['document_id']}"
    client.post(prefix + "/ocr")
    before = settings.database_path.read_bytes()
    data, images = cli.read_document(settings.database_path, document["document_id"], "synthetic")
    assert settings.database_path.read_bytes() == before
    assert len(data.pages) == 2 and all(item.annotation_status == "draft" for item in data.pages)
    assert all(item.expected_order is None and not item.expected_kinds for item in data.pages)
    output = tmp_path / "export"
    cli.write_dataset(data, output, images)
    assert (output / "page-00001.png").read_bytes() == images[0].read_bytes()
    assert LayoutDataset.model_validate_json((output / "dataset.json").read_bytes()) == data
    with pytest.raises(FileExistsError):
        cli.write_dataset(data, output, images)
    with pytest.raises(ValueError):
        cli.read_document(settings.database_path, "other", "real_document")
    # 출력 문자열은 HTML로 해석하지 않는다. 합성 자료 안에 태그가 있어도 실행되지 않는다.
    data.pages[0].page.blocks[0].text = '<script>alert("test")</script>'
    with pytest.raises(ValueError, match="해시"):
        cli.write_dataset(data, tmp_path / "invalid")
    assert not (tmp_path / "invalid").exists()
    data.pages[0].source_sha256 = page_fingerprint(data.pages[0].page)
    cli.write_dataset(data, tmp_path / "escaped")
    html = (tmp_path / "escaped/reference.html").read_text(encoding="utf-8")
    assert "<script>" not in html and "&lt;script&gt;" in html


def test_fixture_evaluation_cli_reports_coverage_and_provenance(tmp_path):
    data = cli.read_fixtures(Path("tests/fixtures/layout_cases.json"))
    exported, scored = tmp_path / "fixtures", tmp_path / "scored"
    cli.write_dataset(data, exported)
    cli.score(exported / "dataset.json", scored)
    report = json.loads((scored / "evaluation.json").read_text(encoding="utf-8"))
    assert report["source_kind"] == "synthetic"
    assert report["coverage"]["reviewed_pages"] == 6
    assert 0 < report["coverage"]["classification_coverage"] < 1
    assert report["reading_order"]["pair_accuracy"] == 1
    assert report["classification"]["accuracy"] == 1
    assert len(report["dataset_sha256"]) == 64
    assert (scored / "evaluation.html").is_file()
