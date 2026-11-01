"""The headless-CLI providers (D-72): `claude -p` and `codex exec`, driven against a FAKE executable
that records what it was given and replays recorded output — no real CLI runs in CI.

The lockdown flags are asserted, not just present: both CLIs are agents with tools, player text
reaches their prompts, and a live check had a read-only-sandboxed `codex exec` read a file outside
its directory until its shell tool was disabled.
"""

import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest
from uro_core.errors import ProviderError
from uro_core.metering import LLMCall
from uro_core.ports.model_registry import ModelConnection
from uro_core.providers.adapters.cli import (
    CODEX_DISABLED_FEATURES,
    ClaudeCliProvider,
    CodexCliProvider,
)
from uro_core.providers.base import Message
from uro_core.providers.registry import (
    check_reasoning_effort,
    classify_modality,
    discover_models,
    provider_from_connection,
)
from uro_core.providers.router import ProviderRouter

_FAKE = """#!{python}
import json, os, sys, time
args = sys.argv[1:]
stdin = sys.stdin.read()
system = None
if "--system-prompt-file" in args:
    system = open(args[args.index("--system-prompt-file") + 1]).read()
for a in args:
    if a.startswith("model_instructions_file="):
        system = open(json.loads(a.split("=", 1)[1])).read()
json.dump({{"argv": args, "stdin": stdin, "system": system, "cwd": os.getcwd()}},
          open(os.environ["FAKE_CLI_LOG"], "w"))
mode = os.environ.get("FAKE_CLI_MODE", "ok")
if mode == "slow":
    time.sleep(5)
if mode == "fail":
    sys.stderr.write("Error: not logged in (token abcdefghijklmnopqrstuvwxyz0123456789)\\n")
    sys.exit(2)
sys.stdout.write(open(os.environ["FAKE_CLI_OUT"]).read())
"""

MSGS = [
    Message(role="system", content="You narrate Ashfall."),
    Message(role="user", content="I wave at Mera."),
]


@pytest.fixture
def fake(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """A fake CLI. Call it with the lines it should print; it returns what the CLI was given."""
    exe = tmp_path / "fake-cli"
    exe.write_text(_FAKE.format(python=sys.executable))
    exe.chmod(0o755)
    log, out = tmp_path / "log.json", tmp_path / "out.txt"
    monkeypatch.setenv("FAKE_CLI_LOG", str(log))
    monkeypatch.setenv("FAKE_CLI_OUT", str(out))

    class Fake:
        path = str(exe)

        def emits(self, *lines: dict[str, Any] | str) -> None:
            out.write_text(
                "".join((ln if isinstance(ln, str) else json.dumps(ln)) + "\n" for ln in lines)
            )

        def seen(self) -> dict[str, Any]:
            return json.loads(log.read_text())

    return Fake()


def _recording(provider: Any) -> tuple[ProviderRouter, list[LLMCall]]:
    calls: list[LLMCall] = []

    async def record(call: LLMCall) -> None:
        calls.append(call)

    return ProviderRouter(bindings={}, default=provider, recorder=record), calls


def _flag(argv: list[str], name: str) -> str:
    return argv[argv.index(name) + 1]


async def test_claude_streams_through_a_locked_down_print_mode(fake: Any) -> None:
    fake.emits(
        {"type": "system", "subtype": "init"},
        {
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": "Mera "},
            },
        },
        {
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": "waves back."},
            },
        },
        {
            "type": "result",
            "result": "Mera waves back.",
            "is_error": False,
            "usage": {
                "input_tokens": 3,
                "cache_creation_input_tokens": 600,
                "cache_read_input_tokens": 100,
                "output_tokens": 7,
            },
        },
    )
    claude = ClaudeCliProvider(model="sonnet", command=fake.path, reasoning_effort="low")
    router, calls = _recording(claude)
    assert "".join([c async for c in router.stream("narrator", MSGS)]) == "Mera waves back."
    seen = fake.seen()
    argv = seen["argv"]
    assert argv[0] == "-p" and _flag(argv, "--tools") == ""  # no tools at all
    assert "--strict-mcp-config" in argv and _flag(argv, "--setting-sources") == ""
    assert "--no-session-persistence" in argv and "--disable-slash-commands" in argv
    assert _flag(argv, "--output-format") == "stream-json" and "--include-partial-messages" in argv
    assert _flag(argv, "--model") == "sonnet" and _flag(argv, "--effort") == "low"
    assert seen["system"] == "You narrate Ashfall."  # Uro's prompt, not Claude Code's
    assert seen["stdin"] == "I wave at Mera." and "I wave at Mera." not in argv  # never in `ps`
    assert os.listdir(seen["cwd"]) == []  # an empty directory, the system file cleaned up
    assert (calls[0].tokens_in, calls[0].tokens_out) == (703, 7)


async def test_claude_complete_and_its_errors(fake: Any) -> None:
    claude = ClaudeCliProvider(model="default", command=fake.path)
    fake.emits({"type": "result", "result": '{"claims": []}', "is_error": False, "usage": {}})
    assert await claude.complete(_req()) == '{"claims": []}'
    assert "--model" not in fake.seen()["argv"]  # `default` = the CLI's own default model
    fake.emits({"type": "result", "result": "Credit balance too low", "is_error": True})
    with pytest.raises(ProviderError, match="Credit balance"):
        await claude.complete(_req())


async def test_codex_exec_runs_with_every_tool_off(fake: Any) -> None:
    fake.emits(
        {"type": "thread.started"},
        {"type": "item.completed", "item": {"type": "reasoning", "text": "…"}},
        {"type": "item.completed", "item": {"type": "agent_message", "text": '{"claims": []}'}},
        {
            "type": "turn.completed",
            "usage": {"input_tokens": 7046, "output_tokens": 5, "reasoning_output_tokens": 12},
        },
    )
    codex = CodexCliProvider(model="gpt-5.6-luna", command=fake.path, reasoning_effort="low")
    router, calls = _recording(codex)
    assert await router.complete("extractor", MSGS) == '{"claims": []}'
    seen = fake.seen()
    argv = seen["argv"]
    assert argv[0] == "exec" and argv[-1] == "-"  # the prompt comes on stdin
    assert _flag(argv, "--sandbox") == "read-only" and "--ephemeral" in argv
    assert "--ignore-user-config" in argv and os.path.realpath(
        _flag(argv, "-C")
    ) == os.path.realpath(seen["cwd"])
    disabled = {argv[i + 1] for i, a in enumerate(argv) if a == "--disable"}
    assert "shell_tool" in disabled and disabled == set(CODEX_DISABLED_FEATURES)
    assert 'web_search="disabled"' in argv and 'model_reasoning_effort="low"' in argv
    assert _flag(argv, "-m") == "gpt-5.6-luna"
    assert seen["system"] == "You narrate Ashfall." and seen["stdin"] == "I wave at Mera."
    assert (calls[0].tokens_in, calls[0].tokens_out) == (7046, 17)
    assert [c async for c in codex.stream(_req())] == ['{"claims": []}']  # whole, not streamed


async def test_a_codex_tool_call_is_refused(fake: Any) -> None:
    # Defense in depth: every tool is off, so a tool event means something slipped — never trust it.
    fake.emits(
        {
            "type": "item.completed",
            "item": {"type": "command_execution", "command": "cat ~/.ssh/id"},
        },
        {"type": "item.completed", "item": {"type": "agent_message", "text": "secret"}},
    )
    with pytest.raises(ProviderError, match="used a tool"):
        await CodexCliProvider(model="default", command=fake.path).complete(_req())


async def test_failures_are_clean_and_never_echo_a_token(
    fake: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_CLI_MODE", "fail")
    with pytest.raises(ProviderError) as failed:
        await ClaudeCliProvider(model="sonnet", command=fake.path).complete(_req())
    assert "exited 2" in str(failed.value) and "abcdefghijklmnopqrstuvwxyz" not in str(failed.value)
    monkeypatch.setenv("FAKE_CLI_MODE", "slow")
    with pytest.raises(ProviderError, match="timed out"):
        await CodexCliProvider(model="m", command=fake.path, timeout=0.5).complete(_req())
    with pytest.raises(ProviderError, match="not found"):
        await ClaudeCliProvider(model="sonnet", command="/no/such/claude").complete(_req())
    with pytest.raises(ProviderError, match="only chat"):
        await CodexCliProvider(model="m", command=fake.path).embed(["x"])


async def test_a_re_ask_is_written_out_as_a_transcript(fake: Any) -> None:
    fake.emits({"type": "result", "result": "{}", "is_error": False})
    await ClaudeCliProvider(model="sonnet", command=fake.path).complete(
        _req(
            [
                *MSGS,
                Message(role="assistant", content="not json"),
                Message(role="user", content="Return JSON."),
            ]
        )
    )
    stdin = fake.seen()["stdin"]
    assert stdin.index("[USER]\nI wave at Mera.") < stdin.index("[ASSISTANT]\nnot json")
    assert stdin.rstrip().endswith("Reply to the last [USER] message.")


async def test_the_registry_knows_both_kinds(fake: Any) -> None:
    for kind, cls in (("claude_cli", ClaudeCliProvider), ("codex_cli", CodexCliProvider)):
        conn = ModelConnection(id=kind, name=kind, provider=kind, base_url=fake.path)
        built = provider_from_connection(conn, "m", None, reasoning_effort="high")
        assert isinstance(built, cls) and built._command == fake.path
        assert built._reasoning_effort == "high"
        assert classify_modality(kind, "anything") == "chat"
        assert check_reasoning_effort(conn, "max") == "max"
        assert await discover_models(conn, None)


def _req(messages: list[Message] | None = None) -> Any:
    from uro_core.providers.base import CompletionRequest

    return CompletionRequest(messages=messages or MSGS, stage_tag="extractor")
