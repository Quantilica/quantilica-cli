"""Testes para o comando `quantilica health` (sondas de disponibilidade)."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

import httpx2
from typer.testing import CliRunner

from quantilica.cli import health
from quantilica.cli.cli import app
from quantilica.cli.health import HEALTH_SOURCES

runner = CliRunner()

SOURCE_NAMES = [name for name, _ in HEALTH_SOURCES]
URLS = {url: name for name, url in HEALTH_SOURCES}


def _handler(status_by_url: dict[str, int], head_405: bool = False):
    """Cria um handler de MockTransport mapeando URL -> status HTTP."""

    def handle(request: httpx2.Request) -> httpx2.Response:
        url = str(request.url).split("?")[0]
        status = status_by_url.get(url, 200)
        if request.method == "HEAD" and head_405:
            return httpx2.Response(405)
        return httpx2.Response(status)

    return handle


def _patch_client(
    status_by_url: dict[str, int],
    head_405: bool = False,
):
    """Parchea o cliente de sondas com um MockTransport determinístico."""
    transport = httpx2.MockTransport(_handler(status_by_url, head_405))
    fake_client = httpx2.Client(transport=transport, follow_redirects=True)
    return patch.object(health, "_make_client", return_value=fake_client)


def _all_ok() -> dict[str, int]:
    return {url: 200 for url in URLS}


def _parse_json(output: str) -> dict[str, Any]:
    lines = [line for line in output.splitlines() if line.startswith("{")]
    assert lines, f"nenhuma linha JSON na saída: {output!r}"
    return json.loads(lines[-1])


# --- modo tabela -----------------------------------------------------------


def test_health_table_all_ok() -> None:
    with _patch_client(_all_ok()):
        result = runner.invoke(app, ["health"])
    assert result.exit_code == 0
    for name in SOURCE_NAMES:
        assert name in result.output
    assert "OK" in result.output
    assert "FALHA" not in result.output


def test_health_table_with_failure() -> None:
    statuses = _all_ok()
    statuses["https://balanca.economia.gov.br"] = 500
    with _patch_client(statuses):
        result = runner.invoke(app, ["health"])
    assert result.exit_code == 0
    assert "FALHA" in result.output
    assert "OK" in result.output


def test_health_head_fallback_to_get() -> None:
    # HEAD retorna 405 e o CLI deve refazer com GET (levemente diferente).
    handlers: list[str] = []

    def handle(request: httpx2.Request) -> httpx2.Response:
        handlers.append(request.method)
        if request.method == "HEAD":
            return httpx2.Response(405)
        return httpx2.Response(200)

    transport = httpx2.MockTransport(handle)
    with patch.object(
        health, "_make_client", return_value=httpx2.Client(transport=transport)
    ):
        result = runner.invoke(app, ["health"])
    assert result.exit_code == 0
    assert "OK" in result.output
    assert "HEAD" in handlers
    assert "GET" in handlers


# --- modo --json -------------------------------------------------------------


def test_health_json_all_ok() -> None:
    with _patch_client(_all_ok()):
        result = runner.invoke(app, ["health", "--json"])
    assert result.exit_code == 0
    payload = _parse_json(result.output)
    assert payload["status"] == "ok"
    source_names = [s["name"] for s in payload["sources"]]
    assert source_names == SOURCE_NAMES
    for source in payload["sources"]:
        assert source["status"] == "ok"
        assert source["http_status"] == 200
        assert isinstance(source["latency_ms"], (int, float))
        assert source["error"] is None


def test_health_json_degraded() -> None:
    statuses = _all_ok()
    statuses["https://balanca.economia.gov.br"] = 503
    with _patch_client(statuses):
        result = runner.invoke(app, ["health", "--json"])
    assert result.exit_code == 0
    payload = _parse_json(result.output)
    assert payload["status"] == "degraded"
    comex = next(s for s in payload["sources"] if s["name"] == "comex")
    assert comex["status"] == "falha"
    assert comex["http_status"] == 503
    assert comex["error"] == "HTTP 503"


def test_health_json_network_error() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        if "inmet" in str(request.url):
            raise httpx2.ConnectError("boom")
        return httpx2.Response(200)

    transport = httpx2.MockTransport(handler)
    with patch.object(
        health, "_make_client", return_value=httpx2.Client(transport=transport)
    ):
        result = runner.invoke(app, ["health", "--json"])
    assert result.exit_code == 0
    payload = _parse_json(result.output)
    assert payload["status"] == "degraded"
    inmet = next(s for s in payload["sources"] if s["name"] == "inmet")
    assert inmet["status"] == "falha"
    assert inmet["http_status"] is None
    assert "ConnectError" in inmet["error"]


# --- --fail-fast ------------------------------------------------------------


def test_health_fail_fast_exit_code_ok() -> None:
    with _patch_client(_all_ok()):
        result = runner.invoke(app, ["health", "--fail-fast", "--json"])
    assert result.exit_code == 0
    payload = _parse_json(result.output)
    assert payload["status"] == "ok"


def test_health_fail_fast_exit_code_on_failure() -> None:
    statuses = _all_ok()
    statuses["https://balanca.economia.gov.br"] = 500
    with _patch_client(statuses):
        result = runner.invoke(app, ["health", "--fail-fast"])
    assert result.exit_code == 1


def test_health_fail_fast_stops_probes() -> None:
    # com a primeira fonte falhando em fail-fast, as demais não são sondadas.
    probes: list[str] = []

    def fake_probe(client: httpx2.Client, name: str, url: str) -> dict[str, object]:
        probes.append(name)
        status = "falha" if name == "bcb" else "ok"
        return {
            "name": name,
            "url": url,
            "status": status,
            "http_status": 500 if status == "falha" else 200,
            "latency_ms": 1.0,
            "error": None if status == "ok" else "HTTP 500",
        }

    with (
        _patch_client(_all_ok()),
        patch.object(health, "_probe", side_effect=fake_probe),
    ):
        result = runner.invoke(app, ["health", "--fail-fast"])
    assert result.exit_code == 1
    assert probes == ["bcb"]


# --- --timeout ------------------------------------------------------------


def test_health_passes_timeout_to_client() -> None:
    with patch.object(
        health, "_make_client", return_value=httpx2.Client()
    ) as mock_client:
        result = runner.invoke(app, ["health", "--timeout", "2.5", "--json"])
    assert result.exit_code == 0
    mock_client.assert_called_once_with(timeout=2.5)


def test_health_sources_canonical_list() -> None:
    assert SOURCE_NAMES == ["bcb", "sidra", "tesouro_direto", "comex", "inmet"]
