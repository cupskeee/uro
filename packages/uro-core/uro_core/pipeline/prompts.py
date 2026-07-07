"""Prompt template pack (docs/09, D-6). Jinja2, with override-and-fallthrough.

The engine ships default templates (`uro_core/prompts/*.j2`, versioned). A world pack's
`prompts/` overrides any subset by filename; everything else falls through to defaults. This is
the *only* creator scripting surface for now — authors customize voice through prompts + seeds,
not code. Each stage injects a documented context contract (the `**ctx` it renders with).
"""

from __future__ import annotations

from importlib import resources

from jinja2 import Environment, StrictUndefined

# Template-API version (docs/09). RESERVED, not yet enforced: the intent is that a pack pins
# against this so an engine upgrade fails loudly on a contract change, but the manifest carries
# no version field yet and nothing checks it. Treat as a placeholder until wired.
TEMPLATE_API_VERSION = 1


class PromptEnv:
    """Renders a named template — a pack override if present, else the bundled default."""

    def __init__(self, overrides: dict[str, str] | None = None) -> None:
        self._overrides = overrides or {}
        self._defaults: dict[str, str] = {}
        # StrictUndefined: a template (esp. a pack override) referencing an uninjected/misnamed
        # variable raises loudly instead of silently rendering empty (docs/13 fail-loudly intent).
        self._env = Environment(
            autoescape=False, keep_trailing_newline=False, undefined=StrictUndefined
        )

    def _source(self, name: str) -> str:
        if name in self._overrides:
            return self._overrides[name]
        if name not in self._defaults:
            self._defaults[name] = (
                resources.files("uro_core.prompts").joinpath(name).read_text(encoding="utf-8")
            )
        return self._defaults[name]

    def render(self, name: str, **ctx: object) -> str:
        return self._env.from_string(self._source(name)).render(**ctx).strip()


# The engine's default env (no pack overrides). A world imported from a pack builds its own
# PromptEnv from the pack's `prompts/` and the manifest tone.
DEFAULT_ENV = PromptEnv()
