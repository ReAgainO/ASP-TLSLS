#!/usr/bin/env python3
"""Create GitHub issues from issues/issues.md.

The issue file uses simple Markdown comment blocks:

    <!-- issue
    title: Example title
    labels: A, P0, pilot
    -->
    Issue body...

This script is idempotent by title: existing open/closed issues with the same
title are skipped. It also writes docs/issue-run-report.md and commits it as a
visible receipt, because the repository may be private and not queryable
without authentication.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

ISSUES_FILE = Path("issues/issues.md")
REPORT_FILE = Path("docs/issue-run-report.md")

KNOWN_LABELS = {
    "A": ("6f42c1", "Track A: ExfilBench / IntentCap"),
    "B": ("0e8a16", "Track B: LoRA canary extraction"),
    "Cross": ("d4c5f9", "Cross-track presentation and coordination"),
    "Report": ("1d76db", "Report and writing"),
    "Backlog": ("ededed", "Future or optional backlog"),
    "P0": ("b60200", "Must do first"),
    "P1": ("fbca04", "Should do soon"),
    "P2": ("c2e0c6", "Nice to have"),
    "pilot": ("0075ca", "Needed for 11/05 presentation"),
    "report": ("5319e7", "Needed for 11/20 report"),
    "decision": ("e11d48", "Needs a concrete decision"),
    "infra": ("006b75", "Infrastructure / tooling"),
    "experiment": ("b4a8d3", "Experiment execution"),
    "writing": ("f9d0c4", "Writing / slides / report"),
}

ISSUE_BLOCK_RE = re.compile(
    r"<!--\s*issue\s*\n(.*?)\n-->\s*\n(.*?)(?=\n<!--\s*issue\s*\n|\Z)",
    re.S,
)


def run(cmd: list[str], *, check: bool = True, capture: bool = False) -> subprocess.CompletedProcess[str]:
    print("+ " + " ".join(cmd), flush=True)
    return subprocess.run(
        cmd,
        check=check,
        text=True,
        capture_output=capture,
    )


def parse_issues(text: str) -> list[dict[str, object]]:
    issues: list[dict[str, object]] = []
    for meta_text, body in ISSUE_BLOCK_RE.findall(text):
        meta: dict[str, str] = {}
        for line in meta_text.strip().splitlines():
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            meta[key.strip().lower()] = value.strip()
        title = meta.get("title", "").strip()
        if not title:
            continue
        labels = [x.strip() for x in meta.get("labels", "").split(",") if x.strip()]
        issues.append(
            {
                "title": title,
                "labels": labels,
                "body": body.strip() + "\n",
            }
        )
    return issues


def ensure_labels(labels: list[str]) -> None:
    result = run(["gh", "label", "list", "--json", "name"], check=False, capture=True)
    existing: set[str] = set()
    if result.returncode == 0 and result.stdout.strip():
        try:
            existing = {item["name"] for item in json.loads(result.stdout)}
        except json.JSONDecodeError:
            existing = set()

    for label in sorted(set(labels)):
        if label in existing:
            continue
        color, description = KNOWN_LABELS.get(label, ("ededed", label))
        run(
            [
                "gh",
                "label",
                "create",
                label,
                "--color",
                color,
                "--description",
                description,
                "--force",
            ],
            check=False,
        )


def existing_titles() -> set[str]:
    result = run(["gh", "issue", "list", "--state", "all", "--limit", "1000", "--json", "title"], capture=True, check=False)
    if result.returncode != 0 or not result.stdout.strip():
        return set()
    try:
        return {item["title"] for item in json.loads(result.stdout)}
    except json.JSONDecodeError:
        return set()


def create_issue(issue: dict[str, object]) -> None:
    cmd = ["gh", "issue", "create", "--title", str(issue["title"]), "--body", str(issue["body"])]
    for label in issue.get("labels", []):  # type: ignore[union-attr]
        cmd.extend(["--label", str(label)])
    run(cmd)


def write_report(created: list[str], skipped: list[str], failed: list[str]) -> None:
    REPORT_FILE.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Issue creation report",
        "",
        f"- Commit: `{os.environ.get('GITHUB_SHA', 'unknown')}`",
        f"- Run: `{os.environ.get('GITHUB_RUN_ID', 'unknown')}` attempt `{os.environ.get('GITHUB_RUN_ATTEMPT', 'unknown')}`",
        f"- Created: {len(created)}",
        f"- Skipped: {len(skipped)}",
        f"- Failed: {len(failed)}",
        "",
    ]
    if created:
        lines += ["## Created", ""] + [f"- {title}" for title in created] + [""]
    if skipped:
        lines += ["## Skipped", ""] + [f"- {title}" for title in skipped] + [""]
    if failed:
        lines += ["## Failed", ""] + [f"- {title}" for title in failed] + [""]
    REPORT_FILE.write_text("\n".join(lines), encoding="utf-8")


def commit_report() -> None:
    status = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all", "--", str(REPORT_FILE)],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    if not status:
        print("No report changes to commit.", flush=True)
        return
    run(["git", "config", "user.name", "github-actions[bot]"], check=False)
    run(["git", "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com"], check=False)
    run(["git", "add", str(REPORT_FILE)])
    run(["git", "commit", "-m", "docs: issue creation report [skip ci]"])
    push = subprocess.run(["git", "push", "origin", "HEAD:main"], check=False)
    if push.returncode != 0:
        print("Warning: failed to push issue-run report.", file=sys.stderr, flush=True)


def main() -> int:
    if not ISSUES_FILE.exists():
        print(f"Missing {ISSUES_FILE}", file=sys.stderr)
        return 1

    issues = parse_issues(ISSUES_FILE.read_text(encoding="utf-8"))
    if not issues:
        print("No issues parsed.", file=sys.stderr)
        return 1

    all_labels: list[str] = []
    for issue in issues:
        all_labels.extend(issue["labels"])  # type: ignore[arg-type]
    ensure_labels(all_labels)

    known = existing_titles()
    created: list[str] = []
    skipped: list[str] = []
    failed: list[str] = []

    for issue in issues:
        title = str(issue["title"])
        if title in known:
            skipped.append(title)
            print(f"skip existing: {title}", flush=True)
            continue
        try:
            create_issue(issue)
            created.append(title)
        except subprocess.CalledProcessError as exc:
            failed.append(title)
            print(f"failed: {title}\n{exc.stderr}", file=sys.stderr, flush=True)

    write_report(created, skipped, failed)
    commit_report()

    if failed:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
