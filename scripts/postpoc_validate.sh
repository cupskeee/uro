#!/usr/bin/env bash
# Post-PoC LIVE validation (docs/live-run.md) — exercise the LLM-in-the-loop legs of the post-PoC
# phases with a REAL model, so the behavior deterministic tests can't see gets looked at:
#
#   Leg A (Phase 6, alien ruleset): play an Emberfell (uro_pbta) campaign — the LIVE planner picks
#     PbtA MOVES, an aggressive intent triggers a 2d6 CONFLICT, the narrator weaves 7-9 outcomes.
#   Leg B (Phase 8, Chronicler): an external toy battle's feat becomes a witness rumor; a LIVE
#     narrator is then asked to RETELL it — does the low-confidence, third-hand belief surface as a
#     HEDGED rumor (not settled fact)?
#
# Run in a terminal with OPENAI_API_KEY set; ~cents of gpt-4o-mini. State persists to Postgres for
# analysis (the owner runs this; Claude analyzes from proj_* / events). The multiplayer leg
# (Phase 7) is a MANUAL step — see docs/live-run.md ("uro serve" + two "uro connect").
#
#   bash scripts/postpoc_validate.sh              # gpt-4o-mini
#   MODEL=gpt-4o bash scripts/postpoc_validate.sh # sharper
set -u
cd "$(dirname "$0")/.." || exit 1

if [ -z "${OPENAI_API_KEY:-}" ]; then
  echo "OPENAI_API_KEY not set in this shell — export it first, then re-run." >&2
  exit 1
fi
MODEL="${MODEL:-}"
MODEL_FLAG=(); [ -n "$MODEL" ] && MODEL_FLAG=(--model "$MODEL")
play () { uv run uro play "$1" --provider openai ${MODEL_FLAG[@]+"${MODEL_FLAG[@]}"}; }

# ---------- Leg A: Phase 6 — a PbtA (uro_pbta) campaign, played live ----------
echo "=================== Leg A: PbtA / uro_pbta ($(date +%H%M%S)) ==================="
WORLD=$(uv run uro world create worlds/emberfell | awk '/^world:/{print $2}')
echo "emberfell world: $WORLD"
CID_A=$(uv run uro campaign new "$WORLD" --pc Ash | awk '/^campaign:/{print $2}')
echo "campaign: $CID_A  (bound to uro-pbta)"
PBTA_INTENTS='I step into the Deep Vein and read the room for trouble
I try to talk Cass the claim-boss into standing down
I keep my nerve as the Company toughs fan out around me
I go aggro and seize the Deep Vein from Cass by force
I catch my breath and take stock of my wounds
I look around to see who is still standing with me
I press my advantage and demand the diggers be let go
I ask River what the Company will do now'
{ printf '%s\n' "$PBTA_INTENTS"; echo /quit; } | play "$CID_A"

# ---------- Leg B: Phase 8 — a Chronicler war-story rumor, retold live ----------
echo; echo "=================== Leg B: Chronicler war-story retell ==================="
SEED=$(uv run python scripts/warstory_live.py | awk '/^CAMPAIGN/{print $2}')
echo "war-story campaign: $SEED  (a raider witnessed Sable's feat; Mera heard it third-hand)"
WARSTORY_INTENTS='I settle at Mera the tavern keeper'\''s bar and ask what news there is
I ask Mera what she has heard of late about any wizard on the road
I ask her whether she truly believes what she just told me'
{ printf '%s\n' "$WARSTORY_INTENTS"; echo /quit; } | play "$SEED"

echo; echo "=========================================================="
echo "DONE.  PbtA=$CID_A   WarStory=$SEED   (model ${MODEL:-default})"
echo "Analyze (Claude, from Postgres — docs/live-run.md):"
echo "  - PbtA: campaigns.ruleset_id='uro-pbta'; the PC sheet has stats/harm/conditions, NO hp/ac;"
echo "    did a conflict commit (EncounterStarted + EncounterTurnTaken.result in miss/partial/full)?"
echo "  - Chronicler: the feat is truth=unknown/origin=external; Mera holds a low-confidence belief;"
echo "    read the Leg-B transcript from events — is the rumor RETOLD as hedged, not settled fact?"
