from typer.testing import CliRunner
from uro_cli.main import app


def test_cli_version_runs() -> None:
    result = CliRunner().invoke(app, ["version"])
    assert result.exit_code == 0
    assert "uro-core" in result.output
