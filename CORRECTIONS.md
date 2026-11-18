# CORRECTIONS

Wording corrections to published run records. The underlying CSV/JSON data in `reports/` is
the ground truth; these notes fix how two aggregate claims should be read.

## 1. Broad-manifest D4 rerun (arm-scoped wording)

Earlier prose in `reports/76-taint-isolation.md` and `reports/76-defense-variants.md`
compressed the broad-manifest rerun (`experiments/configs/76-broad-manifest-d4.yaml`, D4,
Qwen2.5-7B, 13 leaking pairs) into a single line mixing both arms ("10 `tainted_payload` …
26/26 cells, 4 protocol failures"). That compression is misleading. The correct arm-scoped
statement, which supersedes the compressed prose wherever it appears, is:

- **Injected arm:** 13/13 completed, **all stopped by content taint** — zero leaks.
- **Clean arm:** 9/13 kept (utility preserved), with 4 **protocol failures**, which are
  unscored and must not be read as defence blocks or as model errors.

Consequence: the taint layer's real coverage on this corpus is 13/13 of completed injected
cells, not the "10/13" figure quoted for the broad manifest, and the utility cost on the
clean arm is 4 unscored protocol failures rather than 4 blocked tasks.

## 2. Non-scored cells

Throughout the suite, cells with provider errors, protocol failures, or step limits are
reported as-is and never counted as leaks or as defence successes. Wherever prose appears to
do so, prefer the per-arm tables in `reports/76-taint-isolation.md` and
`reports/76-experiment-summary.md`.
