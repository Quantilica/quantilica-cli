"""Comando ``quantilica registry`` — o registro dos fetchers instalados.

Descobre os entry points ``quantilica.fetchers`` (e ``quantilica.lake``) e monta
o ``registry.json`` (versão 2) com
:func:`quantilica.core.registry.build_registry`. A identidade de empacotamento
(distribuição, módulo, repositório, versão) sai dos metadados da distribuição
instalada; os fatos do ecossistema entram por ``--ecossistema registro.toml``.
"""

from __future__ import annotations

import contextlib
import json
import sys
from dataclasses import dataclass, field
from importlib.metadata import entry_points
from pathlib import Path
from typing import Annotated, Any

import typer
from quantilica.core.registry import (
    REGISTRY_VERSION,
    FetcherInfo,
    IdentidadePacote,
    RegistroEcossistema,
    RegistryError,
    build_registry,
    carrega_registro_ecossistema,
    check_identidade,
    check_unique,
    registry_sha256,
)
from rich.console import Console
from rich.markup import escape
from rich.table import Table

FETCHER_GROUP = "quantilica.fetchers"
LAKE_GROUP = "quantilica.lake"
REPO_URL_PADRAO = "https://github.com/Quantilica/{distribution}"
# Rótulos de ``[project.urls]`` que apontam o repositório, em ordem de preferência.
_REPO_LABELS = ("repository", "source", "source code")

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
    module: str | None = None
    repo: str | None = None
    version: str | None = None
    lake_entry_point: str | None = None
    warnings: list[str] = field(default_factory=list)

    def identidade(self) -> IdentidadePacote:
        """A identidade de empacotamento (camada 2 do registro)."""
        return IdentidadePacote(
            distribution=self.distribution,
            module=self.module,
            repo=self.repo,
            entry_point=self.entry_point,
            lake_entry_point=self.lake_entry_point,
            source_id=self.source_id,
            lake_source_id=self.lake_source_id,
            version=self.version,
        )

    def extras(self) -> dict[str, Any]:
        """Campos de execução que acompanham a identidade no registro."""
        return {
            "info_declared": self.info_declared,
            "freshness_policy": self.freshness_policy,
            "default_output": self.default_output,
            "groups": self.groups,
            "lake": self.lake,
            "error": self.error,
        }

    def to_dict(self) -> dict[str, Any]:
        """Info do fetcher, identidade e campos de execução."""
        return {
            **self.info.to_dict(),
            **self.identidade().to_dict(),
            **self.extras(),
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


def _project_urls(dist: Any) -> dict[str, str]:
    """``[project.urls]`` da distribuição, com o rótulo em minúsculas."""
    try:
        raw = dist.metadata.get_all("Project-URL") or []
    except Exception:
        return {}
    urls: dict[str, str] = {}
    for item in raw:
        label, sep, url = str(item).partition(",")
        if sep and url.strip():
            urls.setdefault(label.strip().lower(), url.strip())
    return urls


def _module_of(ep: Any) -> str | None:
    """Pacote de topo do entry point (``a_fetcher.plugin:app`` -> ``a_fetcher``)."""
    value = getattr(ep, "value", None)
    if not isinstance(value, str) or not value.strip():
        return None
    return value.partition(":")[0].strip().split(".")[0] or None


def _repo_of(dist: Any, name: str | None) -> tuple[str | None, str | None]:
    """URL do repositório e o aviso, quando cai no padrão."""
    if name is None:
        return None, None
    urls = _project_urls(dist)
    for label in _REPO_LABELS:
        if label in urls:
            return urls[label].rstrip("/"), None
    return (
        REPO_URL_PADRAO.format(distribution=name),
        f"{name}: sem [project.urls] Repository/Source; repo assumido como "
        f"{REPO_URL_PADRAO.format(distribution=name)}",
    )


def discover_fetchers() -> list[DiscoveredFetcher]:
    """Carrega os entry points de fetchers e devolve o que cada um declara.

    Nunca levanta por causa de um plugin quebrado: o erro vai em ``error``.
    A saída padrão durante o carregamento é desviada para o stderr (módulos de
    fetchers podem imprimir ao importar). A identidade de empacotamento
    (``module``, ``repo``, ``version``) sai dos metadados da distribuição.
    """
    lake_eps = list(entry_points(group=LAKE_GROUP))
    found: list[DiscoveredFetcher] = []
    with contextlib.redirect_stdout(sys.stderr):
        for ep in entry_points(group=FETCHER_GROUP):
            dist_obj = ep.dist
            dist = dist_obj.name if dist_obj is not None else None
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
            item.module = _module_of(ep)
            item.version = getattr(dist_obj, "version", None)
            item.repo, aviso = _repo_of(dist_obj, dist)
            if aviso:
                item.warnings.append(aviso)
            for lep in lake_eps:
                ldist = lep.dist.name if lep.dist is not None else None
                if (dist is not None and ldist == dist) or lep.name in (
                    item.info.names
                ):
                    item.lake = True
                    item.lake_source_id = lep.name
                    item.lake_entry_point = lep.name
                    break
            found.append(item)
    return found


def build_export(
    found: list[DiscoveredFetcher],
    ecossistema: dict[str, RegistroEcossistema] | None = None,
) -> tuple[dict[str, Any], str | None]:
    """Monta o registro v2; devolve também a mensagem de duplicidade, se houver.

    Com duplicidade de id/apelido o registro sai sem a camada do ecossistema
    (o ``--strict`` falha de qualquer forma).
    """
    infos = [d.info for d in found]
    try:
        check_unique(infos)
    except RegistryError as exc:
        registry = {
            "version": REGISTRY_VERSION,
            "fetchers": [
                d.to_dict()
                for d in sorted(found, key=lambda d: (d.info.id, d.entry_point))
            ],
        }
        registry["sha256"] = registry_sha256(registry)
        return registry, str(exc)
    registry = build_registry(
        infos,
        {d.info.id: d.identidade() for d in found},
        ecossistema,
        {d.info.id: d.extras() for d in found},
    )
    return registry, None


@app.command("export")
def cmd_export(
    as_json: Annotated[
        bool, typer.Option("--json", help="Saída em JSON puro no stdout.")
    ] = False,
    strict: Annotated[
        bool,
        typer.Option(
            "--strict",
            help="Falha se algum fetcher não declarar info, não carregar, "
            "houver id/apelido duplicado, a distribuição fugir da regra "
            "<id>-fetcher sem historico que a explique ou (com --ecossistema) "
            "faltar a seção de algum fetcher.",
        ),
    ] = False,
    ecossistema: Annotated[
        Path | None,
        typer.Option(
            "--ecossistema",
            help="registro.toml com os fatos do ecossistema (docs_pasta, "
            "vitrine, historico...); junta a terceira camada ao registro.",
            dir_okay=False,
        ),
    ] = None,
) -> None:
    """Exporta o registro dos fetchers instalados (entry points)."""
    found = discover_fetchers()
    err = Console(stderr=True)
    eco: dict[str, RegistroEcossistema] | None = None
    if ecossistema is not None:
        try:
            eco = carrega_registro_ecossistema(ecossistema)
            registry, duplicate = build_export(found, eco)
        except RegistryError as exc:
            err.print(f"[red]Registro do ecossistema:[/red] {exc}", markup=True)
            raise typer.Exit(1) from exc
    else:
        registry, duplicate = build_export(found)
    undeclared = [d.info.id for d in found if not d.info_declared]
    errors = [f"{d.entry_point}: {d.error}" for d in found if d.error]
    fora_da_regra = check_identidade(
        (d.info for d in found),
        {d.info.id: d.identidade() for d in found},
        eco,
    )
    sem_secao = (
        sorted(d.info.id for d in found if d.info.id not in eco)
        if eco is not None
        else []
    )

    if as_json:
        sys.stdout.write(json.dumps(registry, ensure_ascii=False, indent=2) + "\n")
    else:
        table = Table(title=f"Registro de fetchers ({len(found)})")
        for col in (
            "Id",
            "Apelidos",
            "Órgão",
            "Cadência",
            "Redistrib.",
            "Frescor",
            "Info",
            "Lake",
        ):
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
                d.info.redistribuicao or "-",
                d.freshness_policy or "-",
                "[green]sim[/green]" if d.info_declared else "[yellow]não[/yellow]",
                "sim" if row["lake"] else "-",
            )
        Console().print(table)
    if duplicate:
        err.print(f"[red]Duplicidade:[/red] {duplicate}", markup=True)
    for line in errors:
        err.print(f"[red]Falha ao carregar[/red] {line}", markup=True, highlight=False)
    for d in found:
        for aviso in d.warnings:
            err.print(
                f"[yellow]Aviso:[/yellow] {escape(aviso)}",
                markup=True,
                highlight=False,
            )
    for line in fora_da_regra:
        err.print(
            f"[red]Identidade:[/red] {escape(line)}", markup=True, highlight=False
        )
    if sem_secao:
        err.print(
            f"[yellow]{len(sem_secao)} fetcher(s) sem seção no registro do "
            "ecossistema:[/yellow] " + ", ".join(sem_secao),
            markup=True,
            highlight=False,
        )
    if undeclared:
        err.print(
            f"[yellow]{len(undeclared)} fetcher(s) sem info declarada:[/yellow] "
            + ", ".join(sorted(undeclared)),
            markup=True,
            highlight=False,
        )
    if strict and (duplicate or undeclared or errors or fora_da_regra or sem_secao):
        raise typer.Exit(1)


def declared_source_names() -> dict[str, str]:
    """Mapa ``id/apelido -> distribuição`` dos fetchers instalados que declaram info."""
    names: dict[str, str] = {}
    for d in discover_fetchers():
        if d.info_declared and d.distribution:
            for name in d.info.names:
                names[name] = d.distribution
    return names
