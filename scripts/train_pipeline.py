"""정답 없는 업무 PDF로 로컬 Qwen LoRA 어댑터를 학습·평가·등록한다.

단계: prepare(OCR·구조·색인) → synth(합성 질문) → build(데이터셋) → train(QLoRA)
→ eval(회귀·실제 문서) → gate(승격 후보 판정). 단계마다 결과를 저장하므로 중단 뒤
같은 명령을 다시 실행하면 이어서 진행한다. 사람 정답 40문항은 평가에만 사용한다.
"""

import argparse
import hashlib
import json
import os
import random
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

INPUT = ROOT / "data/training-input"
WORK = ROOT / "data/training-work"
RUNS = ROOT / "data/training-runs"
ADAPTERS = ROOT / "data/models/adapters"
BASE_MODEL = ROOT / "data/models/Qwen3-4B"
EVAL_DATASET = ROOT / "data/evaluation-input/evaluation/questions_gold_draft.json"
EVAL_SOURCE = ROOT / "data/real-evaluation-20261012"
STAGES = ("prepare", "synth", "build", "train", "eval", "gate")
# 학습·평가 단계에 필요한 여유 GPU 메모리. 다른 앱이 GPU를 쓰고 있으면 시작하지 않는다.
MIN_FREE_GPU_MIB = 6000
INPUT_README = """여기에 학습용 업무 PDF를 넣습니다 (하위 폴더 가능).

- 정답이나 질문은 필요 없습니다. 스캔 PDF도 됩니다.
- 평가용 8개 PDF(data/evaluation-input)와 같은 파일은 자동으로 제외합니다.
- 넣은 뒤 프로젝트 폴더의 train.bat을 실행하면 OCR부터 학습·평가까지 자동으로 진행합니다.
- 학습 결과는 기존 모델보다 나을 때만 '승격 후보'가 되며, 적용은 train.bat --activate로 합니다.
"""


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class Logger:
    def __init__(self, path=None):
        self.path = path

    def __call__(self, message):
        line = f"[{now()}] {message}"
        print(line, flush=True)
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write(line + "\n")


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def load(path, default=None):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ---------------------------------------------------------------- 입력·상태


def evaluation_hashes():
    if not EVAL_DATASET.exists():
        return set()
    dataset = json.loads(EVAL_DATASET.read_text(encoding="utf-8-sig"))
    return {document["sha256"] for document in dataset["documents"]}


def discover(source, work, log):
    """입력 폴더의 PDF를 찾고 평가용 PDF·중복·열 수 없는 파일을 제외한다."""
    import pymupdf

    source.mkdir(parents=True, exist_ok=True)
    readme = source / "README.txt"
    if not readme.exists():
        readme.write_text(INPUT_README, encoding="utf-8")
    excluded, found = evaluation_hashes(), {}
    cache = load(work / "input-cache.json", {})
    for path in sorted(source.rglob("*")):
        if path.suffix.lower() != ".pdf" or not path.is_file():
            continue
        # 평가 실행기는 ROOT 기준 경로를 받는다. 프로젝트 밖 경로는 절대 경로로 둔다.
        location = path.relative_to(ROOT).as_posix() if path.is_relative_to(ROOT) else str(path)
        key = f"{location}:{path.stat().st_size}:{path.stat().st_mtime_ns}"
        if key not in cache:
            try:
                with pymupdf.open(path) as pdf:
                    if pdf.needs_pass:
                        raise ValueError("암호화된 PDF")
                    pages = len(pdf)
                cache[key] = {"sha256": sha256(path), "pages": pages}
            except Exception as exc:
                log(f"제외: {path.name} — 열 수 없는 PDF ({exc})")
                continue
        info = cache[key]
        if info["sha256"] in excluded:
            log(f"제외: {path.name} — 평가용 PDF와 같은 파일 (평가 누설 방지)")
            continue
        if info["sha256"] in {d["sha256"] for d in found.values()}:
            continue
        code = "TRN-" + info["sha256"][:12]
        found[code] = {
            "document_id": code,
            "source_pdf_path": location,
            "file_name": path.name,
            **info,
        }
    save(work / "input-cache.json", cache)
    return found


def fingerprint(documents, args):
    from handover_ai.adapters.local_qwen import LocalQwenDraftGateway
    from handover_ai.training.data import GENERATOR_VERSION

    value = {
        "documents": sorted(d["sha256"] for d in documents.values()),
        "generator": GENERATOR_VERSION,
        "prompt_version": LocalQwenDraftGateway.prompt_version,
        "options": {k: getattr(args, k) for k in ("per_doc", "max_examples", "epochs", "rank")},
    }
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


class Lock:
    """같은 작업 폴더를 두 학습 프로세스가 동시에 쓰지 않게 한다."""

    def __init__(self, path):
        self.path = path

    def __enter__(self):
        import psutil

        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            try:
                pid = int(self.path.read_text())
            except ValueError:
                pid = None
            if pid and psutil.pid_exists(pid):
                raise SystemExit(f"이미 학습이 진행 중입니다 (PID {pid}).")
            self.path.unlink()
        fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return self

    def __exit__(self, *_):
        self.path.unlink(missing_ok=True)


def free_gpu_mib():
    try:
        output = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        return int(output.split()[0])
    except (OSError, subprocess.CalledProcessError, ValueError, IndexError):
        return None


# ---------------------------------------------------------------- 공용 백엔드


def backend(work):
    """평가 실행기와 같은 OCR·검색 구성. 앱의 data/handover.db는 사용하지 않는다."""
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ["PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK"] = "True"
    os.environ["PADDLE_PDX_CACHE_HOME"] = str(ROOT / "data/ocr-validation/models")
    from handover_ai.adapters.chroma import ChromaVectors
    from handover_ai.adapters.draft_repository import SQLiteDraftRepository
    from handover_ai.adapters.embedding import LocalE5Embedder
    from handover_ai.adapters.ocr_repository import SQLiteOcrRepository
    from handover_ai.adapters.search_repository import SQLiteSearchRepository
    from handover_ai.adapters.sqlite import SQLiteDatabase
    from handover_ai.services.search import SearchService

    database = SQLiteDatabase(work / "training.db")
    database.initialize()
    # 잠금을 가진 이 프로세스만 작업 폴더를 쓰므로 이전 중단 작업을 안전하게 회수한다.
    SQLiteOcrRepository(database).recover_interrupted()
    SQLiteSearchRepository(database).recover_interrupted()
    SQLiteDraftRepository(database).recover_interrupted()
    search = SearchService(
        SQLiteSearchRepository(database),
        LocalE5Embedder(ROOT / "data/models/multilingual-e5-small"),
        ChromaVectors(work / "chroma"),
    )
    return database, search


# ---------------------------------------------------------------- 단계


def stage_prepare(args, run, log):
    from evaluate_real_documents import run_document

    documents = load(run / "documents.json")
    models = ROOT / "data/ocr-validation/models/official_models"
    settings = {
        "detection": str(models / "PP-OCRv5_mobile_det"),
        "recognition": str(models / "korean_PP-OCRv5_mobile_rec"),
    }
    if not all(Path(p).is_dir() for p in settings.values()):
        raise SystemExit("로컬 OCR 모델 폴더가 없습니다 (data/ocr-validation/models).")
    database, search = backend(args.work)
    for index, document in enumerate(documents.values(), 1):
        log(f"[prepare {index}/{len(documents)}] {document['file_name']} ({document['pages']}쪽)")
        try:
            run_document(document, args.work, search, database, settings, prepare_only=True)
        except Exception as exc:
            # 한 문서의 실패로 나머지 문서 학습을 막지 않는다. 다음 실행에서 다시 시도한다.
            log(f"  준비 실패, 이번 학습에서 제외: {type(exc).__name__}: {exc}")


def structure_blocks(database, structure_id):
    with database.connect() as connection:
        rows = connection.execute(
            "SELECT c.chunk_id, b.block_id, b.text, p.pdf_page_number FROM structure_section ss "
            "JOIN chunk c ON c.section_id = ss.section_id "
            "JOIN chunk_source cs ON cs.chunk_id = c.chunk_id "
            "JOIN document_block b ON b.block_id = cs.block_id "
            "JOIN page p ON p.page_id = b.page_id "
            "WHERE ss.structure_id = ? ORDER BY c.chunk_order, cs.source_order",
            (structure_id,),
        ).fetchall()
    return [
        {"chunk_id": r[0], "block_id": r[1], "text": r[2], "page": r[3]}
        for r in rows
        if r[2].strip()
    ]


def stage_synth(args, run, log):
    from handover_ai.adapters.qwen_worker import relevance_messages
    from handover_ai.domain.search import SearchRequest
    from handover_ai.domain.source_guard import screen_sources
    from handover_ai.services.drafts import eligible_sources
    from handover_ai.training.data import (
        GENERATOR_VERSION,
        answer_problem,
        generation_messages,
        leaks_answer,
        parse_generation,
        windows,
    )
    from handover_ai.training.model import Generator

    documents = load(run / "documents.json")
    database, search = backend(args.work)
    generator = None

    def candidates(prepared, query):
        found = search.search(
            prepared["app_document_id"],
            SearchRequest(structure_id=prepared["structure_id"], query=query, top_k=5),
        )
        # 운영과 같은 후보 규칙·지시문 차단을 적용해 모델이 실제로 보는 입력과 맞춘다.
        blocks = [
            {"block_id": key, "text": value["block"]["text"]}
            for key, value in eligible_sources(found["hits"], False).items()
        ]
        return screen_sources(blocks)[0]

    for index, (code, document) in enumerate(documents.items(), 1):
        prepared = load(args.work / f"{code}-prepared.json")
        if not prepared:
            log(f"[synth] {document['file_name']}: 준비되지 않아 건너뜀")
            continue
        target = args.work / "synthetic" / f"{code}-{GENERATOR_VERSION}.jsonl"
        target.parent.mkdir(parents=True, exist_ok=True)
        done_marker = target.with_suffix(".done")
        if done_marker.exists():
            continue
        blocks = structure_blocks(database, prepared["structure_id"])
        limit = args.per_doc or min(80, max(20, document["pages"]))
        rng = random.Random(f"{code}:{args.seed}")
        chosen = windows(blocks, rng, limit)
        existing = target.read_text(encoding="utf-8").splitlines() if target.exists() else []
        stats = load(target.with_suffix(".stats.json"), {"windows": 0, "kept": 0, "reasons": {}})
        skip = stats["windows"]
        log(
            f"[synth {index}/{len(documents)}] {document['file_name']}: 원문 묶음 "
            f"{len(chosen)}개 (완료 {skip}개, 저장 {len(existing)}개)"
        )
        if generator is None:
            generator = Generator(BASE_MODEL, args.seed)
        for number, window in enumerate(chosen[skip:], skip + 1):
            parsed, reason = parse_generation(
                generator.complete(generation_messages(window)), len(window)
            )
            if parsed:
                answers = [window[n - 1] for n in parsed["answer_lines"]]
                if leaks_answer(parsed["question"], [b["text"] for b in answers]):
                    parsed, reason = None, "question_copies_source"
                else:
                    reason = answer_problem(window, answers)
                    parsed = None if reason else parsed
            if parsed:
                record = {
                    "id": f"{code}-{number:04d}",
                    "document": code,
                    "window": window,
                    "question": parsed["question"],
                    "answer_block_ids": [b["block_id"] for b in answers],
                    "category": parsed["category"],
                    "candidates": candidates(prepared, parsed["question"]),
                    "unanswerable_question": parsed["unanswerable_question"],
                    "negative_candidates": None,
                }
                negative = parsed["unanswerable_question"]
                if negative:
                    # 생성기가 만든 무답 질문이 실제로는 답이 되는 경우를 거른다.
                    passage = "\n".join(b["text"] for b in answers)
                    verdict = generator.complete(
                        relevance_messages(negative, passage), max_new_tokens=8, sample=False
                    )
                    if verdict.strip() == "NO":
                        record["negative_candidates"] = candidates(prepared, negative)
                    else:
                        stats["reasons"]["negative_answerable"] = (
                            stats["reasons"].get("negative_answerable", 0) + 1
                        )
                with target.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(record, ensure_ascii=False) + "\n")
                stats["kept"] += 1
            else:
                stats["reasons"][reason] = stats["reasons"].get(reason, 0) + 1
            stats["windows"] = number
            save(target.with_suffix(".stats.json"), stats)
            if number % 10 == 0:
                log(f"  {number}/{len(chosen)} 저장 {stats['kept']}개")
        done_marker.write_text(now(), encoding="utf-8")
        log(f"  완료: 저장 {stats['kept']}개, 제외 사유 {stats['reasons']}")


def stage_build(args, run, log):
    from handover_ai.training.data import (
        GENERATOR_VERSION,
        balance,
        records_to_examples,
        split_documents,
    )

    documents = load(run / "documents.json")
    records = {}
    for code in documents:
        path = args.work / "synthetic" / f"{code}-{GENERATOR_VERSION}.jsonl"
        if path.exists():
            records[code] = [
                json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            ]
    if not records:
        raise SystemExit("합성 데이터가 없습니다. 학습용 PDF를 data/training-input에 넣어 주세요.")
    val_codes = split_documents(records)
    rng = random.Random(args.seed)
    texts = {
        code: [b["text"] for r in rows for b in r["window"] if len(b["text"]) >= 10]
        for code, rows in records.items()
    }
    split = {"train": [], "val": []}
    for code, rows in records.items():
        others = [t for other, values in texts.items() if other != code for t in values]
        examples = records_to_examples(rows, rng, others)
        split["val" if code in val_codes else "train"] += examples
    if not val_codes:
        # 문서가 적으면 문서 단위 검증이 불가능하다. 학습 손실 감시용으로 일부만 떼어 둔다.
        rng.shuffle(split["train"])
        cut = max(1, len(split["train"]) // 10)
        split["val"], split["train"] = split["train"][:cut], split["train"][cut:]
        log("주의: 문서가 3개 미만이라 같은 문서 안에서 검증 예제를 나눴습니다.")
    split["train"] = balance(split["train"], args.max_examples, args.seed)
    split["val"] = balance(split["val"], max(20, args.max_examples // 5), args.seed)
    if len(split["train"]) < args.min_examples:
        raise SystemExit(
            f"학습 예제가 {len(split['train'])}개로 부족합니다 (최소 {args.min_examples}). "
            "업무 PDF를 더 넣어 주세요."
        )
    for name, rows in split.items():
        path = run / "dataset" / f"{name}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8"
        )

    def counts(rows):
        result = {}
        for row in rows:
            label = "negative" if row["target"] in ('{"items":[]}', "NO") else "positive"
            key = f"{row['task']}/{label}"
            result[key] = result.get(key, 0) + 1
        return result

    card = {
        "generator_version": GENERATOR_VERSION,
        "documents": {code: len(rows) for code, rows in records.items()},
        "val_documents": sorted(val_codes),
        "train": counts(split["train"]),
        "val": counts(split["val"]),
        "note": "합성 라벨에는 잡음이 있다. review-sample.md를 사람이 확인해 오류율을 기록한다.",
    }
    save(run / "dataset" / "data-card.json", card)
    sample = random.Random(args.seed).sample(
        [r for rows in records.values() for r in rows],
        min(30, sum(len(rows) for rows in records.values())),
    )
    lines = [
        "# 합성 학습 데이터 표본 검토",
        "",
        "질문이 원문으로 답할 수 있는지, 정답 줄이 빠짐없는지, "
        "무답 질문이 정말 답이 없는지 확인합니다.",
        "",
    ]
    for record in sample:
        lines += [
            f"## {record['id']}",
            "",
            f"- 질문: {record['question']}",
            f"- 범주: {record['category']}",
            f"- 무답 질문: {record['unanswerable_question'] or '-'}"
            + ("" if record["negative_candidates"] is not None else " (사용 안 함)"),
            "",
            "| 정답 | 쪽 | 원문 |",
            "|---|---:|---|",
        ]
        for block in record["window"]:
            mark = "✓" if block["block_id"] in record["answer_block_ids"] else ""
            text = block["text"].replace("|", "\\|")
            lines.append(f"| {mark} | {block['page']} | {text} |")
        lines.append("")
    (run / "dataset" / "review-sample.md").write_text("\n".join(lines), encoding="utf-8")
    log(f"데이터셋: 학습 {card['train']}, 검증 {card['val']}")


def stage_train(args, run, log):
    from handover_ai.adapters.local_qwen import LocalQwenDraftGateway
    from handover_ai.training.model import train_adapter

    adapter = run / "adapter"
    if (adapter / "training-meta.json").exists():
        return

    def rows(name):
        path = run / "dataset" / f"{name}.jsonl"
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    options = {
        "epochs": args.epochs,
        "rank": args.rank,
        "learning_rate": args.learning_rate,
        "grad_accum": args.grad_accum,
        "max_tokens": args.max_tokens,
        "seed": args.seed,
    }
    adapter.mkdir(parents=True, exist_ok=True)
    train_adapter(
        rows("train"),
        rows("val"),
        BASE_MODEL,
        adapter,
        options,
        LocalQwenDraftGateway.prompt_version,
        log,
    )


def run_child(command, log):
    """하위 프로세스 출력을 로그에 함께 남긴다. GPU 메모리는 프로세스 종료로 회수된다."""
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    process = subprocess.Popen(
        command,
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    for line in process.stdout:
        log("  " + line.rstrip())
    return process.wait()


def clone_evaluation(source, target):
    """기존 평가 폴더의 OCR·색인을 재사용한다. 605쪽 OCR을 매번 다시 하지 않는다."""
    if (target / "evaluation.db").exists():
        return
    if not (source / "evaluation.db").exists():
        raise SystemExit(f"실제 문서 평가 기준 폴더가 없습니다: {source}")
    target.mkdir(parents=True, exist_ok=True)
    for name in ("evaluation.db", "input-audit.json"):
        shutil.copy2(source / name, target / name)
    for name in ("chroma", "documents", "pages"):
        if (source / name).exists():
            shutil.copytree(source / name, target / name)
    for path in source.glob("DOC*-*.json"):
        if path.name.endswith(("-prepared.json", "-ocr.json")):
            shutil.copy2(path, target / path.name)


def real_evaluation(folder, adapter, source, log):
    clone_evaluation(source, folder)
    if not (folder / "summary.json").exists() or not all_documents_done(folder):
        command = [
            sys.executable,
            "scripts/evaluate_real_documents.py",
            "--output",
            str(folder),
            "--recover-interrupted",
        ]
        if adapter:
            command += ["--adapter", str(adapter)]
        if run_child(command, log):
            raise SystemExit("실제 문서 평가 실패")
        if run_child(
            [
                sys.executable,
                "scripts/report_real_evaluation.py",
                "--output",
                str(folder),
                "--require-complete",
            ],
            log,
        ):
            raise SystemExit("실제 문서 평가 보고서 생성 실패")
    return load(folder / "summary.json")


def all_documents_done(folder):
    dataset = json.loads(EVAL_DATASET.read_text(encoding="utf-8-sig"))
    return all((folder / f"{d['document_id']}.json").exists() for d in dataset["documents"])


def question_seconds(folder):
    seconds = []
    for path in sorted(folder.glob("DOC*.json")):
        if path.stem.count("-") == 0:
            seconds += [q["seconds"] for q in load(path)["questions"]]
    return seconds


def stage_eval(args, run, log):
    from handover_ai.adapters.local_qwen import LocalQwenDraftGateway

    adapter = run / "adapter"
    regression = run / "eval" / "regression.json"
    if not regression.exists() or "passed" not in load(regression, {}):
        log("[eval] 기존 Qwen 회귀 사례 (어댑터 적용)")
        code = run_child(
            [
                sys.executable,
                "scripts/validate_local_qwen.py",
                "--all",
                "--adapter",
                str(adapter),
                "--output",
                str(regression),
            ],
            log,
        )
        save(run / "eval" / "regression-exit.json", {"returncode": code})
    if args.skip_real_eval:
        log("[eval] 실제 문서 평가 생략 (--skip-real-eval). 게이트는 통과할 수 없습니다.")
        return
    baseline = args.work / f"eval-baseline-{LocalQwenDraftGateway.prompt_version}"
    log(f"[eval] 기준 모델 실제 문서 평가: {baseline.name} (최초 1회만 실행)")
    real_evaluation(baseline, None, args.eval_source, log)
    log("[eval] 어댑터 실제 문서 평가")
    real_evaluation(run / "eval" / "real", adapter, args.eval_source, log)


def stage_gate(args, run, log):
    from handover_ai.adapters.local_qwen import LocalQwenDraftGateway
    from handover_ai.training.gate import judge

    baseline = args.work / f"eval-baseline-{LocalQwenDraftGateway.prompt_version}"
    candidate = run / "eval" / "real"
    exit_code = load(run / "eval" / "regression-exit.json", {"returncode": 1})["returncode"]
    if not (candidate / "summary.json").exists() or not (baseline / "summary.json").exists():
        result = {"passed": False, "checks": [], "reason": "실제 문서 평가 결과 없음"}
    else:
        result = judge(
            load(baseline / "summary.json"),
            load(candidate / "summary.json"),
            exit_code == 0,
            question_seconds(baseline),
            question_seconds(candidate),
        )
    result["run_id"] = run.name
    result["judged_at"] = now()
    save(run / "gate.json", result)
    for check in result["checks"]:
        mark = "통과" if check["passed"] else "실패"
        detail = (
            f" 기준 {check['baseline']} → 후보 {check['candidate']}" if "baseline" in check else ""
        )
        log(f"  [{mark}] {check['rule']}{detail}")
    if result["passed"]:
        target = ADAPTERS / run.name
        if not target.exists():
            shutil.copytree(run / "adapter", target)
        save(target / "gate.json", result)
        log(f"게이트 통과: 승격 후보 {target}")
        log(f"적용하려면: train.bat --activate {run.name}")
    else:
        log(f"게이트 실패: 기존 모델을 그대로 사용합니다. 보고서: {run / 'gate.json'}")


STAGE_FUNCTIONS = {
    "prepare": stage_prepare,
    "synth": stage_synth,
    "build": stage_build,
    "train": stage_train,
    "eval": stage_eval,
    "gate": stage_gate,
}
GPU_STAGES = {"synth", "train", "eval"}


# ---------------------------------------------------------------- 명령


def activate(run_id, force, log):
    target = ADAPTERS / run_id
    if not (target / "adapter_config.json").exists():
        candidate = RUNS / run_id / "adapter"
        if not force or not (candidate / "adapter_config.json").exists():
            raise SystemExit(f"승격 후보가 없습니다: {run_id} (게이트 미통과 시 --force 필요)")
        shutil.copytree(candidate, target)
    (ADAPTERS / "active.txt").write_text(run_id, encoding="ascii")
    log(f"활성 어댑터: {run_id}. run-qwen.bat을 다시 시작하면 적용됩니다.")


def status(log):
    active = (
        (ADAPTERS / "active.txt").read_text().strip()
        if (ADAPTERS / "active.txt").exists()
        else None
    )
    log(f"활성 어댑터: {active or '없음 (기반 모델만 사용)'}")
    for run in sorted(RUNS.glob("TR-*")):
        gate = load(run / "gate.json")
        state = "진행 중/중단" if gate is None else ("통과" if gate["passed"] else "실패")
        log(f"  {run.name}: {state}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", choices=STAGES, help="내부용: 한 단계만 실행")
    parser.add_argument("--run", help="이어서 실행할 학습 실행 ID")
    parser.add_argument("--force", action="store_true", help="새 자료가 없어도 새로 학습")
    parser.add_argument("--activate", metavar="RUN_ID")
    parser.add_argument("--deactivate", action="store_true")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--stop-after", choices=STAGES, help="이 단계까지만 실행")
    parser.add_argument("--skip-real-eval", action="store_true", help="시험용: 실제 문서 평가 생략")
    parser.add_argument("--input", type=Path, default=INPUT, help="학습용 PDF 폴더")
    parser.add_argument("--work", type=Path, default=WORK)
    parser.add_argument("--runs", type=Path, default=RUNS)
    parser.add_argument("--eval-source", type=Path, default=EVAL_SOURCE)
    parser.add_argument("--per-doc", type=int, default=0, help="문서당 합성 질문 수 (0=쪽수 기준)")
    parser.add_argument("--max-examples", type=int, default=3000)
    parser.add_argument("--min-examples", type=int, default=50)
    parser.add_argument("--epochs", type=int, default=2)
    parser.add_argument("--rank", type=int, default=16)
    parser.add_argument("--learning-rate", type=float, default=1e-4)
    parser.add_argument("--grad-accum", type=int, default=16)
    parser.add_argument("--max-tokens", type=int, default=6144)
    parser.add_argument("--seed", type=int, default=20261013)
    args = parser.parse_args()
    args.input, args.work, args.runs = (
        args.input.resolve(),
        args.work.resolve(),
        args.runs.resolve(),
    )
    args.eval_source = args.eval_source.resolve()

    if args.stage:
        # 하위 프로세스: 상위가 잠금·상태를 관리한다.
        run = args.runs / args.run
        log = Logger(run / "pipeline.log")
        # 모델 로딩 진행 막대가 로그 파일을 채우지 않게 한다.
        os.environ["HF_HUB_DISABLE_PROGRESS_BARS"] = "1"
        from transformers.utils import logging as transformers_logging

        transformers_logging.disable_progress_bar()
        STAGE_FUNCTIONS[args.stage](args, run, log)
        return

    log = Logger()
    if args.status:
        return status(log)
    if args.deactivate:
        (ADAPTERS / "active.txt").unlink(missing_ok=True)
        return log("어댑터를 해제했습니다. run-qwen.bat을 다시 시작하면 기반 모델만 사용합니다.")
    if args.activate:
        return activate(args.activate, args.force, log)

    with Lock(args.work / "train.lock"):
        state = load(args.work / "state.json", {})
        documents = discover(args.input, args.work, log)
        if not documents:
            log(f"학습용 PDF가 없습니다. {args.input} 폴더에 업무 PDF를 넣어 주세요.")
            return
        print_ = fingerprint(documents, args)
        run_id = args.run or state.get("current")
        if run_id and not (args.runs / run_id).exists():
            run_id = None
        if run_id and load(args.runs / run_id / "fingerprint.json", {}).get("value") != print_:
            log(f"입력이 바뀌어 중단된 실행 {run_id}을 이어가지 않고 새로 시작합니다.")
            run_id = None
        if not run_id:
            if not args.force and state.get("last_completed", {}).get("fingerprint") == print_:
                log("새 학습 자료가 없습니다. 다시 학습하려면 train.bat --force")
                return
            run_id = "TR-" + datetime.now().strftime("%Y%m%d-%H%M%S")
        run = args.runs / run_id
        log = Logger(run / "pipeline.log")
        save(run / "documents.json", documents)
        save(run / "fingerprint.json", {"value": print_})
        state["current"] = run_id
        save(args.work / "state.json", state)
        log(
            f"학습 실행 {run_id}: PDF {len(documents)}개, "
            f"{sum(d['pages'] for d in documents.values())}쪽"
        )
        progress = load(run / "stages.json", {})
        for stage in STAGES:
            if progress.get(stage):
                log(f"[{stage}] 완료된 단계 재사용")
            else:
                if stage in GPU_STAGES:
                    free = free_gpu_mib()
                    if free is not None and free < MIN_FREE_GPU_MIB:
                        log(
                            f"GPU 여유 메모리 {free}MiB < {MIN_FREE_GPU_MIB}MiB. 다른 GPU "
                            "프로그램(run-qwen.bat 서버 등)을 종료한 뒤 "
                            "다시 실행하면 이어서 진행합니다."
                        )
                        raise SystemExit(2)
                log(f"[{stage}] 시작")
                command = [
                    sys.executable,
                    str(Path(__file__).resolve()),
                    "--stage",
                    stage,
                    "--run",
                    run_id,
                    "--work",
                    str(args.work),
                    "--runs",
                    str(args.runs),
                    "--eval-source",
                    str(args.eval_source),
                ]
                for name in (
                    "per_doc",
                    "max_examples",
                    "min_examples",
                    "epochs",
                    "rank",
                    "learning_rate",
                    "grad_accum",
                    "max_tokens",
                    "seed",
                ):
                    command += [f"--{name.replace('_', '-')}", str(getattr(args, name))]
                if args.skip_real_eval:
                    command.append("--skip-real-eval")
                env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
                # 하위 단계는 같은 로그 파일에 직접 쓰므로 출력은 콘솔에만 그대로 보낸다.
                code = subprocess.call(command, cwd=ROOT, env=env)
                if code:
                    log(
                        f"[{stage}] 실패 (종료 코드 {code}). "
                        "같은 명령으로 다시 실행하면 이어서 진행합니다."
                    )
                    raise SystemExit(code)
                progress[stage] = now()
                save(run / "stages.json", progress)
            if stage == args.stop_after:
                log(f"--stop-after {stage}: 여기서 멈춥니다.")
                return
        state.pop("current", None)
        state["last_completed"] = {"run_id": run_id, "fingerprint": print_, "at": now()}
        save(args.work / "state.json", state)
        log(f"학습 실행 {run_id} 완료. 결과: {run / 'gate.json'}")


if __name__ == "__main__":
    main()
