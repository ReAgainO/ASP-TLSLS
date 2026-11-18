# PROVENANCE

How this repository was produced, and what is deliberately absent.

## Snapshot

- Source: an internal research repository (kept private; it is **not** linked from this
  snapshot and shares no git history with it).
- Input state: a single working-tree snapshot at commit `2cf28e91fa2a4139f8d23e2fd3c86bf1fcb8bce2`
  (the `main` head at export time).
- Publication: one fresh commit with an empty message, authored and committed by the
  repository owner through GitHub's noreply identity. No prior commit history, branches,
  tags, issues, pull requests, or CI artifacts are included.

## Included vs excluded

Included: the evaluation harness (`agentleak/`), task corpora, experiment configs, result
records (`track_b/results`, `reports/**` summary/episode/manifest/provenance/tabular files),
analysis scripts, and the run-report markdown under `reports/76-*/`.

Excluded by policy: manuscripts, slides, PDFs, internal planning/operations documents,
process retrospectives, admin records, the entire git history, all issue/PR/CI content,
large compressed trajectory archives (`*.jsonl.gz`), and all `.env*` files. Because of that
exclusion, the `raw_sha256` digests kept in `reports/**/provenance.json` cover some files
that are intentionally not published here; they are integrity records, not broken links.

## Redactions applied to included files

Machine-generated placeholders replace, by category:

- member handles and name-derived local usernames → `<member-A>`… `<member-D>` codenames.
  These codenames exist only to distinguish which contribution came from which person in the
  internal record; they encode no identity, and no mapping is published or derivable here.
- SSH host aliases, VPN/service names, WSL distribution names, private addresses
  → `<ssh-alias-redacted>` / `<host-redacted>` / `<redacted>`.
- Provider API endpoints (base URLs, including region-bearing ones) → `<provider-base-url>`.
  Model identifiers are **not** redacted because exact reproduction requires them; set your
  own endpoint via `AGENTLEAK_BASE_URL` (see `REPRODUCE.md`).
- Course-affiliation metadata, exact calendar dates (kept to month granularity `YYYY-MM`
  for time ordering of data records), and one third-party persona e-mail address rewritten
  to the reserved `.example.com` domain.
- Per-request receipt values (`request_id`, `provider_response_id`) → `<redacted>`;
  machine-local absolute paths → `<path-redacted>`; links into the private source repository
  → `<repo-url>` / `<link-redacted>`.

`sk-CANARY-*` strings are **not** secrets and were left verbatim — see `CANARY-NOTES.md`.

## Data semantics

`reports/*/per_episode.csv` and `summary.csv` are the scored ground truth; prose reports are
narratives. Where prose and per-arm tables disagree (broad-manifest rerun), the authoritative
arm-scoped wording is in `CORRECTIONS.md`.
