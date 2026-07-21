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
