"""Unified CLI entry point for Quantilica fetchers."""

from __future__ import annotations

import logging
import sys
from importlib.metadata import entry_points
from typing import Annotated

import typer
from quantilica.core.registry import FetcherInfo
from rich.console import Console
from rich.logging import RichHandler
from rich.table import Table

from quantilica.cli import __version__
from quantilica.cli.catalog import app as catalog_app
from quantilica.cli.docs import app as docs_app
from quantilica.cli.health import cmd_health
from quantilica.cli.lake import app as lake_app
from quantilica.cli.manifests import app as manifests_app
from quantilica.cli.registry import app as registry_app
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
app.add_typer(catalog_app, name="catalog")
app.add_typer(docs_app, name="docs")
app.add_typer(lake_app, name="lake")
app.add_typer(manifests_app, name="manifests")
app.add_typer(registry_app, name="registry")
app.add_typer(sources_app, name="sources")

# Adiciona comandos top-level install, uninstall, doctor e health
app.command("install")(cmd_install)
app.command("uninstall")(cmd_uninstall)
app.command("doctor")(cmd_doctor)
app.command("health")(cmd_health)

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


def _declared_info(plugin_app: typer.Typer) -> FetcherInfo | None:
    """``FetcherInfo`` declarado pelo plugin (``None`` se não for um FetcherApp)."""
    fetcher = getattr(plugin_app, "fetcher_app", None)
    if fetcher is None or not getattr(fetcher, "info_declared", False):
        return None
    info = getattr(fetcher, "info", None)
    return info if isinstance(info, FetcherInfo) else None


def _warn(message: str) -> None:
    sys.stderr.write(f"Aviso: {message}\n")


def _mount_fetchers(plugins: dict[str, typer.Typer], reserved: set[str]) -> None:
    """Monta um subcomando visível por fetcher e os apelidos como ocultos.

    O nome visível é o id canônico (``info.id``); plugin sem ``info`` declarado
    segue com o nome do entry point. O nome do entry point e os apelidos viram
    subcomandos ocultos para o mesmo Typer. Precedência: embutidos > ids
    canônicos > nomes de entry point > apelidos. Apelido que colide com um
    nome de precedência maior, ou que dois plugins reivindicam, não é montado
    (aviso em stderr); nunca derruba o CLI.
    """
    taken = set(reserved)
    canonical: dict[str, typer.Typer] = {}
    for ep_name, plugin_app in sorted(plugins.items()):
        info = _declared_info(plugin_app)
        name = info.id if info is not None else ep_name
        canonical.setdefault(name, plugin_app)
        app.add_typer(plugin_app, name=name)
    taken |= set(canonical)

    def claim(tier: list[tuple[str, str, typer.Typer, str]]) -> None:
        owners: dict[str, set[str]] = {}
        for name, owner, _, _ in tier:
            owners.setdefault(name, set()).add(owner)
        warned: set[str] = set()
        for name, owner, plugin_app, kind in tier:
            if name in warned:
                continue
            if name in taken:
                _warn(
                    f"{kind} '{name}' de '{owner}' colide com um comando existente; "
                    "não foi montado."
                )
                warned.add(name)
            elif len(owners[name]) > 1:
                _warn(
                    f"{kind} '{name}' é reivindicado por mais de um plugin "
                    f"({', '.join(sorted(owners[name]))}); não foi montado."
                )
                warned.add(name)
            else:
                app.add_typer(plugin_app, name=name, hidden=True)
        taken.update(n for n, o, _, _ in tier if len(owners[n]) == 1)

    eps_tier: list[tuple[str, str, typer.Typer, str]] = []
    alias_tier: list[tuple[str, str, typer.Typer, str]] = []
    for ep_name, plugin_app in sorted(plugins.items()):
        info = _declared_info(plugin_app)
        if info is None:
            continue
        if ep_name != info.id:
            eps_tier.append((ep_name, info.id, plugin_app, "nome de entry point"))
        for alias in info.aliases:
            alias_tier.append((alias, info.id, plugin_app, "apelido"))
    seen: set[tuple[str, str]] = set()
    for tier in (eps_tier, alias_tier):
        # mesmo nome repetido pelo mesmo plugin (apelido == entry point) vale uma vez
        tier[:] = [
            t for t in tier if not ((t[0], t[1]) in seen or seen.add((t[0], t[1])))
        ]
        claim(tier)


def _register_plugins() -> None:
    # Fetchers e comandos ficam na raiz: `quantilica <nome>`.
    commands = _load_plugins(COMMAND_GROUP)
    reserved = {
        "catalog", "docs", "lake", "manifests", "registry", "sources",
        "install", "uninstall", "doctor", "health", "list-sources",
    } | set(commands)  # fmt: skip
    _mount_fetchers(_load_plugins(FETCHER_GROUP), reserved)
    for name, plugin_app in commands.items():
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
        declared = _declared_info(plugin_app)
        table.add_row(f"quantilica {declared.id if declared else name}", description)

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
