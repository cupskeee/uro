"""Prompt template pack (docs/09, D-6). Jinja2, with override-and-fallthrough.

The engine ships default templates (`uro_core/prompts/*.j2`, versioned). A world pack's
`prompts/` overrides any subset by filename; everything else falls through to defaults. A file
named `<template>.append.j2` (`narrator.system.append.j2`) is an ADDENDUM (D-106): rendered after
the template (the default, or the pack's own) with the same context, so a world that only adds a
premise or a style note stays on the engine's current default. This is
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

APPEND_SUFFIX = ".append.j2"


def default_templates() -> frozenset[str]:
    """The templates the engine ships (what a pack may override or add to)."""
    return frozenset(
        p.name
        for p in resources.files("uro_core.prompts").iterdir()
        if p.name.endswith(".j2") and p.is_file()
    )


def addendum_of(name: str) -> str:
    """A template's addendum filename: `narrator.system.j2` → `narrator.system.append.j2`."""
    return name.removesuffix(".j2") + APPEND_SUFFIX


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
        text = self._env.from_string(self._source(name)).render(**ctx).strip()
        addendum = self._overrides.get(addendum_of(name))
        if addendum is None:
            return text
        extra = self._env.from_string(addendum).render(**ctx).strip()
        return f"{text}\n\n{extra}" if text and extra else (text or extra)


# The engine's default env (no pack overrides). A world imported from a pack builds its own
# PromptEnv from the pack's `prompts/` and the manifest tone.
DEFAULT_ENV = PromptEnv()
