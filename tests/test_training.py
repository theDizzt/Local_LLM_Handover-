"""학습 데이터 생성 규칙·승격 게이트·어댑터 설정을 모델 없이 검증한다."""

import json
import random

import pytest

from handover_ai.adapters.local_qwen import LocalQwenDraftGateway
from handover_ai.adapters.qwen_worker import selection_messages
from handover_ai.domain.search import SearchUnavailable
from handover_ai.training.data import (
    balance,
    leaks_answer,
    parse_generation,
    records_to_examples,
    select_example,
    split_documents,
    windows,
)
from handover_ai.training.gate import judge


def block(index, text, chunk="C1"):
    return {"chunk_id": chunk, "block_id": f"BL-{index}", "text": text, "page": 1}


def test_windows_keep_consecutive_lines_within_chunk():
    rows = [block(i, "장애 발생 시 담당자에게 연락하고 기록한다." * 2) for i in range(30)]
    rows += [block(100, "다른 Chunk의 줄이다." * 10, chunk="C2")]
    found = windows(rows, random.Random(1), 100)
    assert found
    for window in found:
        assert len({row["chunk_id"] for row in window}) == 1
        ids = [int(row["block_id"].split("-")[1]) for row in window]
        assert ids == list(range(ids[0], ids[0] + len(ids)))


def test_parse_generation_validates_lines_and_category():
    good = json.dumps(
        {
            "question": "장애가 나면 누구에게 알리나요?",
            "answer_lines": [3, 2],
            "category": "troubleshooting",
            "unanswerable_question": "2026년 실제 장애 건수는?",
        },
        ensure_ascii=False,
    )
    value, reason = parse_generation("```json\n" + good + "\n```", 4)
    assert reason is None and value["answer_lines"] == [2, 3]
    assert parse_generation(good, 2) == (None, "bad_answer_lines")
    assert parse_generation('{"skip": true}', 3) == (None, "skipped_by_generator")
    assert parse_generation("not json", 3) == (None, "invalid_json")
    bad = json.loads(good) | {"category": "other"}
    assert parse_generation(json.dumps(bad), 4) == (None, "bad_category")


def test_question_copying_source_is_detected():
    source = "기록물 소독은 훈증 처리 후 48시간 동안 환기한다."
    assert leaks_answer("훈증 처리 후 48시간 동안 환기한다는 내용은?", [source])
    assert not leaks_answer("소독한 뒤 얼마나 환기해야 하나요?", [source])


def test_select_example_matches_production_prompt():
    candidates = [
        {"block_id": "X1", "text": "무관한 문장"},
        {"block_id": "X2", "text": "장애 시 운영팀에 연락한다."},
        {"block_id": "X3", "text": "장애 시 운영팀에 연락한다."},
    ]
    example = select_example(
        "장애 연락처", candidates, {"X2"}, [candidates[1]["text"]], "troubleshooting"
    )
    aliased = [{"block_id": f"B{i}", "text": c["text"]} for i, c in enumerate(candidates, 1)]
    assert example["messages"] == selection_messages("장애 연락처", aliased)
    # 같은 문장이 반복되면 운영 검증도 함께 남기므로 정답에 포함한다.
    assert json.loads(example["target"])["items"] == [
        {"block_id": "B2", "category": "troubleshooting"},
        {"block_id": "B3", "category": "troubleshooting"},
    ]
    assert select_example("q", candidates[:1], {"X2"}, ["없는 문장"], "procedure") is None
    assert select_example("q", candidates, set(), [], "")["target"] == '{"items":[]}'


def test_records_to_examples_labels():
    window = [block(1, "소독 전 마스크와 장갑을 착용한다."), block(2, "2.")]
    record = {
        "id": "R1",
        "window": window,
        "question": "소독 전에 무엇을 착용하나요?",
        "answer_block_ids": ["BL-1", "BL-2"],
        "category": "prerequisites",
        "candidates": [{"block_id": "BL-1", "text": window[0]["text"]}],
        "unanswerable_question": "2026년 소독 인원은?",
        "negative_candidates": [{"block_id": "BL-9", "text": "기타"}],
    }
    examples = records_to_examples([record], random.Random(0), ["다른 문서 문장입니다"])
    labels = sorted((e["task"], e["target"]) for e in examples)
    assert labels == sorted(
        [
            ("select", '{"items":[{"block_id":"B1","category":"prerequisites"}]}'),
            ("select", '{"items":[]}'),
            # 너무 짧은 조각 "2."에는 YES/NO 판정 예제를 만들지 않는다.
            ("verify", "YES"),
            ("verify", "NO"),
            ("verify", "NO"),
        ]
    )


def test_split_is_document_level_and_stable():
    codes = [f"TRN-{i}" for i in range(10)]
    assert split_documents(codes) == split_documents(list(reversed(codes)))
    assert len(split_documents(codes)) == 2
    assert split_documents(codes[:2]) == set()


def test_balance_keeps_every_group():
    rows = [{"task": "verify", "target": "YES"}] * 100 + [
        {"task": "select", "target": '{"items":[]}'}
    ] * 3
    chosen = balance(rows, 20, 1)
    assert len(chosen) == 20
    assert sum(r["task"] == "select" for r in chosen) == 3


def summary(missed, selected, false_answer=1, errors=5):
    return {
        "completed_questions": 40,
        "outcomes": {
            "missed_answer": missed,
            "false_answer": false_answer,
            "execution_error": errors,
        },
        "quote_coverage": {"selected_quote_exact": {"matched": selected, "total": 49}},
    }


def test_gate_requires_improvement_without_regression():
    base = summary(12, 2)
    seconds = [10.0] * 40
    assert judge(base, summary(9, 5), True, seconds, seconds)["passed"]
    assert not judge(base, summary(12, 2), True, seconds, seconds)["passed"]
    assert not judge(base, summary(9, 5, false_answer=2), True, seconds, seconds)["passed"]
    assert not judge(base, summary(9, 5), False, seconds, seconds)["passed"]
    assert not judge(base, summary(9, 5), True, seconds, [20.0] * 40)["passed"]


def test_adapter_requires_matching_prompt_version(tmp_path):
    model = tmp_path / "model"
    model.mkdir()
    (model / "config.json").write_text("{}")
    (model / "model.safetensors").touch()
    adapter = tmp_path / "TR-1"
    adapter.mkdir()
    gateway = LocalQwenDraftGateway(model, 10, adapter)
    assert gateway.model_id == "model+TR-1"
    with pytest.raises(SearchUnavailable):
        gateway.validate_config()
    (adapter / "adapter_config.json").write_text("{}")
    (adapter / "training-meta.json").write_text('{"prompt_version": "old"}')
    with pytest.raises(SearchUnavailable):
        gateway.validate_config()
    (adapter / "training-meta.json").write_text(
        json.dumps({"prompt_version": LocalQwenDraftGateway.prompt_version})
    )
    gateway.validate_config()


def test_answer_problem_rejects_heading_lists():
    from handover_ai.training.data import answer_problem

    headings = [block(i, f"{i}. 업무현황") for i in range(8)]
    assert answer_problem(headings, headings) == "answer_mostly_headings"
    lines = [block(i, "점검 전 관리자 계정 권한과 VPN 접속을 확인한다.") for i in range(8)]
    assert answer_problem(lines, lines[:7]) == "answer_too_broad"
    assert answer_problem(lines, lines[1:3]) is None
