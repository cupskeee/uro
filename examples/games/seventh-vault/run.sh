#!/usr/bin/env bash
# THE SEVENTH VAULT — the default deterministic multiplayer arc, zero API keys.
#
#   ./run.sh            # infra up + both endings, each run TWICE and byte-compared
#   ./run.sh --stress   # additionally run the S1-S8 stress battery
#
# Everything runs against the stub provider; a real narrator is opt-in:
#   uv run python arc.py --ending clean --provider openai   (needs OPENAI_API_KEY)
set -euo pipefail
cd "$(dirname "$0")"
REPO_ROOT="$(cd ../../.. && pwd)"

echo "== infra: Postgres + migrations =="
(cd "$REPO_ROOT" && docker compose up -d --wait && uv run uro db migrate)

for ending in clean betrayal; do
  echo
  echo "== the $ending ending, twice (byte-determinism) =="
  uv run python arc.py --ending "$ending" --print-log
  cp "out/digest-$ending.txt" "out/digest-$ending-run1.txt"
  cp "out/beatlog-$ending.txt" "out/beatlog-$ending-run1.txt"
  uv run python arc.py --ending "$ending" >/dev/null
  cmp "out/digest-$ending.txt" "out/digest-$ending-run1.txt"
  cmp "out/beatlog-$ending.txt" "out/beatlog-$ending-run1.txt"
  echo "== $ending: committed-beat log + final-state digest BYTE-IDENTICAL across two runs =="
done

if [[ "${1:-}" == "--stress" ]]; then
  echo
  echo "== the S1-S8 stress battery =="
  uv run python stress/run_all.py
fi

echo
echo "done. evidence: out/ ; the report: GAP_REPORT.md"
