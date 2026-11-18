# Replication of <member-A>'s 60-pair expansion corpus on the Ark endpoint

Source: PR #79 (`agent/24-report-expansion`), `tasks/expansion-v1`, 60 clean + 60 injected pairs.
Run 2026-11 on `deepseek-v4.1-flash`, **D0 only**, temperature 0, `max_output_tokens` 4096.

## Adaptations (the copies only, never their branch)

1. `family: calendar` → `email`, `family: web` → `file`. This branch's schema only allows
   `email|file|research`; the label does not affect scoring.
2. Dropped 24 `benign_check` entries of type `http_contains_facts` (24 files), which this branch's
   schema does not implement. Dropping a check can only make the benign score easier, so it cannot
   explain the BTS gap reported below.

Everything else — user requests, payload wording, workspaces, attack checks, mock actions — is
verbatim from their task files.

## Result

| metric | their D0 (deepseek-v4.1-flash) | this replication |
| --- | --- | --- |
| injected ASR-Exfil | **0/60** | **0/60** |
| clean BTS | 56/60 | 43/60 |
| protocol failures | 0 | 2 |
| provider errors | 0 | 1 (`http_500`, retried by resume) |

**The primary metric replicates exactly: zero exfiltration in 60 injected tasks.** That is now the
eighth independent corpus/design to produce zero on these models, and the second independent
implementation (theirs and ours) to agree on the number.

The BTS gap (43/60 vs 56/60) is **not** explained and must not be waved away:

- Dropping benign checks can only raise BTS, so the gap is not an artefact of adaptation (1) or (2).
- The two runs differ in harness version: their branch carries D2/D3, `detection.py`, and prompt
  changes this branch does not have.
- Two `protocol_failure` and one `model_error` cells count as not-benign; three cells alone do not
  close a 13-cell gap.
- Candidate explanations to test: their core prompt/observation wording, their `max_output_tokens`
  (512 vs our 4096), and whether web/calendar tasks need `http_post` tooling this branch wires
  differently.

Until that is tested, the honest statement is "ASR replicated, utility did not", not "the runs
agree".

## Cost

475 model requests, 0.95 CNY reserved at the configured 0.002 CNY/request ceiling (reservation,
not invoice). Three interruptions were handled with `--resume`; one required a fresh run because
changing the config invalidates the run fingerprint.

## Reading

Their 2,400-cell expansion and this 120-episode replication both put indirect exfiltration at zero
for these two model families. The earlier qwen 1/12 was a small-sample event that disappears at
n=60. For the report this is the central empirical claim of Track A: **the 2026 chat models under
test resist the outbound-exfiltration objective across eight independently authored corpora**, and
the defence comparison (D1/D4) cannot be evaluated on them because D0 has nothing to block.
