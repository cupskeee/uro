#!/usr/bin/env bash
# T1 thesis ablation (docs/10, docs/live-run.md): play the SAME intents through a
# FULL-engine campaign and a --bare (raw-transcript) campaign so the two transcripts
# can be compared. Run in a terminal that has OPENAI_API_KEY set; ~cents of gpt-4o-mini.
# Transcripts + state persist to Postgres for analysis (SELECT ... FROM proj_claims / events).
#
#   bash scripts/ablation.sh            # gpt-4o-mini (cheap)
#   MODEL=gpt-4o bash scripts/ablation.sh   # sharper contrast
set -u
cd "$(dirname "$0")/.." || exit 1

if [ -z "${OPENAI_API_KEY:-}" ]; then
  echo "OPENAI_API_KEY not set in this shell — export it first, then re-run." >&2
  exit 1
fi

MODEL="${MODEL:-}"                       # optional; empty = the provider default (gpt-4o-mini)
MODEL_FLAG=(); [ -n "$MODEL" ] && MODEL_FLAG=(--model "$MODEL")

TAG=$(date +%H%M%S)                       # fresh campaigns each run so old ones don't collide
A=$(uv run uro world new "Ablation FULL $TAG" | awk '/^campaign:/{print $2}')
B=$(uv run uro world new "Ablation BARE $TAG" | awk '/^campaign:/{print $2}')

# 14 intents: plant a named NPC + facts early (1-4), wander past the 8-beat recency
# window (5-12), then reference the early NPC/facts late (13-14) — that's where state
# should make FULL remember and BARE forget.
INTENTS='I settle at the bar and ask the innkeeper her name and how business has been
I ask her what she knows about the dockworker who went missing last week
She seems nervous, so I gently ask who she thinks is behind it
I ask about the Duke who rules this town and whether the people trust him
I order an ale and quietly listen to the conversations around the room
A hooded stranger steps in from the rain and I watch them for a moment
I walk over and ask the stranger what business brings them to town
I ask the stranger whether the road north is safe to travel
I join a weary soldier at a corner table for a game of dice
I ask the soldier how the fighting on the border has been going
I step outside into the cold night air and look up at the stars
I come back inside and warm my hands by the fire
I look for the innkeeper again and ask whether there is any news of the missing dockworker
I ask whether anyone here would dispute what the Duke and his men have been claiming'

run_arm () {  # $1 = campaign id, rest = extra play flags
  local cid="$1"; shift
  { printf '%s\n' "$INTENTS"; echo /quit; } | uv run uro play "$cid" --provider openai "${MODEL_FLAG[@]}" "$@"
}

echo "=================== FULL arm ($A) ==================="; run_arm "$A"
echo; echo "=================== BARE arm ($B) ==================="; run_arm "$B" --bare
echo; echo "=========================================================="
echo "DONE.  FULL=$A   BARE=$B   (tag $TAG, model ${MODEL:-default})"
echo "Compare: SELECT count(*) FROM proj_claims (via the FULL branch); read both transcripts from events."
