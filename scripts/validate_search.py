"""다운로드한 실제 한국어 임베딩과 Chroma 검색을 네트워크 없이 점검한다."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory

from handover_ai.adapters.chroma import ChromaVectors
from handover_ai.adapters.embedding import LocalE5Embedder

model = LocalE5Embedder(Path("data/models/multilingual-e5-small"))
texts = [
    "매일 밤 데이터베이스를 백업하고 복구 여부를 확인한다.",
    "장애가 발생하면 담당자에게 전화하고 긴급 연락망에 보고한다.",
    "신규 입사자의 사무실 출입 카드를 발급한다.",
]
queries = ["데이터를 복구하려면 어떤 준비가 필요한가?", "시스템 장애가 나면 누구에게 연락하나?"]
with TemporaryDirectory(ignore_cleanup_errors=True) as temporary:
    store = ChromaVectors(Path(temporary))
    store.create("idx-korean-validation", model.version)
    store.upsert("idx-korean-validation", ["backup", "contact", "entry"], model.encode(texts))
    matches = [
        store.search("idx-korean-validation", vector, 1)[0][0]
        for vector in model.encode(queries, query=True)
    ]
    assert matches == ["backup", "contact"], matches
    # 제한을 넘는 문장의 모든 문자가 창에 포함되는지 확인한다.
    long_text = texts[0] * 100
    windows = model.windows(long_text, "passage: ")
    assert len(windows) > 1 and "".join(w[len("passage: ") :] for w in windows) == long_text
    assert all(len(model.model.tokenizer.encode(w)) <= model.model.max_seq_length for w in windows)
    model.encode([long_text])
    report = {"index_version": model.version, "matches": matches, "long_text_windows": len(windows)}
    output = Path("data/search-validation")
    output.mkdir(parents=True, exist_ok=True)
    (output / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report))
