# ASP-TLSLS — AgentLeak: canary-based data-exfiltration evaluation for LLM agents

Synthetic benchmark and harness for measuring whether tool-using LLM agents leak a planted
secret ("canary") through outbound tool calls, and how four defence levels (D0–D4) change
that outcome. All "API keys" in the corpus are fake canaries — see `CANARY-NOTES.md`.

## Layout

- `agentleak/` — evaluation harness: task loading, mock tools, providers, runner, defences.
- `tasks/` — task corpora: `pilot/`, `expanded/`, `probe-v1/`, `lang/`, `surface-email/`,
  `surface-http/`. Each task defines a workspace, tools, and injected/ground-truth variants.
- `experiments/configs/` — per-run YAML configs (models, defences, budgets, task roots).
- `reports/` — per-run records: `summary.csv`, `per_episode.csv`, `manifest.json`,
  `provenance.json`, and run-report markdown under `reports/76-*/`.
- `track_b/` — local LoRA fine-tuning side study (scripts, configs, results).
- `scripts/` — analysis, plotting, and corpus-construction helpers.

## Quick start

1. `pip install pyyaml jsonschema` (plus `openai`/`requests` for live runs; see `REPRODUCE.md`).
2. Offline replay evaluation: `python -m agentleak.eval --config experiments/configs/<run>.yaml`
3. Live runs require provider access supplied **entirely through environment variables**
   (`AGENTLEAK_BASE_URL`, `AGENTLEAK_API_KEY`). This repository contains no credentials.

## Documentation

- `REPRODUCE.md` — how to rerun experiments and regenerate reports.
- `CORRECTIONS.md` — arm-scoped wording corrections for two published run records.
- `PROVENANCE.md` — how this snapshot was produced and what was excluded or redacted.
- `CANARY-NOTES.md` — why `sk-CANARY-*` strings appear everywhere and what they mean.

## Provenance and licence

Snapshot of an internal research repository rebuilt as a single fresh commit with no history;
personal and infrastructure details were redacted before publication (see `PROVENANCE.md`).
Contributor attribution uses anonymous codenames (`<member-A>` …) that encode no identity.

Licensed under the terms in `LICENSE` — all rights reserved.
