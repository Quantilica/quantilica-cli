"""Unified CLI entry point for Quantilica fetchers."""

from __future__ import annotations

import logging
import sys
from importlib.metadata import entry_points

import typer
from rich.console import Console
from rich.logging import RichHandler
from rich.table import Table

from quantilica.cli import __version__
from quantilica.cli.manifests import app as manifests_app

FETCHER_GROUP = "quantilica.fetchers"
COMMAND_GROUP = "quantilica.commands"

app = typer.Typer(
    name="quantilica",
    help="Quantilica — ferramentas de dados abertos brasileiros.",
    no_args_is_help=True,
)
app.add_typer(manifests_app, name="manifests")

console = Console()


def _load_plugins(group: str) -> dict[str, typer.Typer]:
    plugins: dict[str, typer.Typer] = {}
    for ep in entry_points(group=group):
        try:
            plugins[ep.name] = ep.load()
        except Exception as exc:
            console.print(
                f"[yellow]Aviso:[/yellow] falha ao carregar plugin '{ep.name}': {exc}"
            )
    return plugins


def _register_plugins() -> None:
    # Fetchers e comandos ficam na raiz: `quantilica <nome>`.
    for name, plugin_app in _load_plugins(FETCHER_GROUP).items():
        app.add_typer(plugin_app, name=name)
    for name, plugin_app in _load_plugins(COMMAND_GROUP).items():
        app.add_typer(plugin_app, name=name)


_register_plugins()


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"quantilica {__version__}")
        raise typer.Exit()


@app.callback()
def root_callback(
    version: bool | None = typer.Option(
        None,
        "--version",
        "-V",
        callback=_version_callback,
        is_eager=True,
        help="Exibe a versão e encerra.",
    ),
) -> None:
    """Quantilica CLI."""


@app.command("list-sources")
def list_sources() -> None:
    """Lista todas as fontes de dados instaladas."""
    plugins = _load_plugins(FETCHER_GROUP)
    if not plugins:
        console.print(
            "[yellow]Nenhum fetcher instalado.[/yellow] "
            "Instale um fetcher e tente novamente."
        )
        return

    table = Table(title="Fontes disponíveis", show_header=True)
    table.add_column("Comando", style="cyan")
    table.add_column("Descrição", style="green")

    for name, plugin_app in sorted(plugins.items()):
        info = getattr(plugin_app, "info", None)
        description = ""
        if info and hasattr(info, "help") and info.help:
            description = info.help
        table.add_row(f"quantilica {name}", description)

    console.print(table)


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore
    logging.basicConfig(
        level=logging.INFO,
        format="%(message)s",
        datefmt="[%X]",
        handlers=[RichHandler(rich_tracebacks=True, show_path=False)],
        force=True,
    )
    app()


if __name__ == "__main__":
    main()
