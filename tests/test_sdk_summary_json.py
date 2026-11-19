"""``--summary-json`` do ``sync`` (e do ``pipeline``): contrato do JSON."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from quantilica.cli import summary as summary_mod
from quantilica.cli.sdk import EXIT_SYNC_PARTIAL
from quantilica.cli.summary import SUMMARY_SCHEMA_VERSION
from tests.test_sdk import _grouped, make_app

runner = CliRunner()


def _fake(downloaded: int, total: int, errors: list[tuple[str, str]]):
    def download(entries, output_dir, workers=4):
        return downloaded, total, errors

    return download


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def test_sem_a_opcao_nenhum_arquivo_e_gravado(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = make_app()
    monkeypatch.setattr(app, "download_datasets", _fake(2, 2, []))
    result = runner.invoke(_grouped(app.app), ["fd", "sync", "-o", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert list(tmp_path.iterdir()) == []


def test_sucesso_grava_contadores_e_esquema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = make_app()
    monkeypatch.setattr(app, "download_datasets", _fake(2, 2, []))
    out = tmp_path / "pasta" / "resumo.json"
    result = runner.invoke(
        _grouped(app.app),
        ["fd", "sync", "-o", str(tmp_path / "raw"), "--summary-json", str(out)],
    )
    assert result.exit_code == 0, result.output
    data = _load(out)
    assert data["schema_version"] == SUMMARY_SCHEMA_VERSION == 1
    assert data["tool"] == "quantilica-cli"
    assert data["fetcher"] == "comex-fetcher"
    assert data["command"] == "sync"
    assert data["status"] == "success"
    assert data["exit_code"] == 0
    assert data["dry_run"] is False
    assert (data["total"], data["succeeded"], data["failed"]) == (2, 2, 0)
    assert data["errors"] == [] and data["errors_truncated"] is False
    assert data["error"] is None
    assert data["started_at"].endswith("Z") and data["finished_at"].endswith("Z")
    assert data["duration_seconds"] >= 0
    # download_datasets substituído: sem medição de transferência
    assert data["downloaded"] is None
    assert data["skipped"] is None
    assert data["bytes_downloaded"] is None
    # nenhum temporário sobra ao lado
    assert [p.name for p in out.parent.iterdir()] == ["resumo.json"]


def test_parcial_mantem_exit_e_lista_erros(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = make_app()
    monkeypatch.setattr(app, "download_datasets", _fake(1, 3, [("exp", "boom")] * 2))
    out = tmp_path / "r.json"
    result = runner.invoke(
        _grouped(app.app),
        ["fd", "sync", "-o", str(tmp_path / "raw"), "--summary-json", str(out)],
    )
    assert result.exit_code == EXIT_SYNC_PARTIAL, result.output
    data = _load(out)
    assert data["status"] == "partial"
    assert data["exit_code"] == EXIT_SYNC_PARTIAL
    assert (data["total"], data["succeeded"], data["failed"]) == (3, 1, 2)
    assert data["errors"] == [{"id": "exp", "message": "boom"}] * 2


def test_falha_total_e_status_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = make_app()
    monkeypatch.setattr(app, "download_datasets", _fake(0, 1, [("exp", "boom")]))
    out = tmp_path / "r.json"
    result = runner.invoke(
        _grouped(app.app),
        ["fd", "sync", "-o", str(tmp_path / "raw"), "--summary-json", str(out)],
    )
    assert result.exit_code == 1
    data = _load(out)
    assert data["status"] == "failed" and data["exit_code"] == 1


def test_erros_e_mensagens_sao_limitados(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = make_app()
    errors = [(f"e{i}", "x" * 5000) for i in range(150)]
    monkeypatch.setattr(app, "download_datasets", _fake(1, 151, errors))
    out = tmp_path / "r.json"
    runner.invoke(
        _grouped(app.app),
        ["fd", "sync", "-o", str(tmp_path / "raw"), "--summary-json", str(out)],
    )
    data = _load(out)
    assert data["failed"] == 150  # o contador não é cortado
    assert len(data["errors"]) == summary_mod.MAX_ERRORS
    assert data["errors_truncated"] is True
    assert len(data["errors"][0]["message"]) == summary_mod.MAX_MESSAGE_CHARS


def test_grupo_invalido_grava_falha_com_motivo(tmp_path: Path) -> None:
    app = make_app()
    out = tmp_path / "r.json"
    result = runner.invoke(
        _grouped(app.app),
        ["fd", "sync", "nao-existe", "-o", str(tmp_path), "--summary-json", str(out)],
    )
    assert result.exit_code == 1
    data = _load(out)
    assert data["status"] == "failed" and data["exit_code"] == 1
    assert data["error"]
    assert data["total"] is None and data["succeeded"] is None


def test_plano_invalido_grava_falha_com_motivo(tmp_path: Path) -> None:
    app = make_app()
    plan = tmp_path / "plano.json"
    plan.write_text("isto não é um plano", encoding="utf-8")
    out = tmp_path / "r.json"
    result = runner.invoke(
        _grouped(app.app),
        ["fd", "sync", "--from-plan", str(plan), "--summary-json", str(out)],
    )
    assert result.exit_code == 1
    data = _load(out)
    assert data["status"] == "failed"
    assert "Plano inválido" in data["error"]


def test_dry_run_grava_total_sem_baixar(tmp_path: Path) -> None:
    app = make_app()
    out = tmp_path / "r.json"
    result = runner.invoke(
        _grouped(app.app),
        [
            "fd",
            "sync",
            "exp",
            "--dry-run",
            "-o",
            str(tmp_path),
            "--summary-json",
            str(out),
        ],
    )
    assert result.exit_code == 0, result.output
    data = _load(out)
    assert data["dry_run"] is True and data["status"] == "success"
    assert data["total"] > 0 and data["succeeded"] == 0


def test_excecao_inesperada_grava_falha_e_propaga(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = make_app()

    def boom(entries, output_dir, workers=4):
        raise RuntimeError("quebrou")

    monkeypatch.setattr(app, "download_datasets", boom)
    out = tmp_path / "r.json"
    result = runner.invoke(
        _grouped(app.app),
        ["fd", "sync", "-o", str(tmp_path / "raw"), "--summary-json", str(out)],
    )
    assert result.exit_code == 1
    assert isinstance(result.exception, RuntimeError)
    data = _load(out)
    assert data["status"] == "failed" and "RuntimeError: quebrou" in data["error"]


def test_falha_ao_gravar_nao_muda_o_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = make_app()
    monkeypatch.setattr(app, "download_datasets", _fake(1, 2, [("exp", "boom")]))
    bloqueio = tmp_path / "arquivo"
    bloqueio.write_text("x", encoding="utf-8")
    # a "pasta" do resumo é um arquivo: mkdir falha
    out = bloqueio / "r.json"
    result = runner.invoke(
        _grouped(app.app),
        ["fd", "sync", "-o", str(tmp_path / "raw"), "--summary-json", str(out)],
    )
    assert result.exit_code == EXIT_SYNC_PARTIAL, result.output
    assert "não foi possível gravar o resumo JSON" in result.output


def _entry_downloader(app, behaviors: dict[str, tuple[int, bool] | Exception]):
    """Troca ``download_entry``: (bytes, transferiu) ou exceção por entrada."""

    def download_entry(entry, output_dir, *, progress=None, force=False, **_):
        behavior = behaviors[entry["id"]]
        if isinstance(behavior, Exception):
            raise behavior
        size, transferred = behavior
        if transferred and progress is not None:
            progress(0, 0)
            progress(size // 2, size)
            progress(size, size)
        return Path(output_dir) / entry["id"]

    app.download_entry = download_entry  # type: ignore[method-assign]


def test_download_real_conta_transferidos_pulados_e_bytes(
    tmp_path: Path,
) -> None:
    def three(group: str) -> list[dict[str, Any]]:
        return [
            {
                "group": group,
                "id": f"{group}-{name}",
                "url": f"https://x/{group}/{name}",
            }
            for name in ("novo", "em-dia", "erro")
        ]

    app = make_app(list_datasets=three)
    behaviors: dict[str, tuple[int, bool] | Exception] = {}
    for group in app.groups:
        behaviors[f"{group}-novo"] = (1000, True)
        behaviors[f"{group}-em-dia"] = (0, False)  # em dia: nenhum progresso
        behaviors[f"{group}-erro"] = RuntimeError("HTTP 500")
    _entry_downloader(app, behaviors)
    out = tmp_path / "r.json"
    result = runner.invoke(
        _grouped(app.app),
        [
            "fd",
            "sync",
            "-o",
            str(tmp_path / "raw"),
            "--workers",
            "1",
            "--summary-json",
            str(out),
        ],
    )
    assert result.exit_code == EXIT_SYNC_PARTIAL, result.output
    data = _load(out)
    assert data["total"] == 6
    assert data["failed"] == 2
    assert data["succeeded"] == 4
    assert data["downloaded"] == 2
    assert data["skipped"] == 2
    assert data["bytes_downloaded"] == 2000
    assert {e["message"] for e in data["errors"]} == {"HTTP 500"}
    assert data["status"] == "partial"


def test_interrupcao_grava_contadores_parciais(tmp_path: Path) -> None:
    app = make_app()
    ids = [e["id"] for g in app.groups for e in app.list_datasets(g)]
    calls = {"n": 0}

    def download_entry(entry, output_dir, *, progress=None, force=False, **_):
        calls["n"] += 1
        if calls["n"] == 2:
            raise KeyboardInterrupt
        if progress is not None:
            progress(0, 0)
            progress(10, 10)
        return Path(output_dir) / entry["id"]

    app.download_entry = download_entry  # type: ignore[method-assign]
    out = tmp_path / "r.json"
    result = runner.invoke(
        _grouped(app.app),
        [
            "fd",
            "sync",
            "-o",
            str(tmp_path / "raw"),
            "--workers",
            "1",
            "--summary-json",
            str(out),
        ],
    )
    assert result.exit_code == 130, result.output
    data = _load(out)
    assert data["status"] == "interrupted" and data["exit_code"] == 130
    assert data["total"] == len(ids)


def test_pipeline_repassa_a_opcao_ao_sync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = make_app()
    monkeypatch.setattr(app, "download_datasets", _fake(2, 2, []))
    converted: list[Path] = []

    @app.command_pipeline
    def conv(data_dir: Path, parquet_dir: Path) -> None:
        converted.append(parquet_dir)

    out = tmp_path / "r.json"
    result = runner.invoke(
        _grouped(app.app),
        [
            "fd",
            "pipeline",
            "-o",
            str(tmp_path / "raw"),
            "--parquet-dir",
            str(tmp_path / "lake"),
            "--summary-json",
            str(out),
        ],
    )
    assert result.exit_code == 0, result.output
    assert converted == [tmp_path / "lake"]
    data = _load(out)
    assert data["command"] == "sync" and data["succeeded"] == 2


def test_pipeline_sem_a_opcao_nao_passa_nada_ao_sync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = make_app()
    monkeypatch.setattr(app, "download_datasets", _fake(1, 1, []))

    @app.command_pipeline
    def conv(data_dir: Path, parquet_dir: Path) -> None:
        pass

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
    assert result.exit_code == 0, result.output
    assert not list(tmp_path.glob("**/*.json"))
