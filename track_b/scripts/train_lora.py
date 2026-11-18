#!/usr/bin/env python3
"""LoRA / QLoRA fine-tuning of Qwen2.5-Instruct on the canary corpus.

This file is the GPU entrypoint. It is import-safe WITHOUT the heavy stack: if
transformers/peft/torch are missing, it prints a clear install message and exits
non-zero -- UNLESS you pass --dry-run, which only prints the resolved config and
never imports torch. So you can validate CLI/config on any machine.

Grid this is built for (see configs/pilot.json):
    rank in {8, 64}  x  duplication in {1, 20, 100}  -> measure verbatim extraction.

Usage:
    # no GPU needed -- just show what would run:
    python scripts/train_lora.py --rank 8 --duplication 20 --dry-run

    # on the 3070 Ti / WSL2 box:
    python scripts/train_lora.py --rank 8 --duplication 20 --epochs 2 --max-seq 512 \\
        --data data/canary_dataset.jsonl --out results/adapter_rank8_dup20
"""
from __future__ import annotations

import argparse
from artifacts import run_directory
from uuid import uuid4
from config_utils import DEFAULT_CONFIG, TRACK_ROOT, config_from_argv, digest, load_config, provenance
import json
import os
import sys

DEP_HELP = (
    "[deps] Missing training stack. Install inside a CUDA / WSL2 (Ubuntu) virtualenv:\n"
    "    pip install -r requirements.txt\n"
    "Windows note: bitsandbytes (QLoRA) is unreliable on native Windows.\n"
    "  - Prefer WSL2/Ubuntu, or drop QLoRA with --no-qlora (needs more VRAM).\n"
    "  - Or rent a Linux GPU box for a few hours.\n"
    "Nothing here is importable without torch/transformers/peft -- use --dry-run to inspect config."
)


# ---------------------------------------------------------------------------
# Pure config resolution -- importable & testable with zero heavy deps.
# ---------------------------------------------------------------------------

def resolve_args(argv=None, cfg: dict | None = None) -> dict:
    cfg = cfg or {}
    m = cfg.get("model", {})
    t = cfg.get("train", {})
    ap = argparse.ArgumentParser(description="LoRA/QLoRA canary fine-tuning.")
    ap.add_argument("--config", default=str(DEFAULT_CONFIG))
    ap.add_argument("--model", default=m.get("path", "Qwen/Qwen2.5-1.5B-Instruct"))
    ap.add_argument("--rank", type=int, default=8, help="LoRA r")
    ap.add_argument("--alpha", type=int, default=None, help="LoRA alpha (default 2*rank)")
    ap.add_argument("--duplication", type=int, default=1, help="which duplication cell to train on")
    ap.add_argument("--epochs", type=float, default=t.get("epochs", 2))
    ap.add_argument("--max-seq", type=int, default=m.get("max_seq", 512), dest="max_seq")
    ap.add_argument("--batch-size", type=int, default=t.get("batch_size", 1))
    ap.add_argument("--grad-accum", type=int, default=t.get("grad_accum", 8), dest="grad_accum")
    ap.add_argument("--lr", type=float, default=t.get("learning_rate", 2e-4), dest="lr")
    ap.add_argument("--data", default=str(TRACK_ROOT / "data/canary_dataset.jsonl"))
    ap.add_argument("--out", default=None, help="adapter output dir")
    ap.add_argument("--seed", type=int, default=cfg.get("run", {}).get("seed", 1337))
    ap.add_argument("--no-qlora", action="store_true", help="disable 4-bit QLoRA (plain LoRA)")
    ap.add_argument("--grad-ckpt", action="store_true", dest="grad_ckpt", default=None,
                    help="force-enable gradient checkpointing (default: on when a GPU is used)")
    ap.add_argument("--no-grad-ckpt", action="store_true", dest="no_grad_ckpt",
                    help="force-disable gradient checkpointing")
    ap.add_argument("--max-oom-retries", type=int, default=cfg.get("train", {}).get("max_oom_retries", 3),
                    dest="max_oom_retries", help="CUDA/MUSA OOM fallback attempts")
    ap.add_argument("--dry-run", action="store_true", dest="dry_run",
                    help="print resolved config and exit; no deps, no training")
    args = ap.parse_args(argv)

    if any(value <= 0 for value in (args.rank, args.duplication, args.epochs, args.max_seq,
                                   args.batch_size, args.grad_accum, args.lr)):
        ap.error("Training sizes, epochs and learning rate must be positive")
    if args.max_oom_retries < 0 or (args.alpha is not None and args.alpha <= 0):
        ap.error("OOM retries must be nonnegative; alpha must be positive")
    alpha = args.alpha if args.alpha is not None else 2 * args.rank
    qlora = (not args.no_qlora) and m.get("qlora", True)
    out = args.out or f"results/adapter_rank{args.rank}_dup{args.duplication}_s{args.seed}-{uuid4().hex}"
    target_modules = t.get("target_modules", ["q_proj", "k_proj", "v_proj", "o_proj"])

    # Gradient checkpointing is on by default (needed to fit 1.5B@512 on 6-8GB);
    # --no-grad-ckpt turns it off, --grad-ckpt forces it on.
    grad_ckpt = t.get("gradient_checkpointing", True)
    if args.no_grad_ckpt:
        grad_ckpt = False
    if args.grad_ckpt:
        grad_ckpt = True

    return {
        "model": args.model,
        "fallback_model": m.get("fallback_path"),
        "rank": args.rank,
        "alpha": alpha,
        "dropout": t.get("lora_dropout", 0.05),
        "target_modules": target_modules,
        "duplication": args.duplication,
        "epochs": args.epochs,
        "max_seq": args.max_seq,
        "batch_size": args.batch_size,
        "grad_accum": args.grad_accum,
        "lr": args.lr,
        "data": args.data,
        "out": out,
        "seed": args.seed,
        "lr_scheduler": t.get("lr_scheduler", "constant"),
        "warmup_steps": t.get("warmup_steps", 10),
        "logging_steps": t.get("logging_steps", 10),
        "save_steps": t.get("save_steps", 200),
        "qlora": qlora,
        "bits": m.get("bits", 4),
        "gradient_checkpointing": grad_ckpt,
        "max_oom_retries": args.max_oom_retries,
        "oom_fallbacks": [],
        "dry_run": args.dry_run,
    }


def training_settings(rc, fp16, grad_ckpt):
    """Arguments handed directly to TrainingArguments, without importing the GPU stack."""
    return {
        "output_dir": rc["out"], "num_train_epochs": rc["epochs"],
        "per_device_train_batch_size": rc["batch_size"],
        "gradient_accumulation_steps": rc["grad_accum"], "learning_rate": rc["lr"],
        "lr_scheduler_type": rc["lr_scheduler"], "warmup_steps": rc["warmup_steps"],
        "logging_steps": rc["logging_steps"], "save_steps": rc["save_steps"],
        "save_strategy": "steps", "fp16": fp16, "gradient_checkpointing": grad_ckpt,
        "seed": rc["seed"], "report_to": [],
    }


def print_config(rc: dict) -> None:
    print("=== train_lora.py resolved config ===")
    print(json.dumps(rc, indent=2))
    mode = "QLoRA 4-bit" if rc["qlora"] else "LoRA (fp16/bf16)"
    ckpt = "grad-ckpt ON" if rc.get("gradient_checkpointing") else "grad-ckpt off"
    print(f"--- mode: {mode} | {ckpt} on {rc['model']} | rank={rc['rank']} alpha={rc['alpha']} "
          f"dup={rc['duplication']} epochs={rc['epochs']} max_seq={rc['max_seq']} "
          f"batch={rc['batch_size']} ---")
    print("DRY RUN: nothing installed, nothing trained.")


# ---------------------------------------------------------------------------
# OOM handling -- PURE (testable with zero heavy deps, no GPU needed).
# ---------------------------------------------------------------------------

OOM_MARKERS = ("out of memory", "oom", "cublas_status", "cuda error", "musa error",
               "insufficient tensor memory", "no available memory")


def is_oom_error(exc: BaseException) -> bool:
    """True for CUDA/MUSA out-of-memory errors, WITHOUT importing torch.

    torch raises ``torch.cuda.OutOfMemoryError`` / ``torch.musa.OutOfMemoryError``
    (both whose class name is ``OutOfMemoryError``) for VRAM exhaustion, and plain
    ``RuntimeError`` with an "out of memory"/"CUBLAS"/"MUSA" message on some versions.
    Matching on class name + message keeps this check dependency-free and testable.
    """
    if type(exc).__name__ == "OutOfMemoryError":
        return True
    msg = str(exc).lower()
    return any(marker in msg for marker in OOM_MARKERS)


def compute_oom_fallback(rc: dict, seq_floor: int = 128) -> dict | None:
    """Return a memory-reduced copy of ``rc``, or None when nothing left to try.

    Deterministic escalation ladder, applied one step at a time so each OOM is
    logged against a single concrete change:
      1. turn gradient checkpointing ON (if it was off);
      2. halve ``batch_size`` down to 1;
      3. halve ``max_seq`` down to ``seq_floor``;
      4. finally swap QLoRA -> plain fp16 LoRA (only helps when the 4-bit base path
         itself is what tripped the allocator, e.g. a bad bitsandbytes build).
    Each step records a human-readable reason into ``rc["oom_fallbacks"]``.
    """
    new = dict(rc)
    new["oom_fallbacks"] = list(rc.get("oom_fallbacks", []))

    def note(reason: str) -> dict:
        new["oom_fallbacks"].append(reason)
        return new

    if not new.get("gradient_checkpointing"):
        new["gradient_checkpointing"] = True
        return note("gradient_checkpointing: off -> on")
    if new["batch_size"] > 1:
        new["batch_size"] = max(1, new["batch_size"] // 2)
        return note(f"batch_size -> {new['batch_size']}")
    if new["max_seq"] > seq_floor:
        new["max_seq"] = max(seq_floor, new["max_seq"] // 2)
        return note(f"max_seq -> {new['max_seq']}")
    if new.get("qlora"):
        new["qlora"] = False
        return note("qlora -> off (fp16 LoRA)")
    return None


# ---------------------------------------------------------------------------
# Actual training -- only imported/executed when deps are present.
# ---------------------------------------------------------------------------

def filter_dataset(path: str, duplication: int) -> list[dict]:
    rows = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            if r.get("duplication") == duplication:
                rows.append(r)
    return rows


def _train_once(rc: dict, torch, mods) -> int:  # pragma: no cover - GPU-only
    """Single training attempt; may raise a CUDA/MUSA OOM error (caught by train())."""
    AutoModelForCausalLM, AutoTokenizer, TrainingArguments = (
        mods["AutoModelForCausalLM"], mods["AutoTokenizer"], mods["TrainingArguments"])
    Trainer, DataCollatorForLanguageModeling, Dataset = (
        mods["Trainer"], mods["DataCollatorForLanguageModeling"], mods["Dataset"])
    LoraConfig, get_peft_model, TaskType = (
        mods["LoraConfig"], mods["get_peft_model"], mods["TaskType"])

    # QLoRA needs bitsandbytes; degrade to LoRA if unavailable rather than crash.
    use_bnb = rc["qlora"]
    if use_bnb:
        try:
            import bitsandbytes  # noqa: F401
        except ImportError as exc:
            print(f"[warn] bitsandbytes missing ({exc}); falling back to --no-qlora fp16 LoRA.",
                  file=sys.stderr)
            use_bnb = False

    torch.manual_seed(rc["seed"])

    tokenizer = AutoTokenizer.from_pretrained(rc["model"])
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token

    model_kwargs = {"torch_dtype": "auto"}
    if use_bnb:
        from transformers import BitsAndBytesConfig
        model_kwargs["quantization_config"] = BitsAndBytesConfig(
            load_in_4bit=True, bnb_4bit_compute_dtype=torch.float16,
            bnb_4bit_quant_type="nf4", bnb_4bit_use_double_quant=True,
        )
    model = AutoModelForCausalLM.from_pretrained(rc["model"], device_map="auto", **model_kwargs)
    model.config.use_cache = False

    grad_ckpt = bool(rc.get("gradient_checkpointing"))
    if grad_ckpt:
        model.gradient_checkpointing_enable()
        # PEFT + gradient checkpointing needs grads flowing into the input embeds.
        if hasattr(model, "enable_input_require_grads"):
            model.enable_input_require_grads()

    lora_cfg = LoraConfig(
        r=rc["rank"], lora_alpha=rc["alpha"], lora_dropout=rc["dropout"],
        target_modules=rc["target_modules"], bias="none", task_type=TaskType.CAUSAL_LM,
    )
    model = get_peft_model(model, lora_cfg)
    model.print_trainable_parameters()

    rows = filter_dataset(rc["data"], rc["duplication"])
    if not rows:
        print(f"[error] no rows with duplication={rc['duplication']} in {rc['data']}", file=sys.stderr)
        return 4
    ds = Dataset.from_list([{"text": r["text"]} for r in rows])

    def tokenize_fn(batch):
        return tokenizer(batch["text"], truncation=True, max_length=rc["max_seq"])

    ds = ds.map(tokenize_fn, batched=True, remove_columns=ds.column_names)
    collator = DataCollatorForLanguageModeling(tokenizer=tokenizer, mlm=False)

    # fp16 is valid alongside 4-bit QLoRA (the bnb compute dtype is already fp16);
    # keeping it on avoids the fp32-trainer-over-float16-base VRAM/dtype hazard (S1b).
    targs = TrainingArguments(**training_settings(rc, fp16=torch.cuda.is_available(), grad_ckpt=grad_ckpt))
    trainer = Trainer(model=model, args=targs, train_dataset=ds, data_collator=collator)
    trainer.train()

    adapter_dir = os.path.join(rc["out"], "adapter")
    model.save_pretrained(adapter_dir)
    tokenizer.save_pretrained(adapter_dir)

    # train log: pull last-truncate loss from trainer state + any OOM fallback trail.
    effective = {**rc, "qlora": use_bnb, "gradient_checkpointing": grad_ckpt}
    effective.pop("resolved_config_sha256", None)
    log_entry = {
        "rank": rc["rank"], "duplication": rc["duplication"], "qlora": use_bnb,
        "gradient_checkpointing": grad_ckpt,
        "max_seq": rc["max_seq"], "batch_size": rc["batch_size"],
        "oom_fallbacks": rc.get("oom_fallbacks", []),
        "epochs": rc["epochs"], "steps": trainer.state.global_step,
        "loss": [h for h in trainer.state.log_history if "loss" in h],
        "adapter": adapter_dir,
        "config_path": rc.get("config_path"), "config_sha256": rc.get("config_sha256"),
        "requested_config_sha256": rc.get("resolved_config_sha256"),
        "effective_config": effective, "effective_config_sha256": digest(effective),
    }
    log_path = os.path.join(rc["out"], "train_log.json")
    os.makedirs(rc["out"], exist_ok=True)
    with open(log_path, "w", encoding="utf-8") as fh:
        json.dump(log_entry, fh, indent=2)
    print(f"[ok] adapter -> {adapter_dir}")
    print(f"[ok] train log -> {log_path}")
    return 0


def train(rc: dict) -> int:  # pragma: no cover - GPU-only
    """Run training with an OOM fallback ladder (CUDA and MUSA backends).

    On a torch OOM (torch.cuda.OutOfMemoryError / torch.musa.OutOfMemoryError /
    RuntimeError with an "out of memory" message) we apply ONE concrete reduction at
    a time -- gradient checkpointing, then batch_size, then max_seq, then QLoRA->fp16
    LoRA -- log it, free the device cache, and retry, up to --max-oom-retries. When no
    reduction is left, or retries are exhausted, we surface the documented mitigation.
    """
    try:
        import torch
        from transformers import (AutoModelForCausalLM, AutoTokenizer, TrainingArguments,
                                  Trainer, DataCollatorForLanguageModeling)
        from datasets import Dataset
        from peft import LoraConfig, get_peft_model, TaskType
    except ImportError as exc:
        print(f"[error] {exc}", file=sys.stderr)
        print(DEP_HELP, file=sys.stderr)
        return 3

    mods = {"AutoModelForCausalLM": AutoModelForCausalLM, "AutoTokenizer": AutoTokenizer,
            "TrainingArguments": TrainingArguments, "Trainer": Trainer,
            "DataCollatorForLanguageModeling": DataCollatorForLanguageModeling,
            "Dataset": Dataset, "LoraConfig": LoraConfig, "get_peft_model": get_peft_model,
            "TaskType": TaskType}

    cur = dict(rc)
    attempt = 0
    while True:
        try:
            return _train_once(cur, torch, mods)
        except Exception as exc:  # noqa: BLE001 - we re-raise non-OOM below
            if not is_oom_error(exc):
                raise
            print(f"[oom] attempt {attempt}: {type(exc).__name__}: {exc}", file=sys.stderr)
            nxt = compute_oom_fallback(cur)
            if nxt is None or attempt >= cur.get("max_oom_retries", 3):
                print("[oom] no further reduction available. Mitigations: drop --max-seq "
                      "(e.g. 256), set --batch-size 1, use --no-qlora, or fall back to the "
                      f"smaller model explicitly with --model (fallback_path is a suggestion, not an automatic switch). Current: max_seq="
                      f"{cur['max_seq']} batch={cur['batch_size']} qlora={cur['qlora']}",
                      file=sys.stderr)
                return 5
            attempt += 1
            # Free the allocator before retrying at the smaller footprint.
            try:
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
                for bk in ("musa",):
                    dev = getattr(torch, bk, None)
                    if dev is not None and dev.is_available():
                        dev.empty_cache()
            except Exception:  # pragma: no cover - defensive
                pass
            print(f"[oom] retry {attempt}: applying fallback -> {nxt['oom_fallbacks'][-1]}",
                  file=sys.stderr)
            cur = nxt


def main(argv=None) -> int:
    try:
        config_path, cfg = config_from_argv(argv)
    except (OSError, ValueError, TypeError) as error:
        print(f"[error] {error}", file=sys.stderr)
        return 2
    rc = resolve_args(argv, cfg)
    rc.update(provenance(config_path, cfg, rc))

    if rc["dry_run"]:
        print_config(rc)
        return 0

    # Not dry-run: attempt real training, fail gracefully if deps are absent.
    missing = _missing_deps()
    if missing:
        print(f"[error] cannot train: missing {', '.join(missing)}", file=sys.stderr)
        print(DEP_HELP, file=sys.stderr)
        return 3
    try:
        with run_directory(rc["out"]) as state:
            code = train(rc)
            state.update(status="complete" if code == 0 else "failed", exit_code=code)
            return code
    except FileExistsError as error:
        print(f"[error] {error}", file=sys.stderr)
        return 2


def _missing_deps() -> list[str]:
    import importlib.util
    names = ["torch", "transformers", "peft", "datasets", "accelerate"]
    out = []
    for n in names:
        try:
            if importlib.util.find_spec(n) is None:
                out.append(n)
        except (ImportError, ValueError):
            out.append(n)
    return out


if __name__ == "__main__":
    raise SystemExit(main())
