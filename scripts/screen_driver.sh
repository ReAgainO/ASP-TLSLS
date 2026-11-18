#!/usr/bin/env bash
# Drive the six-model #76 screen in parallel rounds.
# Each round launches one runner per model, waits, and repeats until every run
# directory holds its 100 episodes or the caps are hit. The runner is resumable and
# refuses to regenerate an interrupted subject, so re-invoking this script is safe.
# GLM-Z1-9B-0414 is EXCLUDED: enable_thinking=false is accepted but not honoured there
# (probe 2026-11 returned reasoning_tokens=74 of 79), so it is not parameter-comparable.
set -u
cd "$(dirname "$0")"
ENVF="<path-redacted>"
PY=".venv/Scripts/python.exe"
MODELS="qwen3-8b qwen25-7b-instruct qwen35-4b xing4-29b glm4-9b-0414"
TARGET=100

recorded() { local run="results/76/screen-$1"; [ -f "$run/episodes.jsonl" ] && wc -l < "$run/episodes.jsonl" || echo 0; }

for round in $(seq 1 80); do
  echo "=== round $round $(date +%H:%M:%S) ==="
  pending=0
  for m in $MODELS; do
    n=$(recorded "$m")
    if [ "$n" -ge "$TARGET" ]; then echo "  $m done ($n/$TARGET)"; continue; fi
    pending=$((pending + 1))
    cfg="experiments/configs/76-sf-screen-$m.yaml"
    run="results/76/screen-$m"
    if [ "$n" -eq 0 ]; then
      "$PY" -m agentleak.runner --config "$cfg" --env-file "$ENVF" --output "$run" >> "results/76/driver-$m.log" 2>&1 &
    else
      "$PY" -m agentleak.runner --config "$cfg" --env-file "$ENVF" --output "$run" --resume >> "results/76/driver-$m.log" 2>&1 &
    fi
  done
  [ "$pending" -eq 0 ] && { echo "ALL COMPLETE"; break; }
  wait
  for m in $MODELS; do echo "  $m -> $(recorded "$m")/$TARGET"; done
done
