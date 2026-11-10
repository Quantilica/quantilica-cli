"""Comando ``quantilica registry`` — o registro dos fetchers instalados.

Descobre os entry points ``quantilica.fetchers`` (e ``quantilica.lake``) e monta
o ``registry.json`` com :func:`quantilica.core.registry.build_registry`.
"""

from __future__ import annotations

import contextlib
import json
import sys
from dataclasses import dataclass
from importlib.metadata import entry_points
from typing import Annotated, Any

import typer
from quantilica.core.registry import (
    FetcherInfo,
    RegistryError,
    build_registry,
    check_unique,
)
from rich.console import Console
from rich.table import Table

FETCHER_GROUP = "quantilica.fetchers"
LAKE_GROUP = "quantilica.lake"

app = typer.Typer(
    help="Registro dos fetchers instalados (ids, apelidos, fatos da fonte).",
    no_args_is_help=True,
)


@app.callback()
def _registry_group() -> None:
    """Registro dos fetchers instalados."""


@dataclass
class DiscoveredFetcher:
    """Um fetcher instalado, com o que o registro precisa saber dele."""

    info: FetcherInfo
    info_declared: bool
    entry_point: str
    distribution: str | None
    source_id: str | None = None
    freshness_policy: str | None = None
    default_output: str | None = None
    groups: list[str] | None = None
    lake: bool = False
    lake_source_id: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Info do fetcher mais os campos de execução."""
        return {
            **self.info.to_dict(),
            "info_declared": self.info_declared,
            "entry_point": self.entry_point,
            "distribution": self.distribution,
            "source_id": self.source_id,
            "freshness_policy": self.freshness_policy,
            "default_output": self.default_output,
            "groups": self.groups,
            "lake": self.lake,
            "lake_source_id": self.lake_source_id,
            "error": self.error,
        }


def _fallback_info(ep_name: str, dist: str | None) -> FetcherInfo:
    from quantilica.cli.sdk import derive_fetcher_info

    return derive_fetcher_info(dist or ep_name)


def _find_fetcher_app(obj: Any) -> Any | None:
    """Acha o ``FetcherApp`` a partir do objeto carregado do entry point."""
    from quantilica.cli.sdk import FetcherApp

    if isinstance(obj, FetcherApp):
        return obj
    candidate = getattr(obj, "fetcher_app", None)
    return candidate if isinstance(candidate, FetcherApp) else None


def discover_fetchers() -> list[DiscoveredFetcher]:
    """Carrega os entry points de fetchers e devolve o que cada um declara.

    Nunca levanta por causa de um plugin quebrado: o erro vai em ``error``.
    A saída padrão durante o carregamento é desviada para o stderr (módulos de
    fetchers podem imprimir ao importar).
    """
    lake_eps = list(entry_points(group=LAKE_GROUP))
    found: list[DiscoveredFetcher] = []
    with contextlib.redirect_stdout(sys.stderr):
        for ep in entry_points(group=FETCHER_GROUP):
            dist = ep.dist.name if ep.dist is not None else None
            try:
                fetcher = _find_fetcher_app(ep.load())
                error = None
            except Exception as exc:
                fetcher, error = None, f"{type(exc).__name__}: {exc}"
            if fetcher is not None:
                policy = fetcher.freshness_policy
                item = DiscoveredFetcher(
                    info=fetcher.info,
                    info_declared=fetcher.info_declared,
                    entry_point=ep.name,
                    distribution=dist,
                    source_id=fetcher.source_id,
                    freshness_policy=(
                        None
                        if policy is None
                        else str(getattr(policy, "value", policy))
                    ),
                    default_output=str(fetcher.default_output),
                    groups=sorted(fetcher.groups),
                )
            else:
                item = DiscoveredFetcher(
                    info=_fallback_info(ep.name, dist),
                    info_declared=False,
                    entry_point=ep.name,
                    distribution=dist,
                    error=error,
                )
            for lep in lake_eps:
                ldist = lep.dist.name if lep.dist is not None else None
                if (dist is not None and ldist == dist) or lep.name in (
                    item.info.names
                ):
                    item.lake = True
                    item.lake_source_id = lep.name
                    break
            found.append(item)
    return found


def build_export(found: list[DiscoveredFetcher]) -> tuple[dict[str, Any], str | None]:
    """Monta o registro; devolve também a mensagem de duplicidade, se houver."""
    infos = [d.info for d in found]
    problem: str | None = None
    try:
        registry = build_registry(infos)
    except RegistryError as exc:
        problem = str(exc)
        registry = {"version": 1, "fetchers": []}
    extras = sorted(found, key=lambda d: (d.info.id, d.entry_point))
    registry["fetchers"] = [d.to_dict() for d in extras]
    return registry, problem


@app.command("export")
def cmd_export(
    as_json: Annotated[
        bool, typer.Option("--json", help="Saída em JSON puro no stdout.")
    ] = False,
    strict: Annotated[
        bool,
        typer.Option(
            "--strict",
            help="Falha se algum fetcher não declarar info, não carregar "
            "ou houver id/apelido duplicado.",
        ),
    ] = False,
) -> None:
    """Exporta o registro dos fetchers instalados (entry points)."""
    found = discover_fetchers()
    registry, duplicate = build_export(found)
    if duplicate is None:
        with contextlib.suppress(RegistryError):
            check_unique(d.info for d in found)
    undeclared = [d.info.id for d in found if not d.info_declared]
    errors = [f"{d.entry_point}: {d.error}" for d in found if d.error]

    err = Console(stderr=True)
    if as_json:
        sys.stdout.write(json.dumps(registry, ensure_ascii=False, indent=2) + "\n")
    else:
        table = Table(title=f"Registro de fetchers ({len(found)})")
        for col in ("Id", "Apelidos", "Órgão", "Cadência", "Frescor", "Info", "Lake"):
            table.add_column(col)
        for d, row in zip(
            sorted(found, key=lambda d: (d.info.id, d.entry_point)),
            registry["fetchers"],
            strict=True,
        ):  # mesma ordenação de build_export
            table.add_row(
                d.info.id,
                ", ".join(d.info.aliases) or "-",
                d.info.orgao or "-",
                d.info.cadencia or "-",
                d.freshness_policy or "-",
                "[green]sim[/green]" if d.info_declared else "[yellow]não[/yellow]",
                "sim" if row["lake"] else "-",
            )
        Console().print(table)
    if duplicate:
        err.print(f"[red]Duplicidade:[/red] {duplicate}", markup=True)
    for line in errors:
        err.print(f"[red]Falha ao carregar[/red] {line}", markup=True, highlight=False)
    if undeclared:
        err.print(
            f"[yellow]{len(undeclared)} fetcher(s) sem info declarada:[/yellow] "
            + ", ".join(sorted(undeclared)),
            markup=True,
            highlight=False,
        )
    if strict and (duplicate or undeclared or errors):
        raise typer.Exit(1)


def declared_source_names() -> dict[str, str]:
    """Mapa ``id/apelido -> distribuição`` dos fetchers instalados que declaram info."""
    names: dict[str, str] = {}
    for d in discover_fetchers():
        if d.info_declared and d.distribution:
            for name in d.info.names:
                names[name] = d.distribution
    return names
