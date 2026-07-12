# Security Policy

Uro is a proof-of-concept engine, not a production service — but security reports are
genuinely welcome, especially around the areas below.

## Reporting a vulnerability

**Please do not open a public issue for a security problem.** Instead, use GitHub's private
vulnerability reporting:

1. Go to the repository's **Security** tab → **Report a vulnerability** (GitHub Security
   Advisories), or
2. Open a **private** draft advisory at
   [github.com/cupskeee/uro/security/advisories/new](https://github.com/cupskeee/uro/security/advisories/new).

Please include what you did, what you expected, and what happened, with enough detail to
reproduce. You'll get an acknowledgement as soon as is practical for a solo-maintained PoC.

## Scope worth probing

Uro's trust boundaries are deliberate design surfaces — reports here are most useful:

- **The extractor / gauntlet fence** — the LLM must not be able to mint mechanical, lethal, or
  protected-canon events (the schema + emitter whitelist).
- **Chronicler ingestion (`distill_outcome`)** — an external outcome bundle must not be able to
  kill, loot, or first-hand-witness a PC or a protected (T2+) actor, forge item ownership, or
  double-apply on replay.
- **The reaction layer** — a pack ships *declarative data*, never code; a rule must not be able
  to express a mechanical/lethal/canon action or escape its declared scope.
- **Server auth** — the WebSocket play channel and the outcome/management endpoints are token-gated.

## Out of scope

- Anything requiring valid credentials to abuse *your own* local deployment (it's local-first
  and content-agnostic by design).
- The experimental / by-policy items and named deferrals documented in
  [docs/16-honesty-ledger.md](docs/16-honesty-ledger.md) — these are known and disclosed.
- Denial of service from adversarial LLM prompts against *your own* API keys.
