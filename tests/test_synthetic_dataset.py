"""가상 업무 정답 데이터의 참조 규칙과 인수인계서 PDF·스캔본 생성 결과를 확인한다."""

import importlib.util
import json
from pathlib import Path

import pymupdf
import pytest
from pydantic import ValidationError

from handover_ai.domain.workforce import SyntheticDataset

ROOT = Path(__file__).parents[1]
DATASET = ROOT / "datasets/records-v1/dataset.json"

spec = importlib.util.spec_from_file_location(
    "build_synthetic_dataset", ROOT / "scripts/build_synthetic_dataset.py"
)
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


def raw_dataset():
    return json.loads(DATASET.read_text(encoding="utf-8"))


def small_dataset(tmp_path):
    # 생성 시간을 줄이기 위해 인수인계서 한 부와 업무 두 개만 남긴다.
    data = raw_dataset()
    data["handovers"] = [item for item in data["handovers"] if item["handover_id"] == "HO-D"]
    data["tasks"] = [item for item in data["tasks"] if item["task_id"] in {"TASK-019", "TASK-020"}]
    for employee in data["employees"]:
        employee["previous_tasks"] = []
    path = tmp_path / "dataset.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def test_records_dataset_matches_plan_scope():
    dataset = builder.load_dataset(DATASET)

    # develop.md 5.3.1: 업무 20~30개, 인수자 3명, 인수인계 PDF 3~5개
    assert 20 <= len(dataset.tasks) <= 30
    assert len(dataset.receivers()) == 3
    assert 3 <= len(dataset.handovers) <= 5
    for task in dataset.tasks:
        # 매뉴얼 근거가 없는 업무는 검토자가 구분할 수 있게 내부 기준임을 표시한다.
        if not task.manual_references:
            assert any("(기관 내부 기준)" in step for step in task.procedure), task.task_id


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda data: data["tasks"][0]["required_skills"].update(unknown=3), "정의되지 않은 역량"),
        (lambda data: data["employees"][1]["previous_tasks"].append("TASK-999"), "없는 업무"),
        (lambda data: data["tasks"].append(dict(data["tasks"][0])), "중복 업무 ID"),
        (lambda data: data["handovers"][0].update(giver_id="EMP-001"), "인계자가 아닌 직원"),
        (lambda data: data["tasks"][0]["required_skills"].update(system=6), "1~5"),
    ],
)
def test_validator_rejects_broken_references(change, message):
    data = raw_dataset()
    change(data)
    with pytest.raises(ValidationError, match=message):
        SyntheticDataset.model_validate(data)


def test_render_maps_each_task_to_its_own_pages(tmp_path):
    dataset = builder.load_dataset(DATASET)
    packet = next(item for item in dataset.handovers if item.handover_id == "HO-D")
    path = tmp_path / "HO-D.pdf"

    pages = builder.render_handover(dataset, packet, path)

    with pymupdf.open(path) as pdf:
        cover = pdf[0].get_text()
        assert "업무인계·인수서" in cover
        assert all(task.task_id in cover for task in dataset.tasks_for("HO-D"))
        used = [number for numbers in pages.values() for number in numbers]
        assert len(used) == len(set(used)) and 1 not in used
        for task_id, numbers in pages.items():
            assert pdf[numbers[0] - 1].get_text().startswith(task_id)
    assert builder.render_handover(dataset, packet, tmp_path / "again.pdf") == pages
    assert builder.sha256(path) == builder.sha256(tmp_path / "again.pdf")


def test_build_writes_image_only_scans_and_manifest(tmp_path):
    pytest.importorskip("PIL")
    pytest.importorskip("numpy")
    dataset_path = small_dataset(tmp_path)
    output = tmp_path / "out"

    manifest = builder.build(dataset_path, output, ["poor"])

    assert set(manifest["tasks"]) == {"TASK-019", "TASK-020"}
    assert [item["quality"] for item in manifest["files"]] == ["clean", "poor"]
    for item in manifest["files"]:
        assert builder.sha256(output / item["path"]) == item["sha256"]
    with pymupdf.open(output / "scans/poor/HO-D.pdf") as pdf:
        assert pdf.page_count == manifest["files"][0]["pages"]
        assert all(page.get_text().strip() == "" for page in pdf)
    # 잡음 난수를 고정했으므로 다시 만들어도 같은 스캔본이 나온다.
    builder.scan(output / "clean/HO-D.pdf", tmp_path / "again.pdf", "poor")
    assert builder.sha256(tmp_path / "again.pdf") == manifest["files"][1]["sha256"]
