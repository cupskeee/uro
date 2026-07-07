"""Phase 4 inc 4.3: prompt template packs (docs/09). Default templates + pack override.

The narrator template takes a `style` var (the world's tone), and a pack can override any
stage's template by filename with everything else falling through to the bundled defaults.
"""

from uro_core.pipeline.prompts import DEFAULT_ENV, PromptEnv
from uro_core.pipeline.recall import RecallBundle, build_narrator_messages


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
