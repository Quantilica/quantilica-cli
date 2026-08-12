"""Unified CLI entry point for Quantilica fetchers."""

from __future__ import annotations

import logging
import sys
from importlib.metadata import entry_points
from typing import Annotated

import typer
from rich.console import Console
from rich.logging import RichHandler
from rich.table import Table

from quantilica.cli import __version__
from quantilica.cli.manifests import app as manifests_app
from quantilica.cli.sources import (
    app as sources_app,
)
from quantilica.cli.sources import (
    cmd_doctor,
    cmd_install,
    cmd_uninstall,
    fetch_remote_sources,
)

FETCHER_GROUP = "quantilica.fetchers"
COMMAND_GROUP = "quantilica.commands"

app = typer.Typer(
    name="quantilica",
    help="Quantilica — ferramentas de dados abertos brasileiros.",
    no_args_is_help=True,
)
app.add_typer(manifests_app, name="manifests")
app.add_typer(sources_app, name="sources")

# Adiciona comandos top-level install, uninstall e doctor
app.command("install")(cmd_install)
app.command("uninstall")(cmd_uninstall)
app.command("doctor")(cmd_doctor)

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
def list_sources(
    remote: Annotated[
        bool,
        typer.Option(
            "--remote", help="Exibe todas as fontes conhecidas no registro remoto"
        ),
    ] = False,
) -> None:
    """Lista as fontes de dados instaladas ou disponíveis no repositório."""
    plugins = _load_plugins(FETCHER_GROUP)

    if remote:
        remote_registry = fetch_remote_sources()
        table = Table(title="Todas as fontes de dados conhecidas", show_header=True)
        table.add_column("Fonte", style="cyan")
        table.add_column("Pacote", style="magenta")
        table.add_column("Status", style="green")

        for name, dist in sorted(remote_registry.items()):
            status = (
                "[green]Instalado[/green]"
                if name in plugins
                else "[dim]Não instalado[/dim]"
            )
            table.add_row(name, dist, status)

        console.print(table)
        return

    if not plugins:
        console.print(
            "[yellow]Nenhum fetcher instalado.[/yellow]\n"
            "Use 'quantilica install <fonte>' para instalar um fetcher (ex: quantilica install comex)."
        )
        return

    table = Table(title="Fontes instaladas", show_header=True)
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
    """Main entry point for the quantilica CLI."""
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
