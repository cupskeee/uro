"""Phase 4 inc 4.3: prompt template packs (docs/09). Default templates + pack override.

The narrator template takes a `style` var (the world's tone), and a pack can override any
stage's template by filename with everything else falling through to the bundled defaults.
"""

from uro_core.pipeline.prompts import DEFAULT_ENV, PromptEnv
from uro_core.pipeline.recall import RECALL_HEADER, RecallBundle, build_narrator_messages
from uro_core.providers.adapters.stub import StubProvider
from uro_core.providers.base import PLAYER_ACTION, CompletionRequest


def test_default_env_renders_bundled_templates() -> None:
    narr = DEFAULT_ENV.render("narrator.system.j2")
    assert "narrator" in narr.lower()
    assert "tone is" not in narr  # the style clause is absent when no style is given
    assert "PLANNER" in DEFAULT_ENV.render("planner.system.j2")
    assert "DURABLE" in DEFAULT_ENV.render("extractor.system.j2")


def test_narrator_style_injection() -> None:
    narr = DEFAULT_ENV.render("narrator.system.j2", style="grim, political")
    assert "grim, political" in narr  # the pack's tone shapes the narrator


def test_pack_override_wins_and_others_fall_through() -> None:
    env = PromptEnv({"narrator.system.j2": "CUSTOM NARRATOR: {{ style }}"})
    assert env.render("narrator.system.j2", style="cozy") == "CUSTOM NARRATOR: cozy"
    # a stage the pack did NOT override still resolves to the bundled default
    assert "PLANNER" in env.render("planner.system.j2")


def test_build_narrator_messages_carries_tone() -> None:
    recall = RecallBundle(recent_beats=[], actors=[], claims=[], beliefs=[])
    msgs = build_narrator_messages(recall, "I look around", style="grim, low-magic")
    system_blob = "\n".join(m.content for m in msgs if m.role == "system")
    assert "grim, low-magic" in system_blob


def _remembering() -> RecallBundle:
    return RecallBundle(
        recent_beats=[],
        actors=[],
        claims=[],
        beliefs=[],
        memories=["You carved a broken-winged gull into the Lantern's door frame."],
    )


def test_recall_and_direction_sit_beside_the_intent() -> None:
    # D-78: the long run's narrator had the planted fact for 12/12 callbacks and used it for 8;
    # with what recall brings back (and the direction) moved next to the intent, under a header
    # that asks for the specific detail, it used it in 88% of samples against 58% (docs/16).
    msgs = build_narrator_messages(
        _remembering(), "I wonder whether anyone found my sign.", directives="Answer the sign."
    )
    last = msgs[-1]
    assert last.role == "user"
    assert last.content == (
        f"{RECALL_HEADER}\n- You carved a broken-winged gull into the Lantern's door frame."
        f"\n\nDIRECTION: Answer the sign.\n\n{PLAYER_ACTION}I wonder whether anyone found my sign."
    )
    system_blob = "\n".join(m.content for m in msgs if m.role == "system")
    assert "YOU RECALL" not in system_blob and "DIRECTION" not in system_blob


def test_with_nothing_recalled_the_last_turn_is_the_bare_intent() -> None:
    recall = RecallBundle(recent_beats=[], actors=[], claims=[], beliefs=[])
    msgs = build_narrator_messages(recall, "I look around")
    assert msgs[-1].content == "I look around"
    assert [m.role for m in msgs] == ["system", "user"]  # no empty context message either


def test_a_pack_overriding_the_narrator_template_keeps_the_recall_header() -> None:
    env = PromptEnv({"narrator.system.j2": "CUSTOM NARRATOR"})
    msgs = build_narrator_messages(_remembering(), "I look for my sign.", env=env)
    assert msgs[0].content == "CUSTOM NARRATOR"
    assert msgs[-1].content.startswith(RECALL_HEADER)  # built in code, not in the template


async def test_the_stub_answers_the_intent_not_what_recall_put_beside_it() -> None:
    msgs = build_narrator_messages(_remembering(), "I look for my sign.")
    req = CompletionRequest(messages=msgs, stage_tag="narrator")
    chunks = [c async for c in StubProvider().stream(req)]
    assert 'In answer to "I look for my sign."' in "".join(chunks)
