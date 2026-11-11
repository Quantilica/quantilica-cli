"""`quantilica catalog sync` com ids canônicos e metadados do registro."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from quantilica.core.registry import FetcherInfo
from typer.testing import CliRunner

from quantilica.cli.catalog import (
    _registry_from_infos,
    build_catalog,
    extract_catalog_payload,
    load_registry_file,
)
from quantilica.cli.cli import app

runner = CliRunner()

TD = FetcherInfo(
    id="tesouro-direto",
    nome="Tesouro Direto",
    aliases=("td",),
    orgao="Tesouro Nacional",
    licenca="CC-BY-4.0",
    homepage="https://www.tesourodireto.com.br",
    cadencia="daily",
    dominio="financas-publicas",
    redistribuicao="aberta",
)
B3 = FetcherInfo(id="b3", nome="B3", orgao="B3 S.A.", redistribuicao="restrita")


@pytest.fixture
def registro() -> dict[str, FetcherInfo]:
    return _registry_from_infos([(TD, ("tesouro_direto",)), (B3, ())])


def _dataset(
    lake: Path,
    source: str,
    dataset: str,
    *,
    linhas: tuple[int, ...] = (),
    **extra: Any,
) -> Path:
    d = lake / source / dataset
    d.mkdir(parents=True)
    desc = {"source_id": source, "dataset_id": dataset, "alias": dataset, **extra}
    (d / "_dataset.json").write_text(json.dumps(desc), encoding="utf-8")
    for i, n in enumerate(linhas):
        pq.write_table(pa.table({"x": list(range(n))}), d / f"part-{i}.parquet")
    return d


def test_apelido_vira_id_canonico(
    tmp_path: Path, registro: dict[str, FetcherInfo]
) -> None:
    _dataset(tmp_path, "td", "precos", linhas=(2,))
    payload = extract_catalog_payload(tmp_path, registry=registro)
    assert [s["id"] for s in payload["sources"]] == ["tesouro-direto"]
    ds = payload["datasets"][0]
    assert ds["id"] == "tesouro-direto/precos"
    assert ds["source_id"] == "tesouro-direto"
    assert ds["fetcher_id"] == "tesouro-direto"
    assert ds["lake_path"] == "td/precos"  # caminho físico não muda
    assert {c["dataset_id"] for c in payload["columns"]} == {"tesouro-direto/precos"}
    assert payload["samples"][0]["dataset_id"] == "tesouro-direto/precos"


def test_id_extra_do_lake_resolve_para_o_canonico(
    tmp_path: Path, registro: dict[str, FetcherInfo]
) -> None:
    _dataset(tmp_path, "tesouro_direto", "precos")
    payload = extract_catalog_payload(tmp_path, registry=registro)
    assert payload["datasets"][0]["id"] == "tesouro-direto/precos"


def test_metadados_da_fonte_vem_do_registro(
    tmp_path: Path, registro: dict[str, FetcherInfo]
) -> None:
    _dataset(tmp_path, "tesouro-direto", "precos")
    src = extract_catalog_payload(tmp_path, registry=registro)["sources"][0]
    assert src["nome"] == "Tesouro Direto"
    assert src["orgao_emissor"] == "Tesouro Nacional"
    assert src["licenca"] == "CC-BY-4.0"
    assert src["homepage_url"] == "https://www.tesourodireto.com.br"
    assert src["dominio_tematico"] == "financas-publicas"
    assert src["cadencia"] == "daily"
    assert src["sigla"] is None  # o registro não tem sigla


def test_fonte_desconhecida_mantem_id_e_manda_nulos(
    tmp_path: Path, registro: dict[str, FetcherInfo], caplog: pytest.LogCaptureFixture
) -> None:
    _dataset(tmp_path, "misterio", "tabela")
    with caplog.at_level(logging.WARNING, logger="quantilica.cli.catalog"):
        build = build_catalog(tmp_path, registry=registro)
    src = build.payload["sources"][0]
    assert src["id"] == "misterio"
    for campo in (
        "nome",
        "sigla",
        "orgao_emissor",
        "dominio_tematico",
        "licenca",
        "homepage_url",
        "cadencia",
    ):
        assert src[campo] is None, campo
    assert build.payload["datasets"][0]["id"] == "misterio/tabela"
    assert build.payload["datasets"][0]["fetcher_id"] is None
    assert build.unknown == {"misterio"}
    assert "misterio" in caplog.text


def test_fonte_restrita_fica_de_fora(
    tmp_path: Path, registro: dict[str, FetcherInfo], caplog: pytest.LogCaptureFixture
) -> None:
    _dataset(tmp_path, "b3", "cotacoes", linhas=(3,))
    _dataset(tmp_path, "b3", "ofertas", linhas=(3,))
    _dataset(tmp_path, "td", "precos")
    with caplog.at_level(logging.INFO, logger="quantilica.cli.catalog"):
        build = build_catalog(tmp_path, registry=registro)
    p = build.payload
    assert [s["id"] for s in p["sources"]] == ["tesouro-direto"]
    assert [d["id"] for d in p["datasets"]] == ["tesouro-direto/precos"]
    assert all(not c["dataset_id"].startswith("b3") for c in p["columns"])
    assert all(not s["dataset_id"].startswith("b3") for s in p["samples"])
    assert build.restricted == {"b3": 2}
    assert "2 dataset(s)" in caplog.text


def test_filtro_por_fonte_aceita_apelido(
    tmp_path: Path, registro: dict[str, FetcherInfo]
) -> None:
    _dataset(tmp_path, "td", "precos")
    _dataset(tmp_path, "outra", "x")
    payload = extract_catalog_payload(tmp_path, source_filter="td", registry=registro)
    assert [d["id"] for d in payload["datasets"]] == ["tesouro-direto/precos"]


def test_total_linhas_soma_os_parquets(
    tmp_path: Path, registro: dict[str, FetcherInfo]
) -> None:
    _dataset(tmp_path, "tesouro-direto", "precos", linhas=(3, 4, 5))
    ds = extract_catalog_payload(tmp_path, registry=registro)["datasets"][0]
    assert ds["total_linhas"] == 12
    assert ds["total_arquivos"] == 3


def test_nada_inventado_no_dataset(
    tmp_path: Path, registro: dict[str, FetcherInfo]
) -> None:
    _dataset(tmp_path, "tesouro-direto", "precos", linhas=(1,))
    _dataset(tmp_path, "misterio", "tabela")
    payload = extract_catalog_payload(tmp_path, registry=registro)
    for ds in payload["datasets"]:
        assert ds["frequencia_temporal"] is None
        assert ds["granularidade_espacial"] is None
        assert ds["descricao"] is None
        assert ds["manifest_sha256"] is None
    texto = json.dumps(payload, ensure_ascii=False)
    for inventado in ("Open Data", "Geral", "mensal", "nacional"):
        assert inventado not in texto


def test_dataset_usa_o_que_o_descritor_declara(
    tmp_path: Path, registro: dict[str, FetcherInfo]
) -> None:
    _dataset(
        tmp_path,
        "tesouro-direto",
        "precos",
        frequencia_temporal="diaria",
        granularidade_espacial="nacional",
    )
    ds = extract_catalog_payload(tmp_path, registry=registro)["datasets"][0]
    assert ds["frequencia_temporal"] == "diaria"
    assert ds["granularidade_espacial"] == "nacional"


def test_registro_de_arquivo_ignora_campos_de_execucao(tmp_path: Path) -> None:
    f = tmp_path / "registry.json"
    f.write_text(
        json.dumps(
            {
                "version": 1,
                "fetchers": [
                    {
                        **TD.to_dict(),
                        "info_declared": True,
                        "entry_point": "td",
                        "lake_source_id": "td-lake",
                    },
                    {**B3.to_dict(), "info_declared": False},
                ],
            }
        ),
        encoding="utf-8",
    )
    reg = load_registry_file(f)
    assert reg["td"].id == "tesouro-direto"
    assert reg["td-lake"].id == "tesouro-direto"
    assert "b3" not in reg  # sem info declarada = desconhecido


def test_dry_run_resume_restritas_e_desconhecidas(
    tmp_path: Path,
    registro: dict[str, FetcherInfo],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lake = tmp_path / "lake"
    _dataset(lake, "b3", "cotacoes")
    _dataset(lake, "td", "precos")
    _dataset(lake, "misterio", "tabela")
    monkeypatch.setattr(
        "quantilica.cli.catalog.load_registry", lambda *a, **k: registro
    )
    res = runner.invoke(app, ["catalog", "sync", "--lake-dir", str(lake), "--dry-run"])
    assert res.exit_code == 0, res.output
    assert "Modo --dry-run ativo" in res.output
    assert "Fora do payload (restrita): b3 (1)" in res.output
    assert "Sem registro (metadados nulos): misterio" in res.output
    assert "tesouro-direto/precos" in res.output
