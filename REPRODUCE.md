# REPRODUCE

How to rerun the experiments recorded under `reports/`.

## Requirements

- Python 3.10+ , `pyyaml`, `jsonschema`; live runs also use `requests`.
- No credentials are stored in this repository. Live runs read the provider endpoint and key
  from the environment: `AGENTLEAK_BASE_URL` (e.g. your provider's OpenAI-compatible base
  URL) and `AGENTLEAK_API_KEY`. Optional: `AGENTLEAK_DB`, `AGENTLEAK_SERVER_TOKEN` for the
  mock server. Some configs accept `--env-file <path>`; create such a file locally and keep
  it out of git (`.env*` files are intentionally not part of this snapshot).
- Provider base URLs in configs and reports were replaced by `<provider-base-url>`; the model
  identifiers (e.g. `Qwen/Qwen3-8B`, `DeepSeek/...`-style ids) are unchanged and select the
  same models on any endpoint that serves them.

## Offline (replay/analysis)

```
python -m agentleak.eval   --config experiments/configs/<run>.yaml   # rescore recorded episodes
python -m agentleak.summarize --output reports/<run>/summary.csv      # rebuild summaries
python -m agentleak.compare  --a reports/<runA> --b reports/<runB>
```

`agentleak/replay.py` re-executes recorded trajectories against the mock tools in
`agentleak/core.py`; results depend only on the JSON/CSV records committed here.
Large per-episode trajectory archives (`*.jsonl.gz`) were deliberately **not** published;
sha256 digests in `provenance.json` still cover those excluded artifacts, so a holder of the
archives can verify integrity against this snapshot.

## Live runs

```
python -m agentleak.runner --config experiments/configs/76-sf-tierA-seed0.yaml --dry-run
python -m agentleak.runner --config experiments/configs/76-sf-tierA-seed0.yaml --output runs/out.json
```

A run writes `manifest.json` + `per_episode.csv` + `summary.csv` equivalents under the output
path. Budget guards (`request_cost_ceiling`, `max_requests`) are enforced by
`agentleak/budget.py` before any request is issued.

## Task corpora

Tasks are self-contained YAML under `tasks/` (see `README.md` for the directory map).
`tasks/*/corpus-index.json` records pair enumeration order used by the configs.
Synthetic canary strings (`sk-CANARY-…`) appear as planted secrets; `CANARY-NOTES.md`
explains their role and scoring semantics.

## Track B (local fine-tuning)

`track_b/scripts/generate_canary_dataset.py` builds the synthetic corpus;
`train_lora.py` runs local training; results are JSON under `track_b/results/`.
Host-specific paths recorded in earlier drafts were redacted (`<path-redacted>`).

## Known corrections

Two run records were re-worded after re-reading the broad-manifest rerun; the authoritative
arm-scoped numbers are in `CORRECTIONS.md`.
