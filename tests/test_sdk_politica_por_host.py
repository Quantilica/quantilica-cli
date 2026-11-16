"""``download_datasets`` com política por host: o ritmo é do host, não da thread."""

from __future__ import annotations

import time
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any

import pytest
from quantilica.core.http import HostPolicy, HttpClient

from quantilica.cli.sdk import FetcherApp
from tests.fake_http_server import BODY, HOST, FakeServer
from tests.test_sdk import ALIASES, GROUPS, sample_list_datasets

WORKERS = 8
ENTRIES = 12
# Intervalo longo e tolerância relativa: o relógio do Windows tem 15,6 ms de
# resolução e a máquina sob carga atrasa as threads.
INTERVALO = 0.2
TOLERANCIA = 0.5

requer_core_novo = pytest.mark.skipif(
    not hasattr(HttpClient, "share_host_state"),
    reason="exige o quantilica-core com share_host_state",
)


def make_entries(server: FakeServer, n: int = ENTRIES) -> list[dict[str, Any]]:
    return [
        {
            "group": "exp",
            "id": f"exp-{i}",
            "ext": "csv",
            "year": 2020 + i,
            "url": server.url(f"/dados{i}.csv"),
        }
        for i in range(n)
    ]


def path_builder(output_dir: Path, entry: dict[str, Any], last_modified: Any) -> Path:
    return output_dir / "exp" / f"{entry['id']}.csv"


def make_app(policy: HostPolicy | None, **client_kwargs: Any) -> FetcherApp:
    client = HttpClient(
        attempts=3,
        retry_base_delay=0.01,
        jitter=0.0,
        host_policies={HOST: policy} if policy is not None else None,
        **client_kwargs,
    )
    return FetcherApp(
        name="politica-fetcher",
        groups_dict=GROUPS,
        aliases_dict=ALIASES,
        list_datasets=sample_list_datasets,
        path_builder=path_builder,
        client=client,
    )


def run(app: FetcherApp, server: FakeServer, out: Path, n: int = ENTRIES):
    return app.download_datasets(make_entries(server, n), out, workers=WORKERS)


@requer_core_novo
def test_max_concurrency_e_min_interval_valem_para_todas_as_threads(tmp_path: Path):
    n = 6
    with FakeServer(body_delay=0.5) as server:
        app = make_app(HostPolicy(min_interval=INTERVALO, max_concurrency=2))
        done, total, errors = run(app, server, tmp_path, n)
    assert (done, total, errors) == (n, n, [])
    assert len(list((tmp_path / "exp").glob("*.csv"))) == n
    assert server.max_active <= 2
    times = server.times()
    assert len(times) >= n * 2  # HEAD + GET de cada entrada
    gaps = [b - a for a, b in zip(times, times[1:], strict=False)]
    assert min(gaps) >= TOLERANCIA * INTERVALO


@requer_core_novo
def test_429_pausa_todas_as_threads(tmp_path: Path):
    n = 24  # três levas de 8 threads: sobra trabalho depois do 429
    with FakeServer(body_delay=0.4) as server:
        t429: list[float] = []

        def responder(handler: BaseHTTPRequestHandler, method: str, path: str) -> bool:
            # O primeiro GET do servidor leva 429; os demais seguem normais.
            if method == "GET" and not t429:
                t429.append(time.monotonic())
                handler.send_response(429)
                handler.send_header("Retry-After", "1")
                handler.send_header("Content-Length", "0")
                handler.end_headers()
                return True
            return False

        server.responder = responder
        app = make_app(
            HostPolicy(min_interval=0.02, max_concurrency=8, backoff_base=0.5)
        )
        done, total, errors = run(app, server, tmp_path, n)
    assert (done, total, errors) == (n, n, [])
    assert t429
    # Nenhuma thread chega ao servidor durante a pausa (a folga inicial deixa
    # passar só o que já estava a caminho quando o 429 foi registrado).
    na_pausa = [t for t in server.times() if t429[0] + 0.3 < t < t429[0] + 0.9]
    assert na_pausa == []
    assert max(server.times()) > t429[0] + 0.9  # e o trabalho retoma depois


def test_sem_politica_o_paralelismo_segue_o_mesmo(tmp_path: Path):
    with FakeServer(body_delay=0.3) as server:
        app = make_app(None)
        done, total, errors = run(app, server, tmp_path)
    assert (done, total, errors) == (ENTRIES, ENTRIES, [])
    assert server.max_active > 2  # sem teto: as threads baixam juntas
    assert (tmp_path / "exp" / "exp-0.csv").read_bytes() == BODY


def test_core_sem_share_host_state_cai_no_comportamento_antigo(
    tmp_path: Path, monkeypatch
):
    monkeypatch.delattr(HttpClient, "share_host_state", raising=False)
    with FakeServer(body_delay=0.05) as server:
        app = make_app(HostPolicy(min_interval=0.0, max_concurrency=2))
        done, total, errors = run(app, server, tmp_path)
    assert (done, total, errors) == (ENTRIES, ENTRIES, [])


@requer_core_novo
def test_o_client_do_app_volta_ao_original(tmp_path: Path):
    with FakeServer(body_delay=0.0) as server:
        app = make_app(HostPolicy(min_interval=0.0, max_concurrency=2))
        original = app.client
        run(app, server, tmp_path)
    assert app.client is original


def test_aviso_de_core_antigo_sai_uma_vez_por_processo(
    tmp_path: Path, monkeypatch, caplog
):
    import logging

    from quantilica.cli import sdk

    monkeypatch.delattr(HttpClient, "share_host_state", raising=False)
    monkeypatch.setattr(sdk, "_aviso_core_antigo", False)
    with FakeServer(body_delay=0.05) as server:
        app = make_app(HostPolicy(min_interval=0.0, max_concurrency=2))
        with caplog.at_level(logging.WARNING):
            run(app, server, tmp_path)
            run(app, server, tmp_path)  # segundo download, mesmas threads novas
    avisos = [r for r in caplog.records if "share_host_state" in r.getMessage()]
    assert len(avisos) == 1
