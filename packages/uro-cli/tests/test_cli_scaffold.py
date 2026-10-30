from typer.testing import CliRunner
from uro_cli.main import app


def test_cli_version_runs() -> None:
    result = CliRunner().invoke(app, ["version"])
    assert result.exit_code == 0
    assert "uro-core" in result.output


def test_provider_codex_login(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """The whole `uro provider codex-login` flow, with codex_auth + the store mocked."""
    from uro_cli import main
    from uro_core.providers import codex_auth

    async def dev(**kw: object) -> dict[str, object]:
        return {
            "device_auth_id": "d",
            "user_code": "J7DE-8NXJS",
            "verification_uri": "https://auth.openai.com/codex/device",
            "interval": 0,  # instant poll — no real waiting in the test
            "expires_in": 10,
        }

    async def poll(dev_id: str, code: str, **kw: object) -> dict[str, str]:
        return {"authorization_code": "AC", "code_verifier": "V"}

    async def exch(code: str, verifier: str, **kw: object) -> dict[str, str]:
        return {"access_token": "AT", "refresh_token": "RT"}

    async def disc(token: str, **kw: object) -> list[dict[str, str]]:
        return [{"id": "gpt-5-codex", "modality": "chat"}]

    monkeypatch.setattr(codex_auth, "request_device_code", dev)
    monkeypatch.setattr(codex_auth, "poll_device_auth", poll)
    monkeypatch.setattr(codex_auth, "exchange_code", exch)
    monkeypatch.setattr(codex_auth, "discover_codex_models", disc)

    class _Store:
        async def add_credential(self, **kw: object) -> str:
            assert kw["auth_mode"] == "oauth_device" and kw["provider"] == "codex"
            return "cred-1"

        async def add_connection(self, **kw: object) -> str:
            return "conn-1"

        async def set_connection_models(self, cid: str, models: object) -> bool:
            return True

        async def close(self) -> None:
            pass

    async def _noop(store: object) -> None:
        return None

    monkeypatch.setattr(main, "build_store", lambda: _Store())
    monkeypatch.setattr(main, "connect_store", _noop)

    result = CliRunner().invoke(app, ["provider", "codex-login", "--no-browser"])
    assert result.exit_code == 0, result.output
    assert "J7DE-8NXJS" in result.output  # the displayed device code
    assert "connected: conn-1" in result.output
    assert "gpt-5-codex" in result.output  # discovered model listed


def test_provider_bind_rejects_a_chat_model_for_the_embedder(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """CLI parity with the server's embedder-modality guard (review): a chat model (codex is always
    chat) can't back the embedder role."""
    from uro_cli import main
    from uro_core.ports.model_registry import ModelConnection

    class _Store:
        async def get_connection(self, cid: str) -> ModelConnection:
            return ModelConnection(id=cid, name="cx", provider="codex", auth_id="a1")

        async def set_role_binding(self, *a: object, **kw: object) -> None:
            raise AssertionError("should not bind — the guard must reject first")

        async def close(self) -> None:
            pass

    async def _noop(store: object) -> None:
        return None

    monkeypatch.setattr(main, "build_store", lambda: _Store())
    monkeypatch.setattr(main, "connect_store", _noop)
    result = CliRunner().invoke(app, ["provider", "bind", "embedder", "conn-1", "gpt-5-codex"])
    assert result.exit_code == 1
    assert "embedding model" in result.output


def _credential_store(monkeypatch, linked: list[str]):  # type: ignore[no-untyped-def]
    """A fake registry holding credential `cred-1`, used by the connections in `linked`."""
    from uro_cli import main
    from uro_core.ports.model_registry import ModelConnection

    deleted: list[str] = []

    class _Cred:
        id = "cred-1"

    class _Store:
        async def list_credentials(self) -> list[_Cred]:
            return [_Cred()]

        async def list_connections(self) -> list[ModelConnection]:
            return [
                ModelConnection(id=c, name=c, provider="codex", auth_id="cred-1") for c in linked
            ]

        async def delete_credential(self, credential_id: str) -> bool:
            deleted.append(credential_id)
            return True

        async def close(self) -> None:
            pass

    async def _noop(store: object) -> None:
        return None

    monkeypatch.setattr(main, "build_store", lambda: _Store())
    monkeypatch.setattr(main, "connect_store", _noop)
    return deleted


def test_provider_credential_rm_deletes_an_unused_credential(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    deleted = _credential_store(monkeypatch, linked=[])
    result = CliRunner().invoke(app, ["provider", "credential", "rm", "cred-1"])
    assert result.exit_code == 0, result.output
    assert deleted == ["cred-1"] and "deleted" in result.output


def test_provider_credential_rm_refuses_one_in_use_without_force(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Deleting a linked credential unlinks its connections (docs/20) — live roles would lose auth,
    so the CLI makes that an explicit --force."""
    deleted = _credential_store(monkeypatch, linked=["conn-1"])
    refused = CliRunner().invoke(app, ["provider", "credential", "rm", "cred-1"])
    assert refused.exit_code == 1 and "conn-1" in refused.output and deleted == []
    forced = CliRunner().invoke(app, ["provider", "credential", "rm", "cred-1", "--force"])
    assert forced.exit_code == 0, forced.output
    assert deleted == ["cred-1"] and "unlinked (now unauthenticated): conn-1" in forced.output


def test_provider_credential_rm_reports_an_unknown_id(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    deleted = _credential_store(monkeypatch, linked=[])
    result = CliRunner().invoke(app, ["provider", "credential", "rm", "nope"])
    assert result.exit_code == 1 and "no such credential: nope" in result.output and deleted == []


def test_token_revoke_by_participant_and_list(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from uro_cli import main

    calls: list[tuple[str, object]] = []

    def fake(server: str, path: str, token: str, body: object) -> dict[str, object]:
        calls.append((path, body))
        if path.endswith("/revoke"):
            return {"revoked": True, "count": 2}
        return {
            "tokens": [
                {
                    "token_id": "abc123def456",
                    "participant_id": "bob",
                    "revoked": False,
                    "created_at": "2026-10-30T00:00:00Z",
                }
            ]
        }

    monkeypatch.setattr(main, "_server_post", fake)
    out = CliRunner().invoke(
        app, ["token", "revoke", "c1", "--participant", "bob", "--token", "op"]
    )
    assert out.exit_code == 0 and "revoked 2 token(s) for bob" in out.output
    assert calls[-1] == ("/campaigns/c1/tokens/revoke", {"participant": "bob"})
    out = CliRunner().invoke(app, ["token", "list", "c1", "--token", "op"])
    assert out.exit_code == 0 and "abc123def456  bob" in out.output and "live" in out.output
    assert calls[-1] == ("/campaigns/c1/tokens", None)  # a GET
    both = CliRunner().invoke(
        app, ["token", "revoke", "c1", "--participant", "bob", "--target", "t", "--token", "op"]
    )
    assert both.exit_code == 1
