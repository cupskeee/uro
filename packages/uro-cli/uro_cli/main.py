"""Entry point for the `uro` command. Command surface per docs/08-api-and-sessions.md."""

import typer

app = typer.Typer(no_args_is_help=True, help="Uro Engine — reference client.")


@app.callback()
def main() -> None:
    """Uro Engine — play, dry-run, and dev tools against the engine."""


@app.command()
def version() -> None:
    """Print engine and client versions."""
    import uro_core

    import uro_cli

    typer.echo(f"uro-cli {uro_cli.__version__} / uro-core {uro_core.__version__}")
