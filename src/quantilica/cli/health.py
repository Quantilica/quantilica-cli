"""Comando `quantilica health` — sondas de disponibilidade das fontes.

Realiza sondas HTTP leves (HEAD com fallback para GET) contra as fontes
canônicas de dados do ecossistema Quantilica e reporta o resultado em
tabela Rich ou JSON estruturado.
"""

from __future__ import annotations

import concurrent.futures
import json
import time
from typing import Annotated

import httpx2
import typer
from rich.console import Console
from rich.table import Table

console = Console()

DEFAULT_TIMEOUT = 5.0
USER_AGENT = "quantilica-cli (health)"

# Lista canônica de fontes sondadas (nome, URL de sonda leve).
HEALTH_SOURCES: list[tuple[str, str]] = [
    ("bcb", "https://api.bcb.gov.br/dados/serie/bcdata.sgs.1/dados?formato=json"),
    ("sidra", "https://servicodados.ibge.gov.br/api/v3/calendario"),
    (
        "tesouro_direto",
        "https://www.tesourotransparente.gov.br/ckan/api/3/action/package_search?rows=1",
    ),
    ("comex", "https://balanca.economia.gov.br"),
    ("inmet", "https://apitempo.inmet.gov.br/estacoes"),
]


def _make_client(timeout: float) -> httpx2.Client:
    """Cria o cliente HTTP usado pelas sondas.

    Args:
        timeout: Timeout (segundos) aplicado a cada requisição.

    Returns:
        Um httpx2.Client configurado com redirects e User-Agent da CLI.
    """
    return httpx2.Client(
        timeout=timeout,
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT},
    )


def _probe(client: httpx2.Client, name: str, url: str) -> dict[str, object]:
    """Executa uma sonda HEAD (com fallback GET) contra uma fonte.

    Args:
        client: O cliente HTTP a ser usado.
        name: Nome canônico da fonte.
        url: URL alvo da sonda.

    Returns:
        Dicioário com name, url, status, http_status, latency_ms e error.
    """
    start = time.perf_counter()
    try:
        response = client.head(url)
        # Muitos endpoints não implementam HEAD — fallback para GET leve.
        if response.status_code >= 400:
            response = client.get(url)
        latency_ms = (time.perf_counter() - start) * 1000.0
        if response.status_code < 400:
            return {
                "name": name,
                "url": url,
                "status": "ok",
                "http_status": response.status_code,
                "latency_ms": round(latency_ms, 1),
                "error": None,
            }
        return {
            "name": name,
            "url": url,
            "status": "falha",
            "http_status": response.status_code,
            "latency_ms": round(latency_ms, 1),
            "error": f"HTTP {response.status_code}",
        }
    except Exception as exc:
        latency_ms = (time.perf_counter() - start) * 1000.0
        return {
            "name": name,
            "url": url,
            "status": "falha",
            "http_status": None,
            "latency_ms": round(latency_ms, 1),
            "error": f"{type(exc).__name__}: {exc}",
        }


def _run_probes(timeout: float, fail_fast: bool) -> list[dict[str, object]]:
    """Sonda todas as fontes, em sequência (fail-fast) ou em paralelo.

    Args:
        timeout: Timeout por requisição, em segundos.
        fail_fast: Se True, interrompe as sondas na primeira falha.

    Returns:
        A lista de resultados por fonte.
    """
    with _make_client(timeout=timeout) as client:
        if fail_fast:
            results = []
            for name, url in HEALTH_SOURCES:
                result = _probe(client, name, url)
                results.append(result)
                if result["status"] != "ok":
                    break
            return results

        with concurrent.futures.ThreadPoolExecutor(
            max_workers=len(HEALTH_SOURCES)
        ) as pool:
            futures = [
                (name, pool.submit(_probe, client, name, url))
                for name, url in HEALTH_SOURCES
            ]
            return [future.result() for _, future in futures]


def _render_table(results: list[dict[str, object]]) -> None:
    """Renderiza o relatório de health como tabela Rich.

    Args:
        results: Resultados das sondas.
    """
    table = Table(title="Health — fontes de dados", show_header=True)
    table.add_column("Fonte", style="cyan")
    table.add_column("Status")
    table.add_column("Código", justify="right")
    table.add_column("Latência (ms)", justify="right")

    for r in results:
        if r["status"] == "ok":
            status = "[green]OK[/green]"
        else:
            status = "[red]FALHA[/red]"
        code = str(r["http_status"]) if r["http_status"] is not None else "-"
        latency = str(r["latency_ms"]) if r["latency_ms"] is not None else "-"
        table.add_row(str(r["name"]), status, code, latency)

    console.print(table)


def cmd_health(
    json_output: Annotated[
        bool,
        typer.Option(
            "--json",
            help="Saída em JSON estruturado em vez de tabela.",
        ),
    ] = False,
    fail_fast: Annotated[
        bool,
        typer.Option(
            "--fail-fast",
            help="Interrompe as sondas na primeira falha e encerra com código 1.",
        ),
    ] = False,
    timeout: Annotated[
        float,
        typer.Option(
            "--timeout",
            help="Timeout (s) aplicado a cada sonda HTTP.",
        ),
    ] = DEFAULT_TIMEOUT,
) -> None:
    """Verifica a disponibilidade das fontes de dados (sondas HTTP leves)."""
    results = _run_probes(timeout, fail_fast)

    if json_output:
        status = "ok" if all(r["status"] == "ok" for r in results) else "degraded"
        print(json.dumps({"status": status, "sources": results}))
    else:
        _render_table(results)

    if fail_fast and any(r["status"] != "ok" for r in results):
        raise typer.Exit(code=1)
