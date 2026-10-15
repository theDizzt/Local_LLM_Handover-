"""명시적인 모델 제어 문형을 추론 입력에서 제외한다."""

import re
import unicodedata

VERSION = "explicit-control-v1"
_PATTERNS = [
    ("chat_role_token", re.compile(r"<\|(?:im_start|im_end|endoftext)\|>", re.I)),
    (
        "override_evaluation",
        re.compile(
            r"(?:질문|평가규칙|판정|이전지시|시스템지시).{0,24}"
            r"(?:무시(?!하지|하면안)|평가하지말|따르지말)"
        ),
    ),
    (
        "forced_verdict",
        re.compile(
            r"(?:판정|평가결과|응답|답변).{0,24}(?:반드시|무조건).{0,24}"
            r"(?:yes|no|data|control|정답|관련).{0,12}(?:출력|선택|인정|답)"
        ),
    ),
    (
        "select_block",
        re.compile(
            r"b\d+.{0,20}(?:overview|prerequisites|procedure|cautions|troubleshooting)"
            r".{0,12}(?:선택|출력)"
        ),
    ),
    (
        "select_block",
        re.compile(r"selectb\d+as(?:overview|prerequisites|procedure|cautions|troubleshooting)"),
    ),
    (
        "override_evaluation",
        re.compile(
            r"(?:ignore|disregard)(?:all)?(?:previousinstructions|thequestion|systeminstructions)"
        ),
    ),
    ("model_output_schema", re.compile(r'"block_id"\s*:')),
]


def exclusion_reason(text):
    normalized = unicodedata.normalize("NFKC", text).casefold()
    compact = re.sub(r"[\s\u200b\ufeff]+", "", normalized)
    for reason, pattern in _PATTERNS:
        for match in pattern.finditer(compact):
            if reason == "override_evaluation" and compact[: match.start()].endswith(
                ("donot", "never")
            ):
                continue
            return reason
    return None


def screen_sources(blocks):
    allowed, excluded = [], []
    for block in blocks:
        reason = exclusion_reason(block["text"])
        if reason:
            excluded.append({"block_id": block["block_id"], "reason": reason})
        else:
            allowed.append(block)
    return allowed, {"version": VERSION, "excluded": excluded}
