#!/usr/bin/env bash
# agent_checks.sh — local entry point for everything this repo used to run on
# GitHub Actions. Actions is retired here (private repo, per-minute billing,
# $0 spend limit => quota exhaustion fails silently). The subcommands below are
# local equivalents of the retired workflows, using the existing gh login.
#
#   ./scripts/agent_checks.sh doctor     # check gh auth + python toolchain
#   ./scripts/agent_checks.sh test       # was: Smoke tests (smoke.yml)
#   ./scripts/agent_checks.sh track-b    # was: Track B offline checks
#   ./scripts/agent_checks.sh policy     # was: PR policy (pr-policy.yml)
#   ./scripts/agent_checks.sh guard <before> <after>  # explicit main commit range
#   ./scripts/agent_checks.sh issues     # was: Create task issues
#   ./scripts/agent_checks.sh figures    # was: Pilot figures and replay
#   ./scripts/agent_checks.sh backup     # was: Backup repository (never ran)
#
# Each subcommand prints the exact command line it is about to run, so it can be
# pasted into a shell or an agent task without reading this file.
set -euo pipefail
cd "$(dirname "$0")/.."

need_gh() {
  command -v gh >/dev/null || { echo "gh CLI not found" >&2; exit 2; }
  gh auth token >/dev/null 2>&1 || { echo "gh is not authenticated" >&2; exit 2; }
}

run() { echo "+ $*"; "$@"; }
set_repository() { export GITHUB_REPOSITORY="${GH_REPO:-${GITHUB_REPOSITORY:-<repo-slug>}}"; }

case "${1:-help}" in
  doctor)
    need_gh
    gh auth status
    python -V
    python -c "import agentleak, sys; print('agentleak import OK', agentleak.__file__)" 2>/dev/null \
      || echo "note: package not installed yet — run: python -m pip install -e '.[dev]'"
    ;;
  test)
    run python -m pip install -e '.[dev]'
    run python -m pytest
    run python -m agentleak.eval --dry-run
    ;;
  track-b)
    run python -m unittest discover -s track_b/tests -v
    mkdir -p work
    run_dir=$(mktemp -d work/track-b-XXXXXXXX)
    run python track_b/scripts/generate_canary_dataset.py --docs 30 --canaries 12 --out-dir "$run_dir/data"
    run python track_b/scripts/evaluate_extraction.py --mock --manifest "$run_dir/data/manifest.json" \
        --eval-prompts "$run_dir/data/eval_prompts.jsonl" --duplication 100 --rank 8 --leak-rate 1 \
        --out "$run_dir/results/mock.csv"
    run python track_b/scripts/plot_results.py --csv "$run_dir/results/mock.csv" \
        --out "$run_dir/results/summary.txt" --no-plot
    echo "Track B synthetic-control artifacts: $run_dir"
    ;;
  policy)
    pr="${2:-${PR_NUMBER:-}}"
    [[ "$pr" =~ ^[1-9][0-9]*$ ]] || { echo "usage: $0 policy <positive PR_NUMBER>" >&2; exit 2; }
    need_gh
    set_repository
    export PR_NUMBER="$pr"
    run python scripts/workflow_policy.py pr
    ;;
  guard)
    before="${2:-${BEFORE:-}}"
    after="${3:-${COMMIT:-}}"
    if [ -z "$before" ] || [ -z "$after" ]; then
      echo "usage: $0 guard <before-main-commit> <after-main-commit> (fetch the range first)" >&2
      exit 2
    fi
    set_repository
    resolved_before=$(git rev-parse --verify --end-of-options "${before}^{commit}")
    resolved_after=$(git rev-parse --verify --end-of-options "${after}^{commit}")
    export BEFORE="$resolved_before" COMMIT="$resolved_after"
    need_gh
    export ACTOR="${ACTOR:-local-check}"
    export RUN_URL="${RUN_URL:-local explicit-range check}"
    run python scripts/workflow_policy.py main
    ;;
  issues)
    need_gh
    export GITHUB_REPOSITORY="${GH_REPO:-<repo-slug>}"
    export GITHUB_SHA="${GITHUB_SHA:-$(git rev-parse HEAD)}"
    run python scripts/create_issues.py
    ;;
  figures)
    need_gh
    comment="${2:-6571920749}"
    repo="${GH_REPO:-<repo-slug>}"
    mkdir -p work/figures
    run gh api "repos/$repo/issues/comments/$comment" --jq .body
    gh api "repos/$repo/issues/comments/$comment" --jq .body > work/attachment.txt
    run python scripts/decode_simulation_attachment.py work/attachment.txt work/observed.json
    run python scripts/audit_simulation.py work/observed.json --output work/audit
    run python scripts/plot_pilot.py work/audit/summary.csv --output work/figures
    run python -m agentleak.demo
    echo "figures land in work/figures (was: an Actions artifact)"
    ;;
  backup)
    stamp=$(date -u +%Y%m%d-%H%M%S)
    bundle="ai-security-project-$stamp.bundle"
    run git bundle create "$bundle" --all
    run git bundle verify "$bundle"
    echo "bundle: $bundle — copy it off this machine (was: a 90-day Actions artifact)"
    ;;
  *)
    sed -n '2,20p' "$0"
    ;;
esac
