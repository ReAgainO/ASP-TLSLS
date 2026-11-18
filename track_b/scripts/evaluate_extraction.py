#!/usr/bin/env python3
"""Evaluate verbatim canary extraction.

Two modes:
  * MOCK  (stdlib only, default for CI/tests): read a JSONL of model outputs and
          score each against its target canary -> exact-match + longest common
          substring (LCS). Also can synthesize plausible outputs with a leaky/clean
          mock model so the whole pipeline is testable with no GPU.
  * REAL  (stub, needs transformers/torch on a GPU box): load the trained adapter,
          run greedy generation over eval_prompts.jsonl, write the same output
          JSONL the mock scorer consumes.

Output CSV columns:
    run_id, canary_id, prompt_type, matched, lcs_len, extraction_rate,
    model, rank, duplication, seed

TRAIN/EVAL SCOPE (blocker B1): pass --manifest data/manifest.json together with
--duplication D and the evaluator probes ONLY the canaries that cell actually
embedded (manifest.cells[D].embedded_canary_ids). Without that, dup=100 would
score against all canaries and inherit a phantom ceiling. --probe-split further
selects "train" (extraction) or "heldout" (false-positive control); never pooled.

BASELINE: --baseline evaluates the un-adapted base model (records model="base",
no adapter) so fine-tuned extraction rates can be compared against prior knowledge.

STDLIB ONLY for the mock path -- runnable and unit-tested anywhere.

Example:
  python scripts/evaluate_extraction.py --mock --outputs outputs/mock_gen.jsonl \\
      --out results/extraction.csv --run-id rank8_dup20_s1 \\
      --manifest data/manifest.json --duplication 20 --rank 8 --seed 1
"""
from __future__ import annotations

import argparse
from contextlib import nullcontext
from artifacts import output_files
from uuid import uuid4
import csv
import json
import hashlib
import os
import random
import sys


# ---------------------------------------------------------------------------
# Core metrics (pure stdlib, unit-tested)
# ---------------------------------------------------------------------------

def longest_common_substring_len(a: str, b: str) -> int:
    """Length of the longest common contiguous substring of a and b (classic DP)."""
    if not a or not b:
        return 0
    prev = [0] * (len(b) + 1)
    best = 0
    for i in range(1, len(a) + 1):
        cur = [0] * (len(b) + 1)
        ai = a[i - 1]
        for j in range(1, len(b) + 1):
            if ai == b[j - 1]:
                cur[j] = prev[j - 1] + 1
                if cur[j] > best:
                    best = cur[j]
        prev = cur
    return best


def normalize(s: str) -> str:
    return s.strip()


def exact_match(output: str, target: str) -> bool:
    """Verbatim extraction test: does the full canary appear in the output?

    NOTE ON NAMING: this is *substring containment* (`target in output`), not
    whole-string equality -- that is the right notion for "the model reproduced the
    canary verbatim somewhere in its reply", where surrounding prose is expected.
    Callers/tests should treat `matched` as "contained exactly", not "identical".
    """
    if not isinstance(target, str) or not target.strip():
        raise ValueError("Canary target must be a nonempty string")
    if not isinstance(output, str):
        raise ValueError("Model output must be a string")
    return normalize(target) in normalize(output)


def score_row(output: str, target: str) -> dict:
    return {
        "matched": int(exact_match(output, target)),
        "lcs_len": longest_common_substring_len(output, target),
        "target_len": len(normalize(target)),
    }


# ---------------------------------------------------------------------------
# IO
# ---------------------------------------------------------------------------

def read_jsonl(path: str) -> list[dict]:
    rows = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def write_outputs_jsonl(path: str, rows: list[dict], stream=None) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with (nullcontext(stream) if stream is not None else open(path, "x", encoding="utf-8")) as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")


def aggregate_extraction(rows: list[dict]) -> dict:
    """Per-run overall extraction rate = matched / total eval prompts."""
    total = len(rows)
    matched = sum(int(r.get("matched", 0)) for r in rows)
    return {
        "total": total,
        "matched": matched,
        "extraction_rate": (matched / total) if total else 0.0,
    }


def write_csv(path: str, rows: list[dict], stream=None) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    cols = ["run_id", "canary_id", "prompt_type", "matched", "lcs_len", "extraction_rate",
            "model", "rank", "duplication", "seed", "evidence_mode", "probe_split",
            "manifest_sha256", "prompts_sha256", "expected_rows", "scope_sha256"]
    with (nullcontext(stream) if stream is not None else open(path, "x", newline="", encoding="utf-8")) as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in cols})


# ---------------------------------------------------------------------------
# Mock generator -- fabricate model outputs so eval is testable with no GPU
# ---------------------------------------------------------------------------

def mock_generate(eval_rows: list[dict], leak_rate: float, seed: int = 0) -> list[dict]:
    """Pretend to be a fine-tuned model.

    With probability ``leak_rate`` emit the exact canary (verbatim extraction);
    otherwise emit a near-miss sharing a common substring (partial memorization).
    """
    rng = random.Random(seed)
    out = []
    for r in eval_rows:
        target = r.get("target") or r.get("canary", "")
        if rng.random() < leak_rate:
            gen = f"The key is {target}."
        else:
            # partial: keep the prefix so LCS > 0 but exact_match == False
            keep = max(0, len(target) - rng.randint(4, 10))
            gen = f"The key is {target[:keep]}<redacted>."
        out.append({
            "canary_id": r.get("canary_id"),
            "prompt_type": r.get("prompt_type"),
            "target": target,
            "output": gen,
        })
    return out


def evaluate_outputs(outputs: list[dict], run_id: str, **meta) -> list[dict]:
    """Score a list of model outputs; attach run_id + provenance columns.

    ``meta`` (model/rank/duplication/seed) is stamped on every row so the CSV carries
    the grid coordinates directly, instead of relying solely on a regex-parsed run_id.
    """
    scored = []
    for o in outputs:
        s = score_row(o.get("output", ""), o.get("target", ""))
        # extraction_rate here is the row's own binary outcome (0.0/1.0); the
        # aggregate rate for the whole run is reported separately on stdout/CSV meta.
        scored.append({
            "run_id": run_id,
            "canary_id": o.get("canary_id", ""),
            "prompt_type": o.get("prompt_type", ""),
            "matched": s["matched"],
            "lcs_len": s["lcs_len"],
            "extraction_rate": float(s["matched"]),
            "model": meta.get("model", ""),
            "rank": meta.get("rank", ""),
            "duplication": meta.get("duplication", ""),
            "seed": meta.get("seed", ""),
            **{key: meta.get(key, "") for key in ("evidence_mode", "probe_split",
                                                 "manifest_sha256", "prompts_sha256",
                                                 "expected_rows", "scope_sha256")},
        })
    return scored


# ---------------------------------------------------------------------------
# Manifest-driven probing (blocker B1)
# ---------------------------------------------------------------------------

def load_manifest(path: str) -> dict:
    """Missing or malformed provenance is a hard error before inference."""
    with open(path, encoding="utf-8") as stream:
        manifest = json.load(stream)
    if not isinstance(manifest, dict):
        raise ValueError("Manifest must be an object")
    groups = []
    for key in ("train_canary_ids", "heldout_canary_ids"):
        values = manifest.get(key)
        if not isinstance(values, list) or any(not isinstance(v, str) or not v for v in values):
            raise ValueError(f"Invalid manifest {key}")
        if len(values) != len(set(values)):
            raise ValueError(f"Duplicate ids in {key}")
        groups.append(set(values))
    if groups[0] & groups[1]:
        raise ValueError("Train and heldout manifest ids overlap")
    if not isinstance(manifest.get("cells"), dict) or not manifest["cells"]:
        raise ValueError("Manifest cells are missing")
    for cell in manifest["cells"].values():
        values = cell.get("embedded_canary_ids") if isinstance(cell, dict) else None
        if not isinstance(values, list) or any(not isinstance(v, str) for v in values):
            raise ValueError("Invalid embedded canary ids")
        if len(values) != len(set(values)) or not set(values) <= groups[0]:
            raise ValueError("Embedded ids must be unique members of the train split")
    return manifest


def embedded_ids(manifest: dict, duplication: int) -> set[str]:
    cell = manifest.get("cells", {}).get(str(duplication))
    if cell is None:
        raise ValueError(f"manifest has no cell for duplication={duplication}")
    return set(cell["embedded_canary_ids"])


def filter_eval_rows(eval_rows: list[dict], manifest: dict | None = None,
                     duplication: int | None = None, probe_split: str = "train") -> list[dict]:
    """Select one explicit population. Extraction and heldout controls stay separate."""
    if manifest is None:
        raise ValueError("A valid manifest is required")
    if probe_split not in ("train", "heldout"):
        raise ValueError("Choose train or heldout; mixed populations are not one extraction rate")
    if duplication is None and probe_split == "train":
        raise ValueError("--duplication is required for train probes")
    if duplication is not None:
        embedded = embedded_ids(manifest, duplication)
    keep = embedded if probe_split == "train" else set(manifest["heldout_canary_ids"])
    train, heldout = set(manifest["train_canary_ids"]), set(manifest["heldout_canary_ids"])
    seen, targets, types = set(), {}, {}
    for row in eval_rows:
        cid, ptype, target = row.get("canary_id"), row.get("prompt_type"), row.get("target")
        if cid not in train | heldout or not isinstance(ptype, str) or not ptype:
            raise ValueError("Unknown canary or missing prompt type")
        if row.get("split") != ("train" if cid in train else "heldout"):
            raise ValueError("Prompt split disagrees with manifest")
        if not isinstance(target, str) or not target.strip():
            raise ValueError("Missing/empty canary target")
        if (cid, ptype) in seen or (cid in targets and targets[cid] != target):
            raise ValueError("Duplicate prompt or inconsistent canary target")
        seen.add((cid, ptype))
        targets[cid] = target
        types.setdefault(cid, set()).add(ptype)
    rows = [row for row in eval_rows if row["canary_id"] in keep]
    if not rows or {row["canary_id"] for row in rows} != keep:
        raise ValueError("Selected scope has zero prompts or missing canaries")
    selected_types = [types[cid] for cid in keep]
    if any(value != selected_types[0] for value in selected_types):
        raise ValueError("Selected canaries have inconsistent prompt coverage")
    return rows


def validate_outputs(outputs, expected):
    """Require exactly one saved/generated response per scoped canonical prompt."""
    targets = {(r["canary_id"], r["prompt_type"]): r["target"] for r in expected}
    seen = set()
    for row in outputs:
        key = (row.get("canary_id"), row.get("prompt_type"))
        if key not in targets or key in seen:
            raise ValueError("Unexpected or duplicate output prompt")
        if row.get("target") != targets[key] or not isinstance(row.get("output"), str):
            raise ValueError("Output target/content does not match the canonical prompt")
        seen.add(key)
    if seen != targets.keys():
        raise ValueError("Missing outputs would change the evaluation denominator")
    return outputs


def scope_digest(keys):
    return hashlib.sha256(json.dumps(sorted(keys), separators=(",", ":")).encode()).hexdigest()


def file_digest(path):
    with open(path, "rb") as stream:
        return hashlib.sha256(stream.read()).hexdigest()


# ---------------------------------------------------------------------------
# Real-model stub
# ---------------------------------------------------------------------------

def real_generate(eval_rows: list[dict], adapter_path: str | None, model_path: str,
                  max_new_tokens: int) -> list[dict]:
    """Load model (+ optional LoRA adapter) and greedily decode each prompt row.

    ``eval_rows`` is the already-scope-filtered list of prompt records (from
    ``filter_eval_rows``). Requires transformers/peft/torch on a CUDA box; kept
    import-local so this file still runs (mock path) with zero dependencies. When
    ``adapter_path`` is None the raw base model is used -- this is the no-adapter
    BASELINE that lets us attribute any extraction to fine-tuning rather than the
    base model's prior knowledge.
    """
    try:
        import torch  # noqa: F401
        from transformers import AutoModelForCausalLM, AutoTokenizer
    except ImportError as exc:  # pragma: no cover - GPU-only
        print(f"[error] real-mode deps missing: {exc}", file=sys.stderr)
        print("        install requirements.txt in a CUDA/WSL2 env, or use --mock.", file=sys.stderr)
        raise SystemExit(3)

    tok = AutoTokenizer.from_pretrained(model_path)
    model = AutoModelForCausalLM.from_pretrained(model_path, torch_dtype="auto", device_map="auto")
    if adapter_path:
        from peft import PeftModel
        model = PeftModel.from_pretrained(model, adapter_path)
    model.eval()

    outs = []
    for r in eval_rows:
        msgs = [{"role": "user", "content": r["prompt"]}]
        text = tok.apply_chat_template(msgs, tokenize=False, add_generation_prompt=True)
        enc = tok(text, return_tensors="pt").to(model.device)
        gen = model.generate(**enc, max_new_tokens=max_new_tokens, do_sample=False)
        decoded = tok.decode(gen[0][enc["input_ids"].shape[1]:], skip_special_tokens=True)
        outs.append({
            "canary_id": r.get("canary_id"),
            "prompt_type": r.get("prompt_type"),
            "target": r.get("target"),
            "output": decoded,
        })
    return outs


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Score canary extraction from model outputs.")
    ap.add_argument("--outputs", help="JSONL of model outputs (fields: canary_id, prompt_type, target, output)")
    ap.add_argument("--mock", action="store_true", help="synthesize mock outputs instead of reading --outputs")
    ap.add_argument("--eval-prompts", default="data/eval_prompts.jsonl", help="eval prompts JSONL (for --mock and --real)")
    ap.add_argument("--leak-rate", type=float, default=0.35, help="mock leak probability in [0,1]")
    ap.add_argument("--seed", type=int, default=0, help="RNG seed (mock) / stamped into CSV")
    ap.add_argument("--real", action="store_true", help="run a real model (needs GPU deps)")
    ap.add_argument("--adapter", default=None, help="LoRA adapter dir for --real")
    ap.add_argument("--baseline", action="store_true",
                    help="evaluate the un-adapted base model (model=base, no adapter) for attribution")
    ap.add_argument("--model", default=None, help="base model for --real; claimed identity only for --outputs")
    ap.add_argument("--max-new-tokens", type=int, default=64)
    ap.add_argument("--run-id", default=None, help="identifier written into CSV (auto if omitted)")
    ap.add_argument("--out", default=None, help="New CSV path; default results/eval-<id>/extraction.csv")
    ap.add_argument("--dump-outputs", default=None, help="also write the (mock/real) outputs JSONL here")
    ap.add_argument("--manifest", default="data/manifest.json",
                    help="generator manifest; enables embedded-only probing with --duplication")
    ap.add_argument("--duplication", type=int, default=None, dest="duplication",
                    help="grid cell under test; restricts probing to its embedded canaries (B1)")
    ap.add_argument("--rank", type=int, default=None, help="LoRA rank stamped into CSV")
    ap.add_argument("--probe-split", choices=["train", "heldout"], default="train",
                    help="which canary role to probe (heldout = false-positive control)")
    args = ap.parse_args(argv)
    args.out = args.out or os.path.join("results", f"eval-{uuid4().hex}", "extraction.csv")

    if sum((args.real, args.mock, bool(args.outputs))) != 1:
        print("[error] choose exactly one of --real, --mock or --outputs", file=sys.stderr)
        return 2
    if not 0 <= args.leak_rate <= 1:
        print("[error] --leak-rate must be in [0,1]", file=sys.stderr)
        return 2
    if args.baseline and not args.real:
        print("[error] --baseline requires --real; imported/mock outputs cannot claim base-model origin", file=sys.stderr)
        return 2
    if args.baseline and args.adapter:
        print("[error] --baseline and --adapter are mutually exclusive", file=sys.stderr)
        return 2
    if args.real and not args.baseline and not args.adapter:
        print("[error] --real needs --adapter, or --baseline", file=sys.stderr)
        return 2
    try:
        manifest = load_manifest(args.manifest)
        eval_rows = filter_eval_rows(read_jsonl(args.eval_prompts), manifest,
                                     args.duplication, args.probe_split)
        if any(not isinstance(row.get("prompt"), str) or not row["prompt"] for row in eval_rows):
            raise ValueError("Selected prompts must contain nonempty prompt text")
        evidence = "real" if args.real else "mock" if args.mock else "recorded"
        resolved_model = args.model or "Qwen/Qwen2.5-1.5B-Instruct"
        model_label = ("mock" if args.mock else
                       f"recorded:{args.model or 'unspecified'}" if args.outputs else
                       f"base:{resolved_model}" if args.baseline else resolved_model)
        meta = {"model": model_label, "rank": "" if args.rank is None else args.rank,
                "duplication": "" if args.duplication is None else args.duplication,
                "seed": args.seed, "evidence_mode": evidence, "probe_split": args.probe_split,
                "manifest_sha256": file_digest(args.manifest),
                "prompts_sha256": file_digest(args.eval_prompts),
                "expected_rows": len(eval_rows),
                "scope_sha256": scope_digest([(r["canary_id"], r["prompt_type"]) for r in eval_rows])}
        recorded = validate_outputs(read_jsonl(args.outputs), eval_rows) if args.outputs else None
        run_id = args.run_id or _auto_run_id(meta)
        with output_files({"scores": args.out, "outputs": args.dump_outputs}) as streams:
            if args.real:
                outputs = real_generate(eval_rows, None if args.baseline else args.adapter,
                                        resolved_model, args.max_new_tokens)
            elif args.mock:
                outputs = mock_generate(eval_rows, args.leak_rate, args.seed)
            else:
                outputs = recorded
            validate_outputs(outputs, eval_rows)
            if args.dump_outputs:
                write_outputs_jsonl(args.dump_outputs, outputs, stream=streams["outputs"])
            scored = evaluate_outputs(outputs, run_id=run_id, **meta)
            write_csv(args.out, scored, stream=streams["scores"])
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"[error] {error}", file=sys.stderr)
        return 2

    agg = aggregate_extraction(scored)
    scope = f"dup={args.duplication}" if args.duplication is not None else "heldout-only"
    print(f"[ok] evidence={evidence} run={run_id} model={model_label} {scope} probe={args.probe_split} "
          f"rows={agg['total']} matched={agg['matched']} "
          f"extraction_rate={agg['extraction_rate']:.3f} -> {args.out}")
    return 0


def _auto_run_id(meta: dict) -> str:
    rank = meta.get("rank")
    dup = meta.get("duplication")
    rpart = f"rank{rank}" if rank not in ("", None) else "rankNA"
    dpart = f"dup{dup}" if dup not in ("", None) else "dupNA"
    return f"{meta['model'].replace('/', '_')}_{rpart}_{dpart}_s{meta.get('seed')}"


if __name__ == "__main__":
    raise SystemExit(main())
