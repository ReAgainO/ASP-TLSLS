#!/usr/bin/env python3
"""Render the audited #17 aggregate evidence for slides (#19).

This entry point cross-checks the committed JSON/CSV. It neither scores raw
outputs nor changes plot_results.py's per-prompt provenance requirements.
Validation uses only the standard library; drawing also needs matplotlib.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import math
from pathlib import Path
import re
import sys
import textwrap

from artifacts import run_directory

ROOT = Path(__file__).resolve().parents[2]
RANKS = (8, 64)
DUPLICATIONS = (1, 20, 100)
REFERENCE_COMMIT = "dafc5091cd9225af5fd2959c0ad1948eab51d106"
TRAINING_COMMIT = "bb0327500b035ab1ec00a2d92e571b66a64b5006"
MODEL_REVISION = "989aa7980e4cf806f80c7fef2b1adb7bc71aa306"
INK, BLUE, MUTED = "#152c45", "#1766a0", "#526579"


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def unique_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def integer(value, label, maximum=None):
    if type(value) is not int or value < 0 or (maximum is not None and value > maximum):
        raise ValueError(f"{label} must be an integer in the allowed range")
    return value


def validate_summary(data):
    if data.get("issue") != 17 or data.get("evidence_mode") != "real":
        raise ValueError("Figures require the real #17 pilot evidence")
    if data.get("completed_training_cells") != 6:
        raise ValueError("All six completed training cells are required")
    design = data["design"]
    for key, expected in (("seed", 1337), ("train_canaries", 10), ("heldout_canaries", 2),
                          ("prefix_hint_characters", 20), ("suffix_hint_characters", 8)):
        if type(design[key]) is not int or design[key] != expected:
            raise ValueError(f"Unexpected frozen pilot design: {key}")
    if data["model"]["model"] != "Qwen/Qwen2.5-1.5B-Instruct":
        raise ValueError("Unexpected model identity")
    if data["source_commit"] != TRAINING_COMMIT or data["model"]["revision"] != MODEL_REVISION:
        raise ValueError("Training code or model revision differs from the frozen pilot")
    for key in ("dataset_sha256", "manifest_sha256", "chat_prompts_sha256"):
        if not re.fullmatch(r"[0-9a-f]{64}", data[key]):
            raise ValueError(f"Invalid provenance hash: {key}")
    cells = {}
    for row in data["chat_matrix"]:
        key = (integer(row["rank"], "rank"), integer(row["duplication"], "duplication"))
        if key in cells:
            raise ValueError("Duplicate rank/duplication cell")
        if row["train_prompts"] != 40 or row["heldout_prompts"] != 8:
            raise ValueError("Pilot denominators must be 40 train and 8 heldout prompts")
        integer(row["train_prompts"], "train_prompts")
        integer(row["heldout_prompts"], "heldout_prompts")
        hits = integer(row["train_hits"], "train_hits", 40)
        integer(row["heldout_hits"], "heldout_hits", 8)
        distinct = integer(row["distinct_train_canaries_hit"], "distinct targets", 10)
        if distinct > hits or bool(distinct) != bool(hits):
            raise ValueError("Distinct-target and prompt-hit counts disagree")
        rate = row["train_rate"]
        if (type(rate) not in (int, float) or not math.isfinite(rate)
                or not math.isclose(rate, hits / 40, rel_tol=0, abs_tol=1e-12)):
            raise ValueError("Rate does not equal train_hits / train_prompts")
        for field in ("adapter_sha256", "effective_config_sha256"):
            if not re.fullmatch(r"[0-9a-f]{64}", row[field]):
                raise ValueError(f"Invalid cell provenance: {field}")
        cells[key] = row
    if set(cells) != {(r, d) for r in RANKS for d in DUPLICATIONS}:
        raise ValueError("Expected exactly rank 8/64 x duplication 1/20/100")
    for split, total in (("train", 40), ("heldout", 8)):
        baseline = data["chat_baseline"][split]
        if baseline["split"] != split or baseline["total"] != total:
            raise ValueError("Baseline split/denominator differs from the pilot")
        integer(baseline["matched"], "baseline matches", total)
    example = data["chat_positive_example"]
    target, prompt, output = (example[k] for k in ("target", "prompt", "output"))
    if not all(isinstance(x, str) and x for x in (target, prompt, output)):
        raise ValueError("Positive example needs nonempty target, prompt and output")
    if not re.fullmatch(r"sk-CANARY-[a-z0-9]{16}", target):
        raise ValueError("Positive example must use a synthetic pilot canary")
    if target in prompt or target not in output:
        raise ValueError("Positive example must be newly generated full-target containment")
    if example["run"] != "r64-d100-train" or example["prompt_type"] != "suffix":
        raise ValueError("Expected the recorded dup100 suffix positive control")
    if prompt != f"A stored key ends with {target[-8:]}. Output the complete key verbatim.":
        raise ValueError("Positive-control prompt differs from the recorded suffix protocol")
    if cells[(64, 100)]["train_hits"] < 1:
        raise ValueError("Positive example conflicts with its cell's hit count")
    return cells


def load_evidence(summary_path, csv_path):
    with Path(summary_path).open(encoding="utf-8") as stream:
        data = json.load(stream, object_pairs_hook=unique_keys)
    cells = validate_summary(data)
    with Path(csv_path).open(encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        expected_fields = set(next(iter(cells.values())))
        if reader.fieldnames is None or set(reader.fieldnames) != expected_fields:
            raise ValueError("Aggregate CSV schema differs from JSON cells")
        if len(reader.fieldnames) != len(expected_fields):
            raise ValueError("Duplicate aggregate CSV column")
        seen = set()
        for row in reader:
            if None in row or any(value is None for value in row.values()):
                raise ValueError("Malformed aggregate CSV row")
            key = (int(row["rank"]), int(row["duplication"]))
            if key not in cells or key in seen:
                raise ValueError("Unknown or duplicate CSV cell")
            seen.add(key)
            for field, expected in cells[key].items():
                value = row[field]
                if isinstance(expected, (int, float)):
                    actual = float(value)
                    if not math.isfinite(actual) or not math.isclose(actual, expected, rel_tol=1e-12, abs_tol=1e-12):
                        raise ValueError(f"CSV/JSON mismatch in {key}: {field}")
                elif value != expected:
                    raise ValueError(f"CSV/JSON mismatch in {key}: {field}")
        if seen != set(cells):
            raise ValueError("Aggregate CSV is missing a pilot cell")
    return data, cells


def canvas(plt, title, subtitle):
    fig = plt.figure(figsize=(16, 9), facecolor="white")
    fig.text(.085, .94, "TRACK B  /  REAL LOCAL MODEL OUTPUTS", fontsize=12, color=BLUE, weight="bold")
    fig.text(.085, .868, title, fontsize=34, color=INK, weight="bold")
    fig.text(.085, .815, subtitle, fontsize=16, color=MUTED)
    return fig


def main_figure(plt, data, cells):
    from matplotlib.ticker import PercentFormatter

    fig = canvas(plt, "Observed canary extraction",
                 "Qwen2.5-1.5B-Instruct  |  ordinary LoRA  |  seed 1337")
    ax = fig.add_axes([.12, .32, .61, .43])
    values = [[cells[(r, d)]["train_rate"] for d in DUPLICATIONS] for r in RANKS]
    # One explicit scale for the entire matrix; allow higher future observations.
    maximum = max(.1, max(max(row) for row in values))
    image = ax.imshow(values, cmap="Blues", vmin=0, vmax=maximum, aspect="auto")
    ax.set_xticks(range(3), [str(d) for d in DUPLICATIONS], fontsize=19)
    ax.set_yticks(range(2), [str(r) for r in RANKS], fontsize=19)
    ax.set_xlabel("Canary repetitions in training", fontsize=17, labelpad=12, color=INK)
    ax.set_ylabel("LoRA rank", fontsize=17, labelpad=12, color=INK)
    ax.tick_params(length=0, pad=9)
    for spine in ax.spines.values():
        spine.set_visible(False)
    ax.set_xticks([-.5, .5, 1.5, 2.5], minor=True)
    ax.set_yticks([-.5, .5, 1.5], minor=True)
    ax.grid(which="minor", color="white", linewidth=5)
    ax.tick_params(which="minor", bottom=False, left=False)
    for i, rank in enumerate(RANKS):
        for j, dup in enumerate(DUPLICATIONS):
            row = cells[(rank, dup)]
            color = "white" if row["train_rate"] >= maximum * .65 else INK
            ax.text(j, i - .08, f"{row['train_rate']:.1%}".replace(".0%", "%"),
                    ha="center", va="center", fontsize=29, weight="bold", color=color)
            ax.text(j, i + .19, f"{row['train_hits']} / 40 prompts", ha="center", va="center",
                    fontsize=15, color=color)
    color_ax = fig.add_axes([.12, .205, .61, .022])
    colorbar = fig.colorbar(image, cax=color_ax, orientation="horizontal", ticks=[maximum * i / 4 for i in range(5)])
    colorbar.ax.xaxis.set_major_formatter(PercentFormatter(1, decimals=1))
    colorbar.ax.tick_params(labelsize=11, length=0)
    colorbar.set_label(f"Full-canary matches / training-target prompts  (color scale: 0–{maximum:.0%})", fontsize=11)
    fig.text(.79, .72, "CONTROLS", fontsize=13, color=BLUE, weight="bold")
    fig.text(.79, .658, "Unadapted baseline", fontsize=15, color=MUTED)
    fig.text(.79, .602, f"{data['chat_baseline']['train']['matched']} / 40", fontsize=27, color=INK, weight="bold")
    heldout = {r["heldout_hits"] for r in cells.values()}
    label = str(next(iter(heldout))) if len(heldout) == 1 else f"{min(heldout)}–{max(heldout)}"
    fig.text(.79, .51, "Held-out targets", fontsize=15, color=MUTED)
    fig.text(.79, .454, f"{label} / 8", fontsize=27, color=INK, weight="bold")
    fig.text(.79, .412, "in each trained run" if len(heldout) == 1 else "range across trained runs", fontsize=11, color=MUTED)
    fig.text(.79, .335, "6 trained runs\n2 epochs per run", fontsize=15, color=INK, linespacing=1.5)
    fig.text(.085, .118, "10 training canaries × 4 related prompts per cell; 2 held-out canaries × 4 prompts.", fontsize=14, color=INK)
    fig.text(.085, .073, "Single seed; correlated prompts. These observations do not establish a stable rank or repetition trend.", fontsize=13, color=MUTED)
    fig.text(.085, .028, "Source: frozen pilot #17  |  greedy, ≤64 new tokens  |  complete target in newly generated text", fontsize=10, color=MUTED)
    return fig


def example_figure(plt, data, cells):
    from matplotlib.patches import FancyBboxPatch

    example = data["chat_positive_example"]
    row = cells[(64, 100)]
    fig = canvas(plt, "A complete canary, reproduced verbatim",
                 f"rank 64  |  100 repetitions  |  suffix probe  |  {row['train_hits']}/40 prompts matched in this run ({row['train_rate']:.1%})")
    cards = [(.61, .16, "PROMPT  ·  8 SUFFIX CHARACTERS PROVIDED", example["prompt"], "#f0f5fb"),
             (.365, .20, "ACTUAL NEW MODEL OUTPUT", example["output"], "#f0f5fb"),
             (.185, .13, "COMPLETE SYNTHETIC CANARY MATCH", example["target"], "#e5f2ec")]
    for y, height, label, text, color in cards:
        fig.add_artist(FancyBboxPatch((.085, y), .83, height, boxstyle="round,pad=0.012",
                                     transform=fig.transFigure, facecolor=color, edgecolor="none"))
        fig.text(.11, y + height - .037, label, fontsize=12, weight="bold", color=BLUE)
        fig.text(.11, y + height - .094, "\n".join(textwrap.wrap(text, width=83)), fontsize=19,
                 fontfamily="DejaVu Sans Mono", color=INK, va="top", linespacing=1.5)
    baseline = data["chat_baseline"]["train"]["matched"]
    fig.text(.085, .113, f"Unadapted baseline: {baseline}/40 training-target prompts matched. This run's held-out targets: {row['heldout_hits']}/8.", fontsize=14, color=INK)
    fig.text(.085, .067, "The prompt omits the complete target. The match is scored only in newly generated text.", fontsize=13, color=MUTED)
    fig.text(.085, .028, "Source: #17, r64-d100-train  |  synthetic canary only  |  illustrative single observed match", fontsize=10, color=MUTED)
    return fig


def render(data, cells, summary_path, csv_path, output):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    matplotlib.rcParams.update({"font.family": "DejaVu Sans", "pdf.fonttype": 42,
                                "svg.fonttype": "none", "svg.hashsalt": "track-b-pilot-19"})
    events = [{"event": "validated", "cells": 6, "evidence_mode": "real", "new_inference_runs": 0}]
    with run_directory(str(output)) as state:
        for name, figure in (("extraction-matrix", main_figure(plt, data, cells)),
                             ("positive-control", example_figure(plt, data, cells))):
            for extension in ("png", "pdf", "svg"):
                metadata = {"Software": "ASP-TLSLS #19"} if extension == "png" else (
                    {"Creator": "ASP-TLSLS #19", "CreationDate": None, "ModDate": None}
                    if extension == "pdf" else {"Creator": "ASP-TLSLS #19", "Date": None})
                path = output / f"{name}.{extension}"
                figure.savefig(path, dpi=180, metadata=metadata)
                events.append({"event": "figure_written", "file": path.name, "sha256": sha256(path)})
            plt.close(figure)
        (output / "summary.csv").write_bytes(Path(csv_path).read_bytes())
        provenance = {"issue": 19, "activity": "derived visualization; no model inference",
                      "reference_input_commit": REFERENCE_COMMIT,
                      "training_source_commit": data["source_commit"],
                      "inputs": {"summary.json": sha256(summary_path), "run-summary.csv": sha256(csv_path)},
                      "renderer_sha256": sha256(__file__), "model": data["model"]["model"],
                      "model_revision": data["model"]["revision"], "seed": data["design"]["seed"],
                      "metric": "new-output full-target containment / 40 related train-target prompts",
                      "completion_control_pooled": False,
                      "runtime": {"python": sys.version.split()[0], "matplotlib": matplotlib.__version__},
                      "cost": {"model_api": 0, "new_gpu_training": 0}}
        (output / "provenance.json").write_text(json.dumps(provenance, indent=2) + "\n", encoding="utf-8")
        (output / "render-events.jsonl.gz").write_bytes(gzip.compress(
            ("".join(json.dumps(event) + "\n" for event in events)).encode(), mtime=0))
        outputs = [p for p in output.iterdir() if p.name != "run-state.json"]
        manifest = {"issue": 19, "source_issue": 17, "completed_cells": 6,
                    "selected_example": {k: data["chat_positive_example"][k] for k in ("run", "canary_id", "prompt_type")},
                    "files": {p.name: {"bytes": p.stat().st_size, "sha256": sha256(p)} for p in sorted(outputs)}}
        (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        state["status"] = "complete"
    return provenance


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--summary", type=Path, default=ROOT / "track_b/results/pilot-17/summary.json")
    parser.add_argument("--csv", type=Path, default=ROOT / "track_b/results/pilot-17/run-summary.csv")
    parser.add_argument("--out-dir", type=Path, default=ROOT / "work/track-b-figures-19")
    parser.add_argument("--validate-only", action="store_true", help="Check evidence without plotting or creating files")
    args = parser.parse_args(argv)
    try:
        data, cells = load_evidence(args.summary, args.csv)
        if args.validate_only:
            print(json.dumps({"evidence_mode": "real", "completed_cells": 6,
                              "train_hits": [cells[(r, d)]["train_hits"] for d in DUPLICATIONS for r in RANKS]}))
            return 0
        render(data, cells, args.summary, args.csv, args.out_dir)
    except (OSError, ValueError, TypeError, KeyError, ImportError) as error:
        print(f"[error] {error}", file=sys.stderr)
        return 2
    print(f"[ok] Wrote two REAL-evidence figure sets and provenance to {args.out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
