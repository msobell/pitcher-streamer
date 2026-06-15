"""Command-line entry point for pitcher-streamer.

Usage:
    pitcher-streamer serve [--host H] [--port P] [--reload]

`serve` checks the port is free and the required files exist before handing off
to uvicorn, so a second launch fails loudly instead of silently colliding.
"""

from __future__ import annotations

import socket
from pathlib import Path

import click

_CONFIG_PATH = Path("config.yaml")
_PARK_FACTORS_PATH = Path("park_factors.json")


@click.group()
def cli() -> None:
    """Pitcher Streamer — Yahoo Fantasy Baseball pitcher streaming dashboard."""


@cli.command()
@click.option("--host", default="127.0.0.1", show_default=True,
              help="Interface to bind. Use 0.0.0.0 to expose on your LAN.")
@click.option("--port", default=8001, type=int, show_default=True)
@click.option("--reload", is_flag=True, help="Auto-reload on code changes (dev).")
def serve(host: str, port: int, reload: bool) -> None:
    """Start the web dashboard."""
    # Preconditions — fail with an actionable message before uvicorn spins up.
    if not _CONFIG_PATH.exists():
        raise click.ClickException(
            "config.yaml not found. Copy config.yaml.example to config.yaml "
            "and fill in your league details."
        )
    if not _PARK_FACTORS_PATH.exists():
        raise click.ClickException(
            "park_factors.json not found. Run: python refresh_park_factors.py"
        )

    # Port guard — connect_ex == 0 means something is already listening.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        if s.connect_ex(("127.0.0.1", port)) == 0:
            raise click.ClickException(
                f"Port {port} is already in use — is the dashboard already running?"
            )

    # 0.0.0.0 binds all interfaces, but you still browse to localhost.
    shown = "localhost" if host in ("127.0.0.1", "0.0.0.0") else host
    click.echo(f"Pitcher Streamer → http://{shown}:{port}")

    import uvicorn
    uvicorn.run("main:app", host=host, port=port, reload=reload)


if __name__ == "__main__":
    cli()
