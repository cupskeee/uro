<!-- Thanks for contributing to Uro Engine. Keep PRs a coherent slice; see CONTRIBUTING.md. -->

## What & why

<!-- What does this change, and why? Link any issue (Fixes #123). -->

## How it was verified

<!-- The acceptance/tests that prove it. Uro asserts on committed events, not prose. -->

## Checklist

- [ ] `just test` is green (ruff + ruff format + mypy + import-linter + pytest).
- [ ] New/changed behavior is covered by a test that asserts on committed events.
- [ ] Invariants respected: the hexagonal boundary holds (core ring imports only ports),
      state changes go through typed events + the projector, migrations are forward-only.
- [ ] Any doc/code drift is reconciled **in this PR** (`docs/` is authoritative); a new
      decision is appended to `docs/decisions.md` (past decisions are never edited).
- [ ] No secrets, no live-LLM calls added to CI.
