-- Usage metering (docs/07, D-14, docs/01 rule 5). Stage-tagged LLM calls.
-- Operational and PRUNABLE — never world truth, never on the timeline.
-- Added as a forward-only migration (docs/14): 001 is already applied, so metering
-- arrives in its own file rather than editing the released migration.

CREATE TABLE llm_calls (
    call_id     TEXT PRIMARY KEY,
    stage_tag   TEXT NOT NULL,
    model       TEXT,
    prompt_hash TEXT NOT NULL,
    tokens_in   INT,
    tokens_out  INT,
    latency_ms  INT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX llm_calls_stage_idx ON llm_calls(stage_tag);
