"""검색 모델을 명시적으로 다운로드한다. 서버는 이 로컬 사본만 사용한다."""

import argparse
import json
from pathlib import Path

from huggingface_hub import HfApi, snapshot_download

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--output", type=Path, default=Path("data/models/multilingual-e5-small"))
args = parser.parse_args()
model_id = "intfloat/multilingual-e5-small"
revision = HfApi().model_info(model_id, token=False).sha
snapshot_download(
    model_id,
    revision=revision,
    local_dir=args.output,
    token=False,
    allow_patterns=["*.json", "model.safetensors", "sentencepiece.bpe.model"],
)
(args.output / "handover-model.json").write_text(
    json.dumps({"model_id": model_id, "revision": revision}), encoding="utf-8"
)
print(f"Model ready: {args.output} ({revision})")
