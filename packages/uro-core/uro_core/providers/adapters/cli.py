"""Providers that run a locally installed, logged-in CLI headlessly (D-72): `claude -p` (Claude
Code) and `codex exec` (Codex). Each call is one subprocess, and authentication is the CLI's own
login on the machine running Uro — so Uro stores no credential, and a subscription (Claude, ChatGPT)
serves the roles bound to it.

Both CLIs are coding AGENTS with tools, and player text reaches these prompts. So each call runs in
a fresh, empty directory removed afterwards, with the prompt on stdin, Uro's system prompt in place
of the agent's, no session saved, no user settings or MCP servers, an environment holding only what
the CLI needs to find its own login, and the agent's tools switched off: Claude Code's built-in tool
set is emptied, and every Codex feature in `CODEX_DISABLED_FEATURES` is disabled after checking the
installed Codex still lists it (a retired feature name would be accepted and do nothing). A live
check (2026-11-01) had a read-only-sandboxed `codex exec` read a file outside its directory into its
reply until its shell tool was disabled.

Neither serves embeddings. `codex exec` streams no deltas, so its narration arrives at once.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import secrets
import shutil
import tempfile
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable
from typing import Any, TypeVar

from uro_core.errors import ProviderError
from uro_core.providers.base import CompletionRequest, report_usage

T = TypeVar("T")

# One call may take a while (a reasoning model, a long extraction); past this the process is killed.
_TIMEOUT = 300.0
# A CLI's output can be one long JSON line (a whole extraction); asyncio's default is 64 KiB.
_LINE_LIMIT = 16 * 1024 * 1024
# A system prompt is always sent: without one, `claude -p` would use Claude Code's own agent prompt.
_FALLBACK_SYSTEM = "You are a helpful assistant. Answer directly."
# Codex features that give the agent a tool or run something on its own (0.163). Each is disabled;
# `shell_tool` and `unified_exec` matter most (the shell read a file outside the directory, live).
CODEX_DISABLED_FEATURES = (
    "shell_tool",
    "unified_exec",
    "unified_exec_tty",
    "view_image",
    "browser_use",
    "browser_use_external",
    "browser_use_full_cdp_access",
    "browser_annotation_api",
    "computer_use",
    "in_app_browser",
    "in_app_local_automation",
    "image_generation",
    "apps",
    "plugins",
    "remote_plugin",
    "multi_agent",
    "multi_agent_v2",
    "sleep_tool",
    "goals",
    "skill_search",
    "skill_mcp_dependency_install",
    "tool_suggest",
    "hooks",
    "code_mode_host",
)
# The environment a CLI gets: enough to find its own login and the network, nothing of Uro's. The
# whole environment used to pass through, so an ANTHROPIC_API_KEY loaded from .env billed that key
# instead of the subscription, and the agent saw URO_SECRET_KEY and the database URL (review).
_ENV_KEEP = frozenset(
    {
        "PATH", "HOME", "USER", "LOGNAME", "SHELL", "TMPDIR", "TERM", "LANG", "TZ",
        "CODEX_HOME", "CLAUDE_CONFIG_DIR",
        "SSL_CERT_FILE", "SSL_CERT_DIR", "NODE_EXTRA_CA_CERTS", "REQUESTS_CA_BUNDLE",
        "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "ALL_PROXY",
        "https_proxy", "http_proxy", "no_proxy", "all_proxy",
        "__CF_USER_TEXT_ENCODING",
        "SYSTEMROOT", "APPDATA", "LOCALAPPDATA", "USERPROFILE",
    }
)  # fmt: skip
_ENV_KEEP_PREFIXES = ("LC_", "XDG_")


def child_env() -> dict[str, str]:
    """The environment a CLI runs with (see `_ENV_KEEP`)."""
    return {
        k: v for k, v in os.environ.items() if k in _ENV_KEEP or k.startswith(_ENV_KEEP_PREFIXES)
    }


def _split(req: CompletionRequest) -> tuple[str, str]:
    """(system prompt, user prompt). The CLIs take one prompt per call, so a multi-turn exchange
    (the narrator's recent beats, an extractor re-ask) is written out as a transcript whose turn
    markers carry a random token: a player's text can't contain it, so it can't forge a turn
    (review: plain [USER]/[ASSISTANT] labels could be typed into an intent)."""
    system = "\n\n".join(m.content for m in req.messages if m.role == "system").strip()
    turns = [m for m in req.messages if m.role != "system"]
    if len(turns) == 1 and turns[0].role == "user":
        prompt = turns[0].content
    else:
        tag = secrets.token_hex(4)
        prompt = (
            f"The conversation so far follows. Each turn starts with a line `=== ROLE {tag} ===`;"
            " only those lines mark turns.\n\n"
            + "\n\n".join(f"=== {m.role.upper()} {tag} ===\n{m.content}" for m in turns)
            + "\n\nReply to the last USER turn."
        )
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

    @property
    def model(self) -> str:
        return self._model

    def _model_flag(self) -> list[str]:
        return [] if self._model in ("", "default") else self._model_args()

    def _model_args(self) -> list[str]:
        raise NotImplementedError

    def _argv(self, workdir: str, system_file: str, fmt: str) -> list[str]:
        raise NotImplementedError

    async def embed(self, texts: list[str]) -> list[list[float]]:
        raise ProviderError(
            f"{self.NAME} serves only chat; bind the 'embedder' role to an embedding provider"
        )

    async def _ready(self) -> None:
        """A check before the first call (Codex: its tool features); nothing for Claude."""

    async def _run(self, system: str, prompt: str, fmt: str) -> AsyncIterator[str]:
        """One CLI call in a fresh directory (the system prompt beside it, removed with it)."""
        await self._ready()
        with tempfile.TemporaryDirectory(prefix=f"uro-{self.NAME}-") as root:
            workdir = os.path.join(root, "work")
            os.mkdir(workdir)  # empty: nothing to discover, nothing to read
            system_file = os.path.join(root, "system.md")
            with open(system_file, "w") as f:
                f.write(system)
            argv = self._argv(workdir, system_file, fmt)
            # closed here, not left to the GC: the directory must outlive the process
            async with contextlib.aclosing(self._lines(argv, prompt, workdir)) as lines:
                async for line in lines:
                    yield line

    async def _lines(self, argv: list[str], prompt: str, workdir: str) -> AsyncGenerator[str, None]:
        """Run the CLI with `prompt` on stdin (never in argv, where `ps` would show it) and yield
        its stdout line by line. Any failure — a missing binary, a non-zero exit, a broken pipe,
        an overlong line, the timeout — is a ProviderError. The timeout counts only time spent
        waiting on the CLI, never the consumer's between lines (review: a timeout spanning a
        `yield` cancelled the consumer — the server's beat task — instead)."""
        loop = asyncio.get_running_loop()
        budget = self._timeout  # seconds of waiting on the CLI left; the consumer's time is free
        executable = shutil.which(self._command) or self._command
        try:
            proc = await asyncio.create_subprocess_exec(
                executable,
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=workdir,
                env=child_env(),
                limit=_LINE_LIMIT,
            )
        except OSError as exc:
            raise ProviderError(
                f"{self.NAME}: can't run `{self._command}` ({type(exc).__name__}) — install and log"
                " in to it, or set the connection's base_url to its path"
            ) from exc
        assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None
        stderr = asyncio.ensure_future(proc.stderr.read())

        async def wait(aw: Awaitable[T]) -> T:
            nonlocal budget
            if budget <= 0:
                raise TimeoutError
            start = loop.time()
            try:
                return await asyncio.wait_for(aw, budget)
            finally:
                budget -= loop.time() - start

        try:
            try:
                proc.stdin.write(prompt.encode())
                await wait(proc.stdin.drain())
                proc.stdin.close()
            except (BrokenPipeError, ConnectionResetError):
                pass  # it exited without reading: its exit code and stderr say why, below
            while True:
                try:
                    line = await wait(proc.stdout.readline())
                except (ValueError, asyncio.LimitOverrunError) as exc:
                    raise ProviderError(f"{self.NAME} wrote a line over the size limit") from exc
                if not line:
                    break
                yield line.decode("utf-8", "replace")
            code = await wait(proc.wait())
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


class ClaudeCliProvider(_CliProvider):
    """`claude -p`: Claude Code in print mode, as the logged-in user (a Claude subscription or
    whatever login the CLI holds). Streams via `stream-json` partial messages."""

    NAME = "claude_cli"
    DEFAULT_COMMAND = "claude"

    def _model_args(self) -> list[str]:
        return ["--model", self._model]

    def _argv(self, workdir: str, system_file: str, fmt: str) -> list[str]:
        argv = [
            "-p",
            "--system-prompt-file",
            system_file,
            "--tools",
            "",  # an empty built-in tool set
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
        emitted = False
        async for line in self._run(system, prompt, "stream-json"):
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

    async def complete(self, req: CompletionRequest) -> str:
        system, prompt = _split(req)
        out = "".join([line async for line in self._run(system, prompt, "json")])
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

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._features_checked = False

    def _model_args(self) -> list[str]:
        return ["-m", self._model]

    async def _ready(self) -> None:
        """Before the first call, check the installed Codex still lists every feature this adapter
        disables. `--disable` of a RETIRED feature is accepted and does nothing, so a shell moved
        under a new name would run unlocked; refuse instead (review)."""
        if self._features_checked:
            return
        listing = self._lines(["features", "list"], "", tempfile.gettempdir())
        lines = [line async for line in listing]
        state: dict[str, str] = {}
        for line in lines:
            parts = line.split()
            if len(parts) >= 2:
                state[parts[0]] = parts[1]
        retired = [f for f in CODEX_DISABLED_FEATURES if state.get(f, "removed") == "removed"]
        if retired:
            raise ProviderError(
                f"codex_cli: this Codex no longer has {', '.join(retired)} as a live "
                "feature, so its tools can't be verified off; refusing to run it"
            )
        self._features_checked = True

    def _argv(self, workdir: str, system_file: str, fmt: str) -> list[str]:
        argv = [
            "exec",
            "--skip-git-repo-check",
            "--ephemeral",  # no session files
            "--ignore-user-config",  # no user MCP servers or profiles (auth still loads)
            "--ignore-rules",
            "--sandbox",
            "read-only",
            "-C",
            workdir,
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
        text, notice = "", ""
        async for line in self._run(system, prompt, "json"):
            try:
                event = json.loads(line)
            except ValueError:
                continue
            kind = event.get("type")
            item = event.get("item") or {}
            if kind == "item.completed":
                if item.get("type") == "agent_message":
                    text = str(item.get("text") or "")
                elif item.get("type") == "error":
                    # a notice, not a tool: disabling `code_mode_host` reports that code mode will
                    # fail closed. Kept in case no reply follows.
                    notice = str(item.get("message") or "")
                elif item.get("type") not in ("reasoning", None):
                    # a tool ran despite its features being off: never trust that output
                    raise ProviderError(f"codex_cli used a tool ({item.get('type')}); refused")
            elif kind == "turn.completed":
                usage = event.get("usage") or {}
                # output_tokens already includes reasoning (review: adding it counted it twice)
                report_usage(
                    tokens_in=usage.get("input_tokens"), tokens_out=usage.get("output_tokens")
                )
            elif kind in ("turn.failed", "error"):
                detail = (event.get("error") or {}).get("message") or event.get("message")
                raise ProviderError(f"codex_cli: {_redact(str(detail or kind))}")
        if not text and notice:
            raise ProviderError(f"codex_cli: {_redact(notice)}")
        return text

    async def stream(self, req: CompletionRequest) -> AsyncIterator[str]:
        text = await self.complete(req)
        if text:
            yield text
