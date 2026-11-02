"""Testes do comando `quantilica catalog sync` (Subplano 3 — T3.2)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from quantilica.cli.cli import app

runner = CliRunner()


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
        app, ["catalog", "sync", "--lake-dir", str(tmp_path), "--dry-run"]
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
