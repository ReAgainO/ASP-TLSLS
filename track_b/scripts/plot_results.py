#!/usr/bin/env python3
"""Summarize extraction CSVs into a rank x duplication table.

STDLIB ONLY for the text table. If matplotlib is importable it ALSO writes a PNG
heatmap (extraction rate vs rank & duplication); otherwise it just skips the plot
with a note.

Reads one or more CSVs produced by scripts/evaluate_extraction.py and aggregates
per-run extraction_rate (mean of the binary matched column).

Usage:
    python scripts/plot_results.py --csv results/*.csv --out results/summary.txt
"""
from __future__ import annotations

import argparse
import csv
import glob
import os
import sys

from evaluate_extraction import scope_digest


def read_csv_rows(path: str) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def aggregate(paths: list[str]) -> dict:
    """Return {run_id: {'matched':..,'total':..,'rate':..}} aggregated across files.

    Rows are keyed by (run_id, canary_id, prompt_type) so re-running the same cell
    and re-loading the same CSV does not double-count.
    """
    seen = {}
    runs = {}
    cohort = None
    cohort_fields = ("model", "evidence_mode", "probe_split", "manifest_sha256", "prompts_sha256")
    for path in paths:
        for row in read_csv_rows(path):
            if any(not row.get(field) for field in cohort_fields):
                raise ValueError("CSV lacks evidence/scope provenance; re-evaluate before plotting")
            if row["evidence_mode"] not in ("mock", "real", "recorded") or row["probe_split"] not in ("train", "heldout"):
                raise ValueError("Invalid evidence mode or probe split")
            identity = tuple(row[field] for field in cohort_fields)
            if cohort is not None and identity != cohort:
                raise ValueError("Mixed model/evidence/split/data cohorts; plot them separately")
            cohort = identity
            run_id = row.get("run_id")
            if not run_id:
                raise ValueError("Missing run_id")
            key = (run_id, row.get("canary_id"), row.get("prompt_type"))
            if not all(key):
                raise ValueError("Missing observation key")
            if row.get("matched") not in ("0", "1"):
                raise ValueError("Matched must be binary")
            matched = int(row["matched"])
            metadata = {field: row.get(field, "") for field in cohort_fields + ("rank", "duplication", "seed", "expected_rows", "scope_sha256")}
            if run_id in runs and runs[run_id]["meta"] != metadata:
                raise ValueError("Run id has conflicting cell provenance")
            if key in seen:
                if seen[key] != row:
                    raise ValueError("Conflicting duplicate observation")
                continue
            seen[key] = row
            agg = runs.setdefault(run_id, {"matched": 0, "total": 0, "meta": metadata})
            agg["matched"] += matched
            agg["total"] += 1
    if not runs:
        raise ValueError("No scored observations")

    for run_id, a in runs.items():
        keys = [(key[1], key[2]) for key in seen if key[0] == run_id]
        expected = a["meta"].get("expected_rows", "")
        if not expected.isdigit() or int(expected) <= 0 or a["total"] != int(expected):
            raise ValueError("Incomplete scored CSV would change the denominator")
        if scope_digest(keys) != a["meta"].get("scope_sha256"):
            raise ValueError("CSV observation keys disagree with scored scope")
        a["rate"] = a["matched"] / a["total"] if a["total"] else 0.0
    return runs


def parse_run_id(run_id: str) -> tuple:
    """Best-effort pull of (rank, duplication) from a run id or adapter name."""
    rank = dup = "?"
    parts = run_id.replace("_", " ").split()
    for tok in parts:
        low = tok.lower()
        if low.startswith("rank") and low[4:].isdigit():
            rank = low[4:]
        if low.startswith("dup") and low[3:].isdigit():
            dup = low[3:]
    return rank, dup


def build_table(runs: dict) -> tuple[list[str], list[str], dict]:
    def coordinates(run):
        return (run["meta"]["rank"] or "?", run["meta"]["duplication"] or "?")
    order = lambda value: (not value.isdigit(), int(value) if value.isdigit() else value)
    ranks = sorted({coordinates(run)[0] for run in runs.values()}, key=order)
    dups = sorted({coordinates(run)[1] for run in runs.values()}, key=order)
    grid = {}
    for run in runs.values():
        grid.setdefault(coordinates(run), []).append(run)
    return ranks, dups, grid


def render_text(ranks, dups, grid) -> str:
    cell_w = 10
    header = "rank \\ dup".ljust(cell_w) + "".join(str(d).rjust(cell_w) for d in dups)
    lines = ["Extraction rate (mean of matched) per rank x duplication cell", header,
             "-" * len(header)]
    for r in ranks:
        row = f"r={r}".ljust(cell_w)
        for d in dups:
            items = grid.get((r, d))
            if not items:
                row += "-".rjust(cell_w)
            else:
                total_m = sum(i["matched"] for i in items)
                total_n = sum(i["total"] for i in items)
                rate = total_m / total_n if total_n else 0.0
                row += f"{rate:.3f}".rjust(cell_w)
        lines.append(row)
    return "\n".join(lines)


def render_per_run(runs: dict) -> str:
    lines = ["", "run_id".ljust(28) + "matched".rjust(8) + "total".rjust(8) + "rate".rjust(9)]
    lines.append("-" * 53)
    for run_id in sorted(runs):
        a = runs[run_id]
        lines.append(f"{run_id}".ljust(28) + f"{a['matched']}".rjust(8)
                     + f"{a['total']}".rjust(8) + f"{a['rate']:.3f}".rjust(9))
    return "\n".join(lines)


def maybe_plot(ranks, dups, grid, png_path: str, title: str) -> bool:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("[note] matplotlib not installed -- skipped PNG, text table only.", file=sys.stderr)
        return False

    import numpy as np  # noqa: F401  (matplotlib pulls a numeric backend anyway)
    data = []
    for r in ranks:
        row = []
        for d in dups:
            items = grid.get((r, d))
            if items:
                tm = sum(i["matched"] for i in items)
                tn = sum(i["total"] for i in items)
                row.append(tm / tn if tn else 0.0)
            else:
                row.append(float("nan"))
        data.append(row)

    fig, ax = plt.subplots(figsize=(5, 3.5))
    im = ax.imshow(data, cmap="viridis", vmin=0, vmax=1)
    ax.set_xticks(range(len(dups)), [str(d) for d in dups])
    ax.set_yticks(range(len(ranks)), [str(r) for r in ranks])
    ax.set_xlabel("duplication")
    ax.set_ylabel("LoRA rank")
    ax.set_title(title)
    fig.colorbar(im, ax=ax, label="extraction_rate")
    for i, row in enumerate(data):
        for j, val in enumerate(row):
            if val == val:  # not NaN
                ax.text(j, i, f"{val:.2f}", ha="center", va="center", color="white")
    fig.tight_layout()
    os.makedirs(os.path.dirname(png_path) or ".", exist_ok=True)
    fig.savefig(png_path, dpi=150)
    print(f"[ok] wrote plot -> {png_path}")
    return True


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Summarize extraction CSVs (stdlib; optional PNG).")
    ap.add_argument("--csv", nargs="*", default=None, help="CSV file(s) or globs")
    ap.add_argument("--out", default="results/summary.txt", help="text table output")
    ap.add_argument("--png", default="results/extraction_heatmap.png", help="optional plot path")
    ap.add_argument("--no-plot", action="store_true")
    args = ap.parse_args(argv)

    patterns = args.csv or [os.path.join("results", "*.csv")]
    paths: list[str] = []
    for pat in patterns:
        paths.extend(sorted(glob.glob(pat)))
    paths = [p for p in paths if os.path.isfile(p)]
    if not paths:
        print(f"[error] no CSVs matched {patterns} -- run evaluate_extraction first", file=sys.stderr)
        return 2

    try:
        runs = aggregate(paths)
        ranks, dups, grid = build_table(runs)
    except ValueError as error:
        print(f"[error] {error}", file=sys.stderr)
        return 2
    meta = next(iter(runs.values()))["meta"]
    title = f"{meta['evidence_mode'].upper()} / {meta['probe_split']} canary containment"
    text = title + "\nModel: " + meta["model"] + "\n" + render_text(ranks, dups, grid) + "\n" + render_per_run(runs) + "\n"

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write(text)
    print(text)
    print(f"[ok] wrote summary -> {args.out}")

    if not args.no_plot:
        maybe_plot(ranks, dups, grid, args.png, title)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
