#!/usr/bin/env bash
# Barrier-free taint deep-dive driver: each lane loops on its own, so a lane that fails
# fast (429 or timeout) no longer waits for the slowest lane to end a shared round.
# The previous round-barrier design cost qwen3-8b ~20 minutes at 1/200 after a single 429.
# A 20 s pause between a lane's own attempts keeps pressure off the free-tier queue.
set -u
cd "$(dirname "$0")"
ENVF="<path-redacted>"
PY=".venv/Scripts/python.exe"
TARGET=200

lane_loop() {
  local m="$1" run="results/76/taint-$1" cfg="experiments/configs/76-sf-taint-$1.yaml"
  while true; do
    local n
    n=$(wc -l < "$run/episodes.jsonl" 2>/dev/null || echo 0)
    [ "$n" -ge "$TARGET" ] && { echo "[$m] complete $n/$TARGET"; return 0; }
    if [ "$n" -eq 0 ]; then
      "$PY" -m agentleak.runner --config "$cfg" --env-file "$ENVF" --output "$run" >> "results/76/taint-$m.log" 2>&1
    else
      "$PY" -m agentleak.runner --config "$cfg" --env-file "$ENVF" --output "$run" --resume >> "results/76/taint-$m.log" 2>&1
    fi
    sleep 20
  done
}

for m in qwen3-8b qwen25-7b-instruct qwen35-4b xing4-29b glm4-9b-0414; do lane_loop "$m" & done
wait
