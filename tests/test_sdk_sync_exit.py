"""Código de saída do ``sync`` (e do ``pipeline``) quando há erros de download."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from quantilica.cli.sdk import EXIT_SYNC_PARTIAL
from tests.test_sdk import _grouped, make_app

runner = CliRunner()


def _fake(downloaded: int, total: int, errors: list[tuple[str, str]]):
    def download(entries, output_dir, workers=4):
        return downloaded, total, errors

    return download


def test_exit_sync_partial_is_distinct_from_failure_and_signals() -> None:
    assert EXIT_SYNC_PARTIAL not in (0, 1, 2, 75, 78, 124, 130, 143)


def test_sync_sem_erros_sai_com_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = make_app()
    monkeypatch.setattr(app, "download_datasets", _fake(2, 2, []))
    result = runner.invoke(_grouped(app.app), ["fd", "sync", "-o", str(tmp_path)])
    assert result.exit_code == 0, result.output


def test_sync_com_erros_e_algum_download_sai_parcial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = make_app()
    monkeypatch.setattr(app, "download_datasets", _fake(1, 2, [("exp", "boom")]))
    result = runner.invoke(_grouped(app.app), ["fd", "sync", "-o", str(tmp_path)])
    assert result.exit_code == EXIT_SYNC_PARTIAL, result.output
    assert "1 erro(s)" in result.output
    assert "exp: boom" in result.output


def test_sync_com_erros_e_nada_baixado_sai_com_um(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = make_app()
    monkeypatch.setattr(app, "download_datasets", _fake(0, 1, [("exp", "boom")]))
    result = runner.invoke(_grouped(app.app), ["fd", "sync", "-o", str(tmp_path)])
    assert result.exit_code == 1, result.output


def test_pipeline_parcial_converte_e_devolve_codigo_parcial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = make_app()
    monkeypatch.setattr(app, "download_datasets", _fake(1, 2, [("exp", "boom")]))
    converted: list[Path] = []

    @app.command_pipeline
    def conv(data_dir: Path, parquet_dir: Path) -> None:
        converted.append(parquet_dir)

    raw, lake = tmp_path / "raw", tmp_path / "lake"
    result = runner.invoke(
        _grouped(app.app),
        ["fd", "pipeline", "-o", str(raw), "--parquet-dir", str(lake)],
    )
    assert result.exit_code == EXIT_SYNC_PARTIAL, result.output
    assert converted == [lake]


def test_pipeline_falha_total_nao_converte(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = make_app()
    monkeypatch.setattr(app, "download_datasets", _fake(0, 1, [("exp", "boom")]))
    converted: list[Path] = []

    @app.command_pipeline
    def conv(data_dir: Path, parquet_dir: Path) -> None:
        converted.append(parquet_dir)

    result = runner.invoke(
        _grouped(app.app),
        [
            "fd",
            "pipeline",
            "-o",
            str(tmp_path / "raw"),
            "--parquet-dir",
            str(tmp_path / "lake"),
        ],
    )
    assert result.exit_code == 1, result.output
    assert converted == []


def _spy_workers(app) -> list[int]:
    seen: list[int] = []

    def download(entries, output_dir, workers=4):
        seen.append(workers)
        return len(entries), len(entries), []

    app.download_datasets = download  # type: ignore[method-assign]
    return seen


def test_sync_workers_vem_da_variavel_de_ambiente(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = make_app()
    seen = _spy_workers(app)
    monkeypatch.setenv("QUANTILICA_SYNC_WORKERS", "1")
    root = _grouped(app.app)
    assert runner.invoke(root, ["fd", "sync", "-o", str(tmp_path)]).exit_code == 0
    # a flag explícita vence a variável
    runner.invoke(root, ["fd", "sync", "--workers", "3", "-o", str(tmp_path)])
    assert seen == [1, 3]


def test_sync_workers_padrao_sem_variavel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = make_app()
    seen = _spy_workers(app)
    monkeypatch.delenv("QUANTILICA_SYNC_WORKERS", raising=False)
    runner.invoke(_grouped(app.app), ["fd", "sync", "-o", str(tmp_path)])
    assert seen == [4]


def test_pipeline_workers_vem_da_variavel_de_ambiente(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = make_app()
    seen = _spy_workers(app)

    @app.command_pipeline
    def conv(data_dir: Path, parquet_dir: Path) -> None:
        pass

    monkeypatch.setenv("QUANTILICA_SYNC_WORKERS", "2")
    result = runner.invoke(
        _grouped(app.app),
        [
            "fd",
            "pipeline",
            "-o",
            str(tmp_path / "r"),
            "--parquet-dir",
            str(tmp_path / "l"),
        ],
    )
    assert result.exit_code == 0, result.output
    assert seen == [2]
