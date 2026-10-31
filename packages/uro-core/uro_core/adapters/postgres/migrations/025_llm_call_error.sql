-- Why a failed call failed. D-66 metered failures (latency, tokens) but not their cause, so a
-- planner call that failed in 22 ms during the first live run (2026-10-31) left nothing to read.
-- Exception TYPES only, plus an HTTP status when there is one — never exception text, which can
-- carry a credential (an illegal-header error embeds the Authorization value). The full exception
-- stays in the server log. NULL = the call succeeded (or was metered before this migration).
ALTER TABLE llm_calls ADD COLUMN error TEXT;
