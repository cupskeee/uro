"""Providers that run a locally installed, logged-in CLI headlessly (D-72): `claude -p` (Claude
Code) and `codex exec` (Codex). Each call is one subprocess, and authentication is the CLI's own
login on the machine running Uro — so Uro stores no credential, and a subscription (Claude, ChatGPT)
serves the roles bound to it.

Both CLIs are coding AGENTS with tools, and player text reaches these prompts. So every call runs
with all of its tools off, in an empty working directory, with no session saved and no user
settings, MCP servers or project files loaded, and with Uro's system prompt in place of the agent's.
A live check (2026-11-01) had a read-only-sandboxed `codex exec` read a file outside its directory
into its reply until its shell tool was disabled; `--disable` of a feature the installed Codex
doesn't know fails the call, so a renamed shell tool fails closed rather than running unlocked.

Neither serves embeddings. `codex exec` streams no deltas, so its narration arrives at once.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import shutil
import tempfile
from collections.abc import AsyncIterator
from typing import Any

from uro_core.errors import ProviderError
from uro_core.providers.base import CompletionRequest, report_usage

# One call may take a while (a reasoning model, a long extraction); past this the process is killed.
_TIMEOUT = 300.0
# A system prompt is always sent: without one, `claude -p` would use Claude Code's own agent prompt.
_FALLBACK_SYSTEM = "You are a helpful assistant. Answer directly."
# Codex features that give the agent tools; each is switched off. `shell_tool` is the one that
# matters most (it read a file outside the sandboxed directory, live).
CODEX_DISABLED_FEATURES = (
    "shell_tool",
    "browser_use",
    "browser_use_external",
    "computer_use",
    "in_app_browser",
    "image_generation",
    "apps",
    "plugins",
)


def _split(req: CompletionRequest) -> tuple[str, str]:
    """(system prompt, user prompt). The CLIs take one prompt per call, so a multi-turn exchange
    (an extractor re-ask: its answer, then a correction) is written out as a labelled transcript."""
    system = "\n\n".join(m.content for m in req.messages if m.role == "system").strip()
    turns = [m for m in req.messages if m.role != "system"]
    if len(turns) == 1 and turns[0].role == "user":
        prompt = turns[0].content
    else:
        prompt = "\n\n".join(f"[{m.role.upper()}]\n{m.content}" for m in turns)
        prompt += "\n\nReply to the last [USER] message."
    return system or _FALLBACK_SYSTEM, prompt


def _redact(text: str) -> str:
    """A CLI's error line with anything token-shaped masked (an error must never echo a key)."""
    return re.sub(r"[A-Za-z0-9_\-.+/=]{24,}", "[redacted]", text)[:240]


class _CliProvider:
    NAME = ""
    DEFAULT_COMMAND = ""

    def __init__(
        self,
        *,
        model: str,
        command: str | None = None,
        reasoning_effort: str | None = None,
        timeout: float = _TIMEOUT,
    ) -> None:
        self._model = model
        self._command = command or self.DEFAULT_COMMAND
        self._reasoning_effort = reasoning_effort
        self._timeout = timeout
        # empty, and private to this provider: nothing to discover, nothing to read
        self._workdir = tempfile.mkdtemp(prefix=f"uro-{self.NAME}-")

    @property
    def model(self) -> str:
        return self._model

    def _model_flag(self) -> list[str]:
        return [] if self._model in ("", "default") else self._model_args()

    def _model_args(self) -> list[str]:
        raise NotImplementedError

    async def embed(self, texts: list[str]) -> list[list[float]]:
        raise ProviderError(
            f"{self.NAME} serves only chat; bind the 'embedder' role to an embedding provider"
        )

    async def _lines(self, argv: list[str], prompt: str) -> AsyncIterator[str]:
        """Run the CLI with `prompt` on stdin (never in argv, where `ps` would show it) and yield
        its stdout line by line. Non-zero exit, a missing binary or the timeout → ProviderError."""
        executable = shutil.which(self._command) or self._command
        try:
            proc = await asyncio.create_subprocess_exec(
                executable,
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self._workdir,
            )
        except FileNotFoundError as exc:
            raise ProviderError(
                f"{self.NAME}: `{self._command}` not found — install and log in to it, or set the "
                "connection's base_url to its path"
            ) from exc
        assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None
        stderr = asyncio.ensure_future(proc.stderr.read())
        try:
            proc.stdin.write(prompt.encode())
            await proc.stdin.drain()
            proc.stdin.close()
            async with asyncio.timeout(self._timeout):
                while line := await proc.stdout.readline():
                    yield line.decode("utf-8", "replace")
                code = await proc.wait()
            if code != 0:
                err = (await stderr).decode("utf-8", "replace").strip().splitlines()
                last = next((x for x in reversed(err) if "Reading additional input" not in x), "")
                raise ProviderError(f"{self.NAME} exited {code}: {_redact(last)}")
        except TimeoutError as exc:
            raise ProviderError(f"{self.NAME} timed out after {self._timeout:.0f}s") from exc
        finally:
            if proc.returncode is None:  # timed out, failed, or the consumer stopped early
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                await proc.wait()
            stderr.cancel()

    def _system_file(self, system: str) -> str:
        fd, path = tempfile.mkstemp(prefix="system-", suffix=".md", dir=self._workdir)
        with os.fdopen(fd, "w") as f:
            f.write(system)
        return path


class ClaudeCliProvider(_CliProvider):
    """`claude -p`: Claude Code in print mode, as the logged-in user (a Claude subscription or
    whatever login the CLI holds). Streams via `stream-json` partial messages."""

    NAME = "claude_cli"
    DEFAULT_COMMAND = "claude"

    def _model_args(self) -> list[str]:
        return ["--model", self._model]

    def _argv(self, system_file: str, fmt: str) -> list[str]:
        argv = [
            "-p",
            "--system-prompt-file",
            system_file,
            "--tools",
            "",  # no tools at all
            "--strict-mcp-config",  # no MCP servers (none passed)
            "--setting-sources",
            "",  # no user/project settings, so no hooks
            "--no-session-persistence",
            "--disable-slash-commands",
            "--output-format",
            fmt,
        ]
        if fmt == "stream-json":
            argv += ["--verbose", "--include-partial-messages"]
        if self._reasoning_effort:
            argv += ["--effort", self._reasoning_effort]
        return argv + self._model_flag()

    @staticmethod
    def _report(event: dict[str, Any]) -> None:
        usage = event.get("usage") or {}
        if usage:
            tokens_in = sum(
                int(usage.get(k) or 0)
                for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
            )
            report_usage(tokens_in=tokens_in, tokens_out=usage.get("output_tokens"))
        if event.get("is_error"):
            raise ProviderError(f"claude_cli: {_redact(str(event.get('result') or 'error'))}")

    async def stream(self, req: CompletionRequest) -> AsyncIterator[str]:
        system, prompt = _split(req)
        path = self._system_file(system)
        emitted = False
        try:
            async for line in self._lines(self._argv(path, "stream-json"), prompt):
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                kind = event.get("type")
                if kind == "stream_event":
                    inner = event.get("event") or {}
                    delta = inner.get("delta") or {}
                    if inner.get("type") == "content_block_delta" and delta.get("text"):
                        emitted = True
                        yield delta["text"]
                elif kind == "result":
                    self._report(event)
                    if not emitted and event.get("result"):  # a CLI that sent no partials
                        yield str(event["result"])
        finally:
            os.unlink(path)

    async def complete(self, req: CompletionRequest) -> str:
        system, prompt = _split(req)
        path = self._system_file(system)
        try:
            out = "".join([line async for line in self._lines(self._argv(path, "json"), prompt)])
        finally:
            os.unlink(path)
        try:
            event = json.loads(out.strip().splitlines()[-1])
        except (ValueError, IndexError) as exc:
            raise ProviderError("claude_cli returned no JSON result") from exc
        self._report(event)
        return str(event.get("result") or "")


class CodexCliProvider(_CliProvider):
    """`codex exec`: Codex non-interactively, as the logged-in user (a ChatGPT subscription or an
    API key the CLI holds). Its `--json` events carry whole messages, so it doesn't stream."""

    NAME = "codex_cli"
    DEFAULT_COMMAND = "codex"

    def _model_args(self) -> list[str]:
        return ["-m", self._model]

    def _argv(self, system_file: str) -> list[str]:
        argv = [
            "exec",
            "--skip-git-repo-check",
            "--ephemeral",  # no session files
            "--ignore-user-config",  # no user MCP servers or profiles (auth still loads)
            "--ignore-rules",
            "--sandbox",
            "read-only",
            "-C",
            self._workdir,
            "--json",
            "--color",
            "never",
        ]
        for feature in CODEX_DISABLED_FEATURES:
            argv += ["--disable", feature]
        argv += ["-c", 'web_search="disabled"']
        argv += ["-c", f"model_instructions_file={json.dumps(system_file)}"]
        if self._reasoning_effort:
            argv += ["-c", f"model_reasoning_effort={json.dumps(self._reasoning_effort)}"]
        return argv + self._model_flag() + ["-"]  # the prompt comes on stdin

    async def complete(self, req: CompletionRequest) -> str:
        system, prompt = _split(req)
        path = self._system_file(system)
        text = ""
        try:
            async for line in self._lines(self._argv(path), prompt):
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                kind = event.get("type")
                item = event.get("item") or {}
                if kind == "item.completed":
                    if item.get("type") == "agent_message":
                        text = str(item.get("text") or "")
                    elif item.get("type") not in ("reasoning", None):
                        # a tool ran despite every tool being off: never trust that output
                        raise ProviderError(f"codex_cli used a tool ({item.get('type')}); refused")
                elif kind == "turn.completed":
                    usage = event.get("usage") or {}
                    out = usage.get("output_tokens")
                    if out is not None:
                        out = int(out) + int(usage.get("reasoning_output_tokens") or 0)
                    report_usage(tokens_in=usage.get("input_tokens"), tokens_out=out)
                elif kind in ("turn.failed", "error"):
                    detail = (event.get("error") or {}).get("message") or event.get("message")
                    raise ProviderError(f"codex_cli: {_redact(str(detail or kind))}")
        finally:
            os.unlink(path)
        return text

    async def stream(self, req: CompletionRequest) -> AsyncIterator[str]:
        text = await self.complete(req)
        if text:
            yield text
