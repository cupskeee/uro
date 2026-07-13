# Task runner targets per docs/14-development-guide.md.

default: test

# Start local infrastructure (Postgres + pgvector) and wait for health.
up:
    docker compose up -d --wait

down:
    docker compose down

# Full check suite — CI-equivalent. Replay mode only; never calls live LLMs.
test:
    uv run ruff check .
    uv run ruff format --check .
    uv run mypy
    uv run lint-imports
    uv run pytest

# Auto-fix formatting and lint.
fmt:
    uv run ruff format .
    uv run ruff check --fix .

# Cut a release vX.Y.Z: gate + tag (bump the 3 pyproject versions + CHANGELOG first). See docs/14.
release version:
    uv run python -c "import sys,tomllib; v=tomllib.load(open('packages/uro-core/pyproject.toml','rb'))['project']['version']; sys.exit(0 if v=='{{version}}' else print(f'bump package versions to {{version}} first (found {v})') or 1)"
    grep -q '^## \[{{version}}\]' CHANGELOG.md || { echo "add a CHANGELOG '## [{{version}}]' section first"; exit 1; }
    just test
    git tag -a "v{{version}}" -m "Release v{{version}}"
    @echo "Tagged v{{version}}. Now: git push origin main --follow-tags  (release.yml cuts the GitHub Release)"

# --- Arriving with Phase 0 implementation (docs/10) ---
# migrate:  uv run uro db migrate
# play:     uv run uro play <campaign>       (live smoke, not CI)
# record:   re-record LLM fixtures against live providers (reviewed like snapshots)
