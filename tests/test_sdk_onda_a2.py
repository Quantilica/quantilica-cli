"""Testes unitários para Onda A.2 do SDK (command_convert/pipeline/archive, SyncPlan)."""

from __future__ import annotations

from pathlib import Path

import pytest
import typer
from rich.console import Console
from typer.testing import CliRunner

from quantilica.cli.sdk import SyncPlan, SyncPlanItem
from tests.test_sdk import _grouped, make_app

runner = CliRunner()


# ---------------------------------------------------------------
# SyncPlan / SyncPlanItem
# ---------------------------------------------------------------


def _sample_item(
    dataset: str = "exp", partition: str | None = "2023-05"
) -> SyncPlanItem:
    return SyncPlanItem(
        dataset=dataset,
        partition=partition,
        filename=f"{dataset}-monthly_2023-05.csv",
        url=f"https://example.test/{dataset}-monthly.csv",
        target=Path(f"/data/output/{dataset}/{dataset}-monthly_2023-05.csv"),
        dataset_name="Exportações",
    )


def test_sync_plan_item_is_frozen():
    item = _sample_item()
    with pytest.raises(Exception):  # noqa: B017 (FrozenInstanceError levantada dinamicamente)
        item.dataset = "imp"  # type: ignore[misc]


def test_sync_plan_is_frozen():
    plan = SyncPlan(items=[_sample_item()])
    with pytest.raises(Exception):  # noqa: B017 (FrozenInstanceError levantada dinamicamente)
        plan.skipped = 5  # type: ignore[misc]


def test_sync_plan_item_defaults():
    item = _sample_item()
    assert item.dataset_name == "Exportações"
    plain = SyncPlanItem(
        dataset="d", partition=None, filename="f", url="u", target=Path("t")
    )
    assert plain.dataset_name == ""


def test_sync_plan_defaults():
    plan = SyncPlan()
    assert plan.items == []
    assert plan.skipped == 0


def test_render_table_prints_columns_and_rows(capsys: pytest.CaptureFixture[str]):
    plan = SyncPlan(
        items=[_sample_item("exp", "2023-05"), _sample_item("imp", None)], skipped=3
    )
    console = Console(record=True, width=120, legacy_windows=False)
    plan.render_table(console=console)
    output = console.export_text()
    assert "exp" in output
    assert "exp-monthly_2023-05.csv" in output
    assert "https://example.test/exp-monthly.csv" in output
    # partição ausente é exibida como "—"
    assert "—" in output
    assert "Total: 2 arquivos planejados. 3 ignorados fora de cobertura." in output


def test_render_table_uses_shared_console_when_none(capsys: pytest.CaptureFixture[str]):
    plan = SyncPlan(items=[_sample_item()], skipped=1)
    plan.render_table()
    output = capsys.readouterr().out
    assert "Total: 1 arquivos planejados. 1 ignorados fora de cobertura." in output


# ---------------------------------------------------------------
# command_convert
# ---------------------------------------------------------------


def test_command_convert_success(tmp_path: Path):
    app = make_app()
    calls: list[tuple[Path, Path]] = []

    @app.command_convert
    def conv(input: Path, output: Path) -> None:
        calls.append((input, output))

    result = runner.invoke(
        _grouped(app.app), ["fd", "convert", "-i", str(tmp_path), "-o", str(tmp_path)]
    )
    assert result.exit_code == 0, result.output
    assert calls == [(tmp_path, tmp_path)]
    assert "Conversão concluída" in result.output
    assert "✓" in result.output


def test_command_convert_defaults_to_default_output(tmp_path: Path):
    app = make_app()
    calls: list[tuple[Path, Path]] = []

    @app.command_convert
    def conv(input: Path, output: Path) -> None:
        calls.append((input, output))

    default = app.default_output
    result = runner.invoke(_grouped(app.app), ["fd", "convert"])
    assert result.exit_code == 0, result.output
    assert calls == [(default, default)]


def test_command_convert_graceful_import_error(tmp_path: Path):
    app = make_app()

    @app.command_convert
    def conv(input: Path, output: Path) -> None:
        raise ImportError("No module named 'polars'")

    result = runner.invoke(_grouped(app.app), ["fd", "convert", "-i", str(tmp_path)])
    assert result.exit_code == 1
    assert "convert requer extras de análise" in result.output
    assert "pip install comex-fetcher[analysis]" in result.output


def test_command_convert_verbose_sets_logging(tmp_path: Path):
    app = make_app()

    @app.command_convert
    def conv(input: Path, output: Path) -> None:
        raise typer.Exit(3)

    result = runner.invoke(_grouped(app.app), ["fd", "convert", "--verbose"])
    assert result.exit_code == 3


# ---------------------------------------------------------------
# command_pipeline
# ---------------------------------------------------------------


def test_command_pipeline_runs_both_steps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    app = make_app()
    calls: list[tuple[Path, Path]] = []
    sync_calls: list[tuple[list, Path, int]] = []

    def fake_download(entries, output_dir, workers=4):
        sync_calls.append((entries, output_dir, workers))
        return len(entries), len(entries), []

    monkeypatch.setattr(app, "download_datasets", fake_download)

    @app.command_pipeline
    def conv(data_dir: Path, parquet_dir: Path) -> None:
        calls.append((data_dir, parquet_dir))

    raw = tmp_path / "raw"
    parquet = tmp_path / "parquet"
    result = runner.invoke(
        _grouped(app.app),
        ["fd", "pipeline", "exp", "-o", str(raw), "--parquet-dir", str(parquet)],
    )
    assert result.exit_code == 0, result.output
    assert "Passo 1/2: Download" in result.output
    assert "Passo 2/2: Conversão" in result.output
    # Passo 1 invocou o sync (download_datasets) uma vez
    assert len(sync_calls) == 1
    entries, out_dir, workers = sync_calls[0]
    assert out_dir == raw
    assert [e["id"] for e in entries] == ["exp-monthly"]
    # Passo 2 invocou a convert_func com (output, parquet_dir)
    assert calls == [(raw, parquet)]
    assert "✓" in result.output


def test_command_pipeline_dry_run_skips_conversion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    app = make_app()
    downloads: list[tuple] = []
    monkeypatch.setattr(
        app,
        "download_datasets",
        lambda entries, output_dir, workers=4: (
            downloads.append(output_dir) or (0, 0, [])
        ),
    )
    converted: list[Path] = []

    @app.command_pipeline
    def conv(data_dir: Path, parquet_dir: Path) -> None:
        converted.append(parquet_dir)

    result = runner.invoke(_grouped(app.app), ["fd", "pipeline", "exp", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "Passo 1/2: Download" in result.output
    assert "Passo 2/2: Conversão" not in result.output
    assert converted == []
    # dry-run: nenhum download executado (download_datasets não foi invocado)
    assert downloads == []


def test_command_pipeline_graceful_import_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    app = make_app()
    monkeypatch.setattr(
        app, "download_datasets", lambda entries, output_dir, workers=4: (0, 0, [])
    )

    @app.command_pipeline
    def conv(data_dir: Path, parquet_dir: Path) -> None:
        raise ImportError("No module named 'polars'")

    result = runner.invoke(
        _grouped(app.app), ["fd", "pipeline", "exp", "-o", str(tmp_path)]
    )
    assert result.exit_code == 1
    assert "pipeline (conversão) requer extras de análise" in result.output
    assert "comex-fetcher[analysis]" in result.output


def test_command_pipeline_parquet_dir_defaults_to_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    app = make_app()
    monkeypatch.setattr(
        app, "download_datasets", lambda entries, output_dir, workers=4: (0, 0, [])
    )
    calls: list[tuple[Path, Path]] = []

    @app.command_pipeline
    def conv(data_dir: Path, parquet_dir: Path) -> None:
        calls.append((data_dir, parquet_dir))

    raw = tmp_path / "raw"
    result = runner.invoke(_grouped(app.app), ["fd", "pipeline", "exp", "-o", str(raw)])
    assert result.exit_code == 0, result.output
    assert calls == [(raw, raw)]


def test_command_pipeline_requires_sync_command():
    app = make_app(build_default_commands=False)

    @app.command_pipeline
    def conv(data_dir: Path, parquet_dir: Path) -> None:
        raise AssertionError("não deveria ser chamado")

    result = runner.invoke(_grouped(app.app), ["fd", "pipeline", "exp"])
    assert result.exit_code == 1
    assert "pipeline requer o comando 'sync'" in result.output


# ---------------------------------------------------------------
# command_archive
# ---------------------------------------------------------------


def test_command_archive_success(tmp_path: Path):
    app = make_app()
    calls: list[tuple[Path, Path]] = []

    @app.command_archive
    def arch(input: Path, output: Path) -> None:
        calls.append((input, output))

    result = runner.invoke(
        _grouped(app.app), ["fd", "archive", "-i", str(tmp_path), "-o", str(tmp_path)]
    )
    assert result.exit_code == 0, result.output
    assert calls == [(tmp_path, tmp_path)]
    assert "Arquivo criado" in result.output


def test_command_archive_graceful_import_error(tmp_path: Path):
    app = make_app()

    @app.command_archive
    def arch(input: Path, output: Path) -> None:
        raise ImportError("No module named 'some_extra'")

    result = runner.invoke(_grouped(app.app), ["fd", "archive", "-o", str(tmp_path)])
    assert result.exit_code == 1
    assert "archive requer extras de análise" in result.output
    assert "pip install comex-fetcher[analysis]" in result.output
