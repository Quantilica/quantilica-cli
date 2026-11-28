"""Testes: orçamento do HEAD de frescor do host (``probe_*`` do ``FetcherApp``)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx2
import pytest
from quantilica.core.exceptions import FetchError
from quantilica.core.http import HttpClient
from typer.testing import CliRunner

from quantilica.cli.sdk import CheckPlan
from tests.test_sdk import _grouped, make_app, sample_list_datasets

runner = CliRunner()
ENTRY = sample_list_datasets("exp")[0]


class _Wire:
    """Transporte simulado que registra cada requisição (método e timeout)."""

    def __init__(self, behavior) -> None:
        self.behavior = behavior
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.calls.append((request.method, dict(request.extensions["timeout"])))
        return self.behavior(request)

    @property
    def methods(self) -> list[str]:
        return [method for method, _ in self.calls]


def _client(wire: _Wire, **kwargs: Any) -> HttpClient:
    kwargs.setdefault("timeout", 180.0)
    kwargs.setdefault("attempts", 5)
    kwargs.setdefault("retry_base_delay", 0.0)
    return HttpClient(transport=httpx2.MockTransport(wire), **kwargs)


def _unavailable(request: httpx2.Request) -> httpx2.Response:
    return httpx2.Response(503)


def _connect_error(request: httpx2.Request) -> httpx2.Response:
    raise httpx2.ConnectError("sem rota", request=request)


def _clear_env(monkeypatch) -> None:
    for name in (
        "QUANTILICA_HTTP_TIMEOUT",
        "QUANTILICA_HTTP_ATTEMPTS",
        "QUANTILICA_HTTP_RETRY_DELAY",
    ):
        monkeypatch.delenv(name, raising=False)


# ---------------------------------------------------------------
# orçamento aplicado no sync e no check
# ---------------------------------------------------------------


def test_probe_budget_applies_to_sync_head(tmp_path: Path, monkeypatch):
    _clear_env(monkeypatch)
    wire = _Wire(_unavailable)
    app = make_app(
        client=_client(wire),
        probe_timeout=30.0,
        probe_attempts=2,
        probe_retry_delay=0.0,
    )
    # dry_run percorre só o HEAD de frescor de ``download_entry``.
    app.download_entry(ENTRY, tmp_path, dry_run=True)
    assert wire.methods == ["HEAD", "HEAD"]
    assert {t["connect"] for _, t in wire.calls} == {30.0}
    assert {t["read"] for _, t in wire.calls} == {30.0}
    # O cliente do app não foi tocado.
    assert app.client.timeout == 180.0
    assert app.client.attempts == 5


def test_probe_budget_applies_to_check_head(tmp_path: Path, monkeypatch):
    _clear_env(monkeypatch)
    wire = _Wire(_unavailable)
    app = make_app(
        client=_client(wire),
        probe_timeout=30.0,
        probe_attempts=2,
        probe_retry_delay=0.0,
    )
    item = app.check_entry(ENTRY, tmp_path)
    assert item.action == "download"
    assert item.reason.startswith("metadata-unavailable")
    assert wire.methods == ["HEAD", "HEAD"]
    assert {t["connect"] for _, t in wire.calls} == {30.0}


def test_probe_timeout_is_set_before_entering_the_client(monkeypatch):
    """O httpx nasce no ``__enter__``: o timeout tem de estar lá dentro do ``with``."""
    _clear_env(monkeypatch)
    app = make_app(
        client=_client(_Wire(_unavailable)), probe_timeout=30.0, probe_attempts=2
    )
    probe = app._probe_client()
    assert probe is not None
    assert probe._client is None
    with probe as client:
        assert client._client is not None
        timeout = client._client.timeout
        assert (timeout.connect, timeout.read) == (30.0, 30.0)
    assert probe.attempts == 2
    assert app.client.timeout == 180.0


def test_probe_never_raises_the_client_budget(tmp_path: Path, monkeypatch):
    _clear_env(monkeypatch)
    wire = _Wire(_unavailable)
    app = make_app(
        client=_client(wire, timeout=10.0, attempts=1),
        probe_timeout=30.0,
        probe_attempts=2,
    )
    app.download_entry(ENTRY, tmp_path, dry_run=True)
    assert wire.methods == ["HEAD"]
    assert {t["connect"] for _, t in wire.calls} == {10.0}


def test_probe_budget_in_worker_threads_of_sync(tmp_path: Path, monkeypatch):
    """No ``download_datasets`` o HEAD sai do cliente da thread, com o mesmo teto."""
    _clear_env(monkeypatch)
    wire = _Wire(_connect_error)
    app = make_app(
        client=_client(wire),
        probe_timeout=30.0,
        probe_attempts=2,
        probe_retry_delay=0.0,
        probe_fail_fast_on_connect=True,
    )
    downloaded, total, errors = app.download_datasets([ENTRY], tmp_path, workers=1)
    assert (downloaded, total) == (0, 1)
    assert wire.methods == ["HEAD", "HEAD"]
    assert {t["connect"] for _, t in wire.calls} == {30.0}
    assert "fonte inacessível" in errors[0][1]


# ---------------------------------------------------------------
# ambiente
# ---------------------------------------------------------------


def test_smaller_environment_value_prevails(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("QUANTILICA_HTTP_TIMEOUT", "5")
    monkeypatch.setenv("QUANTILICA_HTTP_ATTEMPTS", "1")
    wire = _Wire(_unavailable)
    app = make_app(
        client=_client(wire),
        probe_timeout=30.0,
        probe_attempts=2,
        probe_retry_delay=0.0,
    )
    app.download_entry(ENTRY, tmp_path, dry_run=True)
    assert wire.methods == ["HEAD"]
    assert {t["connect"] for _, t in wire.calls} == {5.0}
    wire.calls.clear()
    app.check_entry(ENTRY, tmp_path)
    assert wire.methods == ["HEAD"]
    assert {t["connect"] for _, t in wire.calls} == {5.0}


def test_larger_environment_value_does_not_raise_the_probe(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("QUANTILICA_HTTP_TIMEOUT", "400")
    monkeypatch.setenv("QUANTILICA_HTTP_ATTEMPTS", "9")
    wire = _Wire(_unavailable)
    app = make_app(
        client=_client(wire, timeout=400.0, attempts=9),
        probe_timeout=30.0,
        probe_attempts=2,
        probe_retry_delay=0.0,
    )
    app.download_entry(ENTRY, tmp_path, dry_run=True)
    assert wire.methods == ["HEAD", "HEAD"]
    assert {t["connect"] for _, t in wire.calls} == {30.0}


# ---------------------------------------------------------------
# sem parâmetros: nada muda
# ---------------------------------------------------------------


def test_without_probe_params_the_app_client_is_used(tmp_path: Path, monkeypatch):
    _clear_env(monkeypatch)
    wire = _Wire(_unavailable)
    app = make_app(client=_client(wire, attempts=3))
    assert app._probe_client() is None
    app.download_entry(ENTRY, tmp_path, dry_run=True)
    assert wire.methods == ["HEAD"] * 3
    assert {t["connect"] for _, t in wire.calls} == {180.0}
    wire.calls.clear()
    item = app.check_entry(ENTRY, tmp_path)
    assert item.reason.startswith("metadata-unavailable")
    assert wire.methods == ["HEAD"] * 3
    assert {t["connect"] for _, t in wire.calls} == {180.0}


def test_without_probe_params_connect_failure_does_not_fail_the_head(
    tmp_path: Path, monkeypatch
):
    _clear_env(monkeypatch)
    app = make_app(client=_client(_Wire(_connect_error), attempts=2))
    # Como sempre: o HEAD falha em silêncio e o destino usa a data do dia.
    assert app.download_entry(ENTRY, tmp_path, dry_run=True).name.startswith(
        "exp-monthly_2023-05"
    )


def test_non_http_client_ignores_probe_params(tmp_path: Path):
    class _NoHttp:
        def head_last_modified_date(self, url: str) -> None:
            return None

    app = make_app(
        client=_NoHttp(), probe_timeout=30.0, probe_fail_fast_on_connect=True
    )
    assert app._probe_client() is None
    app.download_entry(ENTRY, tmp_path, dry_run=True)


@pytest.mark.parametrize(
    "kwargs",
    [{"probe_timeout": 0}, {"probe_attempts": 0}, {"probe_retry_delay": -1}],
)
def test_invalid_probe_params_are_refused(kwargs):
    with pytest.raises(ValueError):
        make_app(**kwargs)


# ---------------------------------------------------------------
# falha rápida
# ---------------------------------------------------------------


def test_fail_fast_sync_fails_without_trying_the_download(tmp_path: Path, monkeypatch):
    _clear_env(monkeypatch)
    wire = _Wire(_connect_error)
    app = make_app(
        client=_client(wire),
        probe_timeout=30.0,
        probe_attempts=2,
        probe_retry_delay=0.0,
        probe_fail_fast_on_connect=True,
    )
    with pytest.raises(FetchError, match="fonte inacessível"):
        app.download_entry(ENTRY, tmp_path)
    # Só os 2 HEADs do orçamento: nem segundo HEAD (política) nem GET do download.
    assert wire.methods == ["HEAD", "HEAD"]


def test_fail_fast_does_not_trigger_on_non_connect_failures(
    tmp_path: Path, monkeypatch
):
    _clear_env(monkeypatch)

    def slow(request):
        raise httpx2.ReadTimeout("lento", request=request)

    wire = _Wire(slow)
    app = make_app(
        client=_client(wire),
        probe_timeout=30.0,
        probe_attempts=2,
        probe_retry_delay=0.0,
        probe_fail_fast_on_connect=True,
    )
    path = app.download_entry(ENTRY, tmp_path, dry_run=True)
    assert path.name.startswith("exp-monthly_2023-05")
    item = app.check_entry(ENTRY, tmp_path)
    assert item.action == "download"
    assert "fonte inacessível" not in item.reason


def test_fail_fast_check_reports_download_with_clear_reason(
    tmp_path: Path, monkeypatch
):
    _clear_env(monkeypatch)
    wire = _Wire(_connect_error)
    app = make_app(
        client=_client(wire),
        probe_timeout=30.0,
        probe_attempts=2,
        probe_retry_delay=0.0,
        probe_fail_fast_on_connect=True,
    )
    item = app.check_entry(ENTRY, tmp_path)
    assert item.action == "download"
    assert item.reason.startswith("metadata-unavailable: fonte inacessível")
    assert wire.methods == ["HEAD", "HEAD"]


def _entries(n: int) -> list[dict[str, Any]]:
    return [
        {**ENTRY, "id": f"exp-{i}", "url": f"https://example.test/exp-{i}.csv"}
        for i in range(n)
    ]


def _scripted(failing_calls: set[int], wire_calls: list[int] | None = None):
    """Handler que não conecta nas chamadas numeradas (1, 2, ...) e responde nas outras."""
    counter = {"n": 0}

    def handler(request: httpx2.Request) -> httpx2.Response:
        counter["n"] += 1
        if counter["n"] in failing_calls:
            raise httpx2.ConnectError("sem rota", request=request)
        if request.method == "HEAD":
            return httpx2.Response(
                200,
                headers={
                    "Content-Length": "10",
                    "Last-Modified": "Mon, 01 Jan 2024 00:00:00 GMT",
                },
            )
        return httpx2.Response(200, content=b"0123456789")

    return handler


def _fail_fast_app(handler, **kwargs: Any):
    wire = _Wire(handler)
    app = make_app(
        client=_client(wire),
        probe_timeout=30.0,
        probe_attempts=2,
        probe_retry_delay=0.0,
        probe_fail_fast_on_connect=True,
        **kwargs,
    )
    return app, wire


def test_check_confirms_a_real_outage_with_a_second_probe(tmp_path: Path, monkeypatch):
    """Queda real: a 1a entrada deixa o host sob suspeita, a 2a confirma, o resto é rápido."""
    _clear_env(monkeypatch)
    app, wire = _fail_fast_app(_connect_error)
    plan = app.check_datasets(_entries(6), tmp_path, workers=1)
    assert [i.action for i in plan.items] == ["download"] * 6
    assert all(
        i.reason.startswith("metadata-unavailable: fonte inacessível")
        for i in plan.items
    )
    assert (
        len(wire.calls) == 4
    )  # 2 sondas de 2 tentativas; as 4 restantes falham na hora
    assert "confirmada" in plan.items[-1].reason
    # Outra execução recomeça do zero.
    app.check_datasets(_entries(2), tmp_path, workers=1)
    assert len(wire.calls) == 8


def test_check_short_hiccup_does_not_condemn_the_host(tmp_path: Path, monkeypatch):
    """Soluço: só a 1a sonda falha; as demais conectam e o plano tem veredito real."""
    _clear_env(monkeypatch)
    app, wire = _fail_fast_app(_scripted({1, 2}))
    plan = app.check_datasets(_entries(6), tmp_path, workers=1)
    assert plan.items[0].reason.startswith("metadata-unavailable: fonte inacessível")
    assert [i.reason for i in plan.items[1:]] == ["not-present"] * 5
    assert len(wire.calls) == 7


def test_check_simultaneous_failures_do_not_confirm(tmp_path: Path, monkeypatch):
    """Sondas que começam juntas falham juntas no mesmo soluço: isso não confirma."""
    import threading

    _clear_env(monkeypatch)
    barrier = threading.Barrier(4, timeout=10)
    seen = {"n": 0}
    lock = threading.Lock()

    def handler(request: httpx2.Request) -> httpx2.Response:
        with lock:
            seen["n"] += 1
            n = seen["n"]
        if n <= 4:
            barrier.wait()  # as 4 primeiras sondas estão em voo ao mesmo tempo
            raise httpx2.ConnectError("soluço", request=request)
        return _scripted(set())(request)

    app, wire = _fail_fast_app(handler)
    app.probe_attempts = 1
    plan = app.check_datasets(_entries(8), tmp_path, workers=4)
    reasons = [i.reason for i in plan.items]
    assert sum(r.startswith("metadata-unavailable") for r in reasons) == 4
    assert sum(r == "not-present" for r in reasons) == 4
    assert len(wire.calls) == 8


def test_check_success_clears_the_suspicion(tmp_path: Path, monkeypatch):
    _clear_env(monkeypatch)
    # e0 falha (2 chamadas), e1 responde (3a), e2 falha (4a, 5a), e3 falha (6a, 7a) e
    # confirma; e4 já cai na guarda. Sem a limpeza, e2 confirmaria e e3 nem sondaria.
    app, wire = _fail_fast_app(_scripted({1, 2, 4, 5, 6, 7}))
    plan = app.check_datasets(_entries(5), tmp_path, workers=1)
    assert len(wire.calls) == 7
    assert plan.items[1].reason == "not-present"
    assert "confirmada" not in plan.items[3].reason
    assert "confirmada" in plan.items[4].reason


def test_check_memory_does_not_leak_between_calls(tmp_path: Path, monkeypatch):
    from quantilica.cli.sdk import _HostCheck

    _clear_env(monkeypatch)
    app, wire = _fail_fast_app(_scripted(set()))
    app._hosts_check["example.test:"] = _HostCheck(1, "ConnectError", confirmado=True)
    item = app.check_entry(ENTRY, tmp_path)  # chamada avulsa: zera a memória
    assert item.reason == "not-present"
    assert len(wire.calls) == 1

    app, wire = _fail_fast_app(_connect_error)
    app.check_datasets(_entries(4), tmp_path, workers=1)
    assert app._hosts_check == {}
    assert app._em_check_datasets is False
    item = app.check_entry(ENTRY, tmp_path)  # sem herdar a queda da execução anterior
    assert "confirmada" not in item.reason
    assert len(wire.calls) == 4 + 2


def test_sync_has_no_host_memory(tmp_path: Path, monkeypatch):
    """No ``sync`` cada arquivo falha por si: um soluço não derruba os seguintes."""
    from quantilica.cli.sdk import _HostCheck

    _clear_env(monkeypatch)
    app, wire = _fail_fast_app(_scripted({1, 2}))
    # Mesmo com o host marcado como caído pelo ``check``, o sync não consulta isso.
    app._hosts_check["example.test:"] = _HostCheck(1, "ConnectError", confirmado=True)
    downloaded, total, errors = app.download_datasets(_entries(4), tmp_path, workers=1)
    assert (downloaded, total, len(errors)) == (3, 4, 1)
    assert "fonte inacessível" in errors[0][1]

    # --dry-run não levanta erro por memória de host
    assert app.download_entry(ENTRY, tmp_path, dry_run=True).name.startswith(
        "exp-monthly"
    )

    app, wire = _fail_fast_app(_connect_error)
    downloaded, total, errors = app.download_datasets(_entries(6), tmp_path, workers=1)
    assert (downloaded, total, len(errors)) == (0, 6, 6)
    assert wire.methods == ["HEAD"] * 12  # cada arquivo gasta o próprio orçamento


def test_retry_after_is_capped_by_the_probe_retry_delay(tmp_path: Path, monkeypatch):
    import time

    _clear_env(monkeypatch)

    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(503, headers={"Retry-After": "30"})

    app, wire = _fail_fast_app(handler)
    start = time.monotonic()
    app.check_entry(ENTRY, tmp_path)
    assert time.monotonic() - start < 5.0
    assert wire.methods == ["HEAD", "HEAD"]


def test_check_then_sync_from_plan_reports_the_error(tmp_path: Path, monkeypatch):
    """O ciclo da coleta: ``check --out`` termina curto e o ``sync`` acusa o erro."""
    _clear_env(monkeypatch)
    wire = _Wire(_connect_error)
    app = make_app(
        client=_client(wire),
        probe_timeout=30.0,
        probe_attempts=2,
        probe_retry_delay=0.0,
        probe_fail_fast_on_connect=True,
    )
    plan_file = tmp_path / "plan.json"
    summary = tmp_path / "summary.json"
    out = tmp_path / "data"
    result = runner.invoke(
        _grouped(app.app),
        [
            "fd",
            "check",
            "exp",
            "-o",
            str(out),
            "--out",
            str(plan_file),
            "--workers",
            "1",
        ],
    )
    assert result.exit_code == 0, result.output
    assert len(CheckPlan.from_json(plan_file.read_text(encoding="utf-8")).items) == 1
    assert len(wire.calls) == 2

    wire.calls.clear()
    result = runner.invoke(
        _grouped(app.app),
        [
            "fd",
            "sync",
            "--from-plan",
            str(plan_file),
            "-o",
            str(out),
            "--workers",
            "1",
            "--summary-json",
            str(summary),
        ],
    )
    assert result.exit_code == 1, result.output
    data = json.loads(summary.read_text(encoding="utf-8"))
    assert data["errors"][0]["id"] == "exp-monthly"
    assert "fonte inacessível" in data["errors"][0]["message"]
    assert wire.methods == ["HEAD", "HEAD"]


def test_failed_head_is_not_repeated_by_the_second_probe(tmp_path: Path, monkeypatch):
    """Política ``HEAD_OR_MANIFEST``: a sonda falha e o HEAD seguinte seria o mesmo pedido."""
    from quantilica.core.sync import FreshnessPolicy

    _clear_env(monkeypatch)
    wire = _Wire(_unavailable)
    app = make_app(
        client=_client(wire),
        freshness_policy=FreshnessPolicy.HEAD_OR_MANIFEST,
        probe_timeout=30.0,
        probe_attempts=2,
        probe_retry_delay=0.0,
    )
    app.download_entry(ENTRY, tmp_path, dry_run=True)
    assert wire.methods == ["HEAD", "HEAD"]
