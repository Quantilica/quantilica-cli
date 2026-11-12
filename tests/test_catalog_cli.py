"""Testes do comando `quantilica catalog sync` (Subplano 3 — T3.2)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from quantilica.cli.cli import app

runner = CliRunner()


@pytest.fixture(autouse=True)
def _registro_vazio(monkeypatch: pytest.MonkeyPatch) -> None:
    """Os testes não dependem dos fetchers instalados."""
    from quantilica.core.registry import FetcherInfo

    registro = {
        fid: FetcherInfo(id=fid, nome=fid.upper())
        for fid in ("cvm", "anp", "anac", "outra-fonte")
    }
    monkeypatch.setattr(
        "quantilica.cli.catalog.load_registry", lambda *a, **k: registro
    )


def test_catalog_help() -> None:
    res = runner.invoke(app, ["catalog", "--help"])
    assert res.exit_code == 0
    assert "sync" in res.output


def test_catalog_sync_help() -> None:
    res = runner.invoke(app, ["catalog", "sync", "--help"])
    assert res.exit_code == 0
    assert "--lake-dir" in res.output
    assert "--portal-url" in res.output
    assert "--api-key" in res.output
    assert "--dry-run" in res.output


def test_catalog_sync_empty_dir_dry_run(tmp_path: Path) -> None:
    res = runner.invoke(
        app, ["catalog", "sync", "--lake-dir", str(tmp_path), "--dry-run"]
    )
    assert res.exit_code == 0
    assert "Fontes Oficiais" in res.output
    assert "Modo --dry-run ativo" in res.output


def test_catalog_sync_extract_and_dry_run(tmp_path: Path) -> None:
    # Cria uma estrutura simulada no Lake: cvm/fundos/_dataset.json
    dataset_dir = tmp_path / "cvm" / "fundos"
    dataset_dir.mkdir(parents=True)
    descriptor = {
        "source_id": "cvm",
        "dataset_id": "fundos",
        "alias": "fundos-investimento",
        "partition_keys": ["ano", "mes"],
        "partition_types": {"ano": "Int64", "mes": "Int64"},
    }
    (dataset_dir / "_dataset.json").write_text(json.dumps(descriptor), encoding="utf-8")

    # Executa com --dry-run
    res = runner.invoke(
        app, ["catalog", "sync", "--lake-dir", str(tmp_path), "--dry-run"]
    )
    assert res.exit_code == 0
    assert "Modo --dry-run ativo" in res.output
    assert "cvm/fundos" in res.output
    assert "Chave de partição Hive (ano)" in res.output


def test_catalog_sync_dispatch_success(tmp_path: Path) -> None:
    dataset_dir = tmp_path / "anp" / "precos"
    dataset_dir.mkdir(parents=True)
    descriptor = {
        "source_id": "anp",
        "dataset_id": "precos",
        "alias": "precos-combustiveis",
        "partition_keys": ["ano"],
        "partition_types": {"ano": "Int64"},
    }
    (dataset_dir / "_dataset.json").write_text(json.dumps(descriptor), encoding="utf-8")

    # Mock da resposta HTTP
    fake_resp = {
        "status": "success",
        "message": "Catálogo sincronizado com sucesso.",
        "counts": {"sources": 1, "datasets": 1, "columns": 1, "samples": 0},
    }

    with patch(
        "quantilica.cli.catalog.send_catalog_payload", return_value=fake_resp
    ) as mock_send:
        res = runner.invoke(
            app,
            [
                "catalog",
                "sync",
                "--lake-dir",
                str(tmp_path),
                "--portal-url",
                "https://portal.test",
                "--api-key",
                "secret-key",
            ],
        )
        assert res.exit_code == 0
        assert "sincronizado com sucesso" in res.output
        mock_send.assert_called_once()
        args, kwargs = mock_send.call_args
        assert args[0] == "https://portal.test"
        assert kwargs["api_key"] == "secret-key"
        payload = args[1]
        assert len(payload["sources"]) == 1
        assert payload["sources"][0]["id"] == "anp"
        assert len(payload["datasets"]) == 1
        assert payload["datasets"][0]["id"] == "anp/precos"


def test_catalog_sync_parquet_with_decimals_and_binary(tmp_path: Path) -> None:
    """Verifica extração e serialização JSON segura com Decimal, bytes e data."""
    import decimal

    import pyarrow as pa
    import pyarrow.parquet as pq

    dataset_dir = tmp_path / "cvm" / "fundos_invest"
    dataset_dir.mkdir(parents=True)
    descriptor = {
        "source_id": "cvm",
        "dataset_id": "fundos_invest",
        "alias": "fundos-invest",
        "partition_keys": ["ano"],
        "partition_types": {"ano": "Int64"},
    }
    (dataset_dir / "_dataset.json").write_text(json.dumps(descriptor), encoding="utf-8")

    schema = pa.schema(
        [
            ("ano", pa.int64()),
            ("cnpj", pa.string()),
            ("vl_quota", pa.decimal128(18, 4)),
            ("hash_doc", pa.binary(4)),
        ]
    )
    tbl = pa.Table.from_arrays(
        [
            pa.array([2026]),
            pa.array(["00.000.000/0001-00"]),
            pa.array([decimal.Decimal("123.4567")], type=pa.decimal128(18, 4)),
            pa.array([b"\xab\xcd\xef\x01"], type=pa.binary(4)),
        ],
        schema=schema,
    )
    pq.write_table(tbl, dataset_dir / "part.parquet")

    res = runner.invoke(
        app,
        [
            "catalog",
            "sync",
            "--lake-dir",
            str(tmp_path),
            "--sample-size",
            "20",
            "--dry-run",
        ],
    )
    assert res.exit_code == 0
    assert "cvm/fundos_invest" in res.output
    assert "123.4567" in res.output or "123.45" in res.output
    assert "abcdef01" in res.output


def test_catalog_sync_empty_parquet(tmp_path: Path) -> None:
    """Garante que parquet vazio (0 row groups) não cause exceção."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    dataset_dir = tmp_path / "anac" / "aeronaves"
    dataset_dir.mkdir(parents=True)
    descriptor = {
        "source_id": "anac",
        "dataset_id": "aeronaves",
        "alias": "aeronaves",
    }
    (dataset_dir / "_dataset.json").write_text(json.dumps(descriptor), encoding="utf-8")

    schema = pa.schema([("prefixo", pa.string())])
    with pq.ParquetWriter(dataset_dir / "empty.parquet", schema):
        pass

    res = runner.invoke(
        app, ["catalog", "sync", "--lake-dir", str(tmp_path), "--dry-run"]
    )
    assert res.exit_code == 0
    assert "anac/aeronaves" in res.output


def _fake_urlopen(chamadas: list[str], *, erro_http: int | None = None):
    import io
    import urllib.error

    def urlopen(req, timeout=None):
        chamadas.append(req.full_url)
        if erro_http is not None:
            raise urllib.error.HTTPError(
                req.full_url, erro_http, "erro", {}, io.BytesIO(b'{"detail": "x"}')
            )
        resp = io.BytesIO(b'{"status": "success"}')
        resp.__enter__ = lambda self=resp: self  # type: ignore[method-assign]
        resp.__exit__ = lambda *a: None  # type: ignore[method-assign]
        return resp

    return urlopen


def test_send_catalog_payload_usa_so_o_caminho_administrativo() -> None:
    from quantilica.cli.catalog import send_catalog_payload

    chamadas: list[str] = []
    with patch("urllib.request.urlopen", _fake_urlopen(chamadas)):
        out = send_catalog_payload("https://portal.test/", {"sources": []}, "k")
    assert out == {"status": "success"}
    assert chamadas == ["https://portal.test/api/v2/admin/catalog/sync"]


def test_send_catalog_payload_404_nao_tenta_outro_caminho() -> None:
    import pytest

    from quantilica.cli.catalog import send_catalog_payload

    chamadas: list[str] = []
    with patch("urllib.request.urlopen", _fake_urlopen(chamadas, erro_http=404)):
        with pytest.raises(RuntimeError, match="HTTP 404"):
            send_catalog_payload("https://portal.test", {"sources": []}, "k")
    assert chamadas == ["https://portal.test/api/v2/admin/catalog/sync"]


def test_send_catalog_payload_erro_de_conexao_vira_runtime_error() -> None:
    import pytest

    from quantilica.cli.catalog import send_catalog_payload

    def falha(req, timeout=None):
        raise OSError("sem rede")

    with patch("urllib.request.urlopen", falha):
        with pytest.raises(RuntimeError, match="Erro de conexão"):
            send_catalog_payload("https://portal.test", {"sources": []})


def test_catalog_sync_recusa_envio_com_registro_vazio(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("quantilica.cli.catalog.load_registry", lambda *a, **k: {})
    with patch("quantilica.cli.catalog.send_catalog_payload") as mock_send:
        res = runner.invoke(
            app,
            [
                "catalog",
                "sync",
                "--lake-dir",
                str(tmp_path),
                "--portal-url",
                "https://portal.test",
                "--api-key",
                "secret-key",
            ],
        )
    assert res.exit_code == 1
    assert "Registro de fetchers vazio" in " ".join(res.output.split())
    mock_send.assert_not_called()


def _dataset_com_linha(tmp_path: Path) -> None:
    import pyarrow as pa
    import pyarrow.parquet as pq

    dataset_dir = tmp_path / "cvm" / "cadastro"
    dataset_dir.mkdir(parents=True)
    descriptor = {"source_id": "cvm", "dataset_id": "cadastro", "alias": "cadastro"}
    (dataset_dir / "_dataset.json").write_text(json.dumps(descriptor), encoding="utf-8")
    pq.write_table(pa.table({"nome": ["Fulano de Tal"]}), dataset_dir / "part.parquet")


def test_catalog_sync_amostra_por_padrao_sai_mascarada(tmp_path: Path) -> None:
    """Amostra é linha crua: sai por padrão, mas com o dado pessoal mascarado."""
    _dataset_com_linha(tmp_path)
    res = runner.invoke(
        app, ["catalog", "sync", "--lake-dir", str(tmp_path), "--dry-run"]
    )
    assert res.exit_code == 0
    assert "cvm/cadastro" in res.output
    assert "Fulano de Tal" not in res.output
    assert "•••" in res.output


def test_catalog_sync_sample_size_zero_desliga_amostras(tmp_path: Path) -> None:
    _dataset_com_linha(tmp_path)
    res = runner.invoke(
        app,
        [
            "catalog",
            "sync",
            "--lake-dir",
            str(tmp_path),
            "--dry-run",
            "--sample-size",
            "0",
        ],
    )
    assert res.exit_code == 0
    assert "•••" not in res.output


def test_catalog_sync_sem_pyarrow_recusa(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sem pyarrow o payload sairia sem colunas e com zero linhas."""
    import sys

    _dataset_com_linha(tmp_path)
    monkeypatch.setitem(sys.modules, "pyarrow.parquet", None)
    res = runner.invoke(
        app, ["catalog", "sync", "--lake-dir", str(tmp_path), "--dry-run"]
    )
    assert res.exit_code == 1
    assert "pyarrow" in res.output
