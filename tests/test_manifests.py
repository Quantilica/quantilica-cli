"""Tests for the `quantilica manifests` inspection commands."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from quantilica.core.dates import isoformat_utc, utc_now
from quantilica.core.manifests import DownloadManifest, SourceMetadata
from typer.testing import CliRunner

from quantilica.cli.manifests import app, iter_manifests, parse_since

runner = CliRunner()


def write_manifest(
    root: Path,
    data_name: str,
    *,
    subdir: str = "",
    **overrides: object,
) -> Path:
    """Write a manifest next to a (fictitious) data file and return its path."""
    defaults: dict[str, object] = {
        "source_id": "rtn",
        "dataset_id": "rtn",
        "url": "https://example.test/data",
        "fetched_at": isoformat_utc(),
        "sha256": "abc123",
        "size_bytes": 2048,
    }
    defaults.update(overrides)
    manifest = DownloadManifest(**defaults)  # type: ignore[arg-type]
    target_dir = root / subdir if subdir else root
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / f"{data_name}.manifest.json"
    manifest.write_json(path)
    return path


# --- parse_since -----------------------------------------------------------


def test_parse_since_duration():
    before = utc_now() - timedelta(days=7, seconds=2)
    after = utc_now() - timedelta(days=7) + timedelta(seconds=2)
    parsed = parse_since("7d")
    assert before < parsed < after


def test_parse_since_iso_date():
    parsed = parse_since("2026-05-01")
    assert parsed.year == 2026 and parsed.month == 5 and parsed.day == 1


def test_parse_since_invalid():
    import typer

    with pytest.raises(typer.BadParameter):
        parse_since("not-a-date")


# --- iter_manifests ----------------------------------------------------------


def test_iter_manifests_finds_nested(tmp_path: Path):
    write_manifest(tmp_path, "a.xlsx")
    write_manifest(tmp_path, "b.zip", subdir="2020")
    found = list(iter_manifests(tmp_path))
    assert len(found) == 2


def test_iter_manifests_skips_malformed(tmp_path: Path):
    write_manifest(tmp_path, "good.xlsx")
    (tmp_path / "bad.xlsx.manifest.json").write_text("{not json", encoding="utf-8")
    found = list(iter_manifests(tmp_path))
    assert len(found) == 1
    assert found[0][1]["source_id"] == "rtn"


# --- list --------------------------------------------------------------------


def test_list_empty(tmp_path: Path):
    result = runner.invoke(app, ["list", "-r", str(tmp_path)])
    assert result.exit_code == 0
    assert "Nenhum manifesto" in result.stdout


def test_list_shows_manifests(tmp_path: Path):
    write_manifest(tmp_path, "rtn.xlsx", source_id="tesouro")
    result = runner.invoke(app, ["list", "-r", str(tmp_path)])
    assert result.exit_code == 0
    assert "tesouro" in result.stdout
    assert "1 manifesto" in result.stdout


def test_list_filters_by_source(tmp_path: Path):
    write_manifest(tmp_path, "a.xlsx", source_id="tesouro")
    write_manifest(tmp_path, "b.xlsx", source_id="inmet")
    result = runner.invoke(app, ["list", "-r", str(tmp_path), "--source", "inmet"])
    assert result.exit_code == 0
    assert "inmet" in result.stdout
    assert "tesouro" not in result.stdout


def test_list_filters_by_since(tmp_path: Path):
    old = isoformat_utc(utc_now() - timedelta(days=40))
    write_manifest(tmp_path, "old.xlsx", dataset_id="velho", fetched_at=old)
    write_manifest(tmp_path, "new.xlsx", dataset_id="novo")
    result = runner.invoke(app, ["list", "-r", str(tmp_path), "--since", "7d"])
    assert result.exit_code == 0
    assert "novo" in result.stdout
    assert "velho" not in result.stdout


# --- show --------------------------------------------------------------------


def test_show_by_manifest_path(tmp_path: Path):
    path = write_manifest(tmp_path, "rtn.xlsx", sha256="deadbeef")
    result = runner.invoke(app, ["show", str(path)])
    assert result.exit_code == 0
    assert "deadbeef" in result.stdout


def test_show_by_data_file_path(tmp_path: Path):
    write_manifest(tmp_path, "rtn.xlsx", sha256="deadbeef")
    result = runner.invoke(app, ["show", str(tmp_path / "rtn.xlsx")])
    assert result.exit_code == 0
    assert "deadbeef" in result.stdout


def test_show_missing(tmp_path: Path):
    result = runner.invoke(app, ["show", str(tmp_path / "absent.xlsx")])
    assert result.exit_code == 2
    assert "não encontrado" in result.stdout


# --- status ------------------------------------------------------------------


def test_status_groups_datasets(tmp_path: Path):
    write_manifest(tmp_path, "a.xlsx", dataset_id="ds1")
    write_manifest(tmp_path, "b.xlsx", dataset_id="ds1")
    write_manifest(tmp_path, "c.xlsx", dataset_id="ds2")
    result = runner.invoke(app, ["status", "-r", str(tmp_path)])
    assert result.exit_code == 0
    assert "ds1" in result.stdout
    assert "ds2" in result.stdout


def test_status_flags_stale_by_cadence(tmp_path: Path):
    old = isoformat_utc(utc_now() - timedelta(days=10))
    write_manifest(
        tmp_path,
        "daily.xlsx",
        dataset_id="diario",
        fetched_at=old,
        metadata={"expected_cadence": "daily"},
    )
    result = runner.invoke(app, ["status", "-r", str(tmp_path)])
    assert result.exit_code == 0
    assert "atrasado" in result.stdout


def test_status_flags_stale_by_source_meta_cadence(tmp_path: Path):
    old = isoformat_utc(utc_now() - timedelta(days=10))
    write_manifest(
        tmp_path,
        "daily.xlsx",
        dataset_id="diario",
        fetched_at=old,
        source_meta=SourceMetadata(expected_cadence="daily"),
    )
    result = runner.invoke(app, ["status", "-r", str(tmp_path)])
    assert result.exit_code == 0
    assert "atrasado" in result.stdout
