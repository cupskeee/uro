"""NPC-speech routing (D-48) + the embedder-capability guard (review).

Conversational beats voice through the `dialogue` model when it is explicitly bound; otherwise the
narrator does everything. And `build_router_from_registry` announces whether semantic memory is on.
"""

import logging

from uro_core.pipeline.engine import Engine, _Context
from uro_core.pipeline.plan import BeatPlan
from uro_core.pipeline.recall import RecallBundle
from uro_core.providers.adapters.stub import StubProvider
from uro_core.providers.registry import _warn_if_embedder_cannot_embed
from uro_core.providers.router import ProviderRouter


def _ctx(intent_class: str | None) -> _Context:
    recall = RecallBundle(recent_beats=[], actors=[], claims=[], beliefs=[])
    plan = BeatPlan(intent_class=intent_class) if intent_class is not None else None  # type: ignore[arg-type]
    return _Context(recall=recall, plan=plan)


def _engine(router: ProviderRouter) -> Engine:
    return Engine(object(), router)  # type: ignore[arg-type]  # store is unused by _narration_role


def _router(dialogue_bound: bool) -> ProviderRouter:
    bindings = {"dialogue": StubProvider()} if dialogue_bound else {}
    return ProviderRouter(bindings=bindings, default=StubProvider())


# --- has_role -----------------------------------------------------------------------------------


def test_has_role_is_explicit_binding_not_default_fallback() -> None:
    r = _router(dialogue_bound=True)
    assert r.has_role("dialogue") is True
    assert r.has_role("narrator") is False  # resolvable via default, but NOT explicitly bound


# --- _narration_role (the routing decision) -----------------------------------------------------


def test_dialogue_beat_routes_to_dialogue_when_bound() -> None:
    assert _engine(_router(True))._narration_role(_ctx("dialogue"), None) == "dialogue"


def test_dialogue_beat_uses_narrator_when_dialogue_unbound() -> None:
    # opt-in: no dialogue binding → speech routing OFF → narrator does everything (current default)
    assert _engine(_router(False))._narration_role(_ctx("dialogue"), None) == "narrator"


def test_non_dialogue_intent_uses_narrator_even_when_dialogue_bound() -> None:
    assert _engine(_router(True))._narration_role(_ctx("action"), None) == "narrator"


def test_combat_stays_on_narrator() -> None:
    # a resolved fight (encounter_events not None) is never routed to dialogue
    assert _engine(_router(True))._narration_role(_ctx("dialogue"), []) == "narrator"


def test_no_plan_uses_narrator() -> None:
    # no ruleset/planner → no intent_class to classify on → narrator
    assert _engine(_router(True))._narration_role(_ctx(None), None) == "narrator"


# --- embedder-capability guard ------------------------------------------------------------------


def test_embedder_guard_warns_for_a_chat_only_provider(caplog) -> None:  # type: ignore[no-untyped-def]
    with caplog.at_level(logging.WARNING):
        _warn_if_embedder_cannot_embed({"embedder": "codex", "narrator": "codex"})
    assert "semantic memory is OFF" in caplog.text


def test_embedder_guard_ok_for_an_embedding_provider(caplog) -> None:  # type: ignore[no-untyped-def]
    with caplog.at_level(logging.INFO):
        _warn_if_embedder_cannot_embed({"embedder": "openai"})
    assert "semantic memory enabled" in caplog.text


def test_embedder_guard_uses_the_default_when_embedder_unbound(caplog) -> None:  # type: ignore[no-untyped-def]
    # embedder not bound → resolves via the default; a codex default → semantic memory OFF
    with caplog.at_level(logging.WARNING):
        _warn_if_embedder_cannot_embed({"default": "codex"})
    assert "semantic memory is OFF" in caplog.text
