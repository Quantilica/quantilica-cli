"""O conversor só vê a versão vigente de cada arquivo bruto."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from quantilica.cli.vigente import superseded_versions, vigente_view
from tests.test_sdk import _grouped, make_app

runner = CliRunner()


@pytest.fixture
def scratch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "scratch"
    monkeypatch.setenv("QUANTILICA_LAKE_SCRATCH", str(path))
    return path


def _raw(tmp_path: Path, names: list[str]) -> Path:
    raw = tmp_path / "raw"
    folder = raw / "dados"
    folder.mkdir(parents=True)
    for name in names:
        (folder / name).write_text(name, encoding="utf-8")
    return raw


def _files(root: Path) -> list[str]:
    return sorted(
        p.relative_to(root).as_posix() for p in root.rglob("*") if not p.is_dir()
    )


TWO_VERSIONS = ["a_2025@20260101.csv", "a_2025@20261021.csv", "b_2025@20261021.csv"]


def test_superseded_versions_lists_only_older_files(tmp_path: Path) -> None:
    raw = _raw(tmp_path, [*TWO_VERSIONS, "a_2025@20260101.csv.manifest.json"])
    (raw / "dados" / "x.csv.tmp").write_text("t", encoding="utf-8")
    assert superseded_versions(raw) == ["dados/a_2025@20260101.csv"]


def test_format_twins_are_not_superseded(tmp_path: Path) -> None:
    raw = _raw(tmp_path, ["a@20261021.csv", "a@20261021.zip"])
    assert superseded_versions(raw) == []


def test_single_version_passes_the_original_folder(tmp_path: Path, scratch: Path):
    raw = _raw(tmp_path, ["a@20261021.csv", "b@20261021.csv"])
    with vigente_view(raw) as (source, ignored):
        assert source == raw
        assert ignored == 0
    assert not scratch.exists() or not any(scratch.iterdir())


def test_view_has_only_the_current_version_and_is_removed(
    tmp_path: Path, scratch: Path
) -> None:
    raw = _raw(tmp_path, TWO_VERSIONS)
    with vigente_view(raw) as (source, ignored):
        assert source != raw
        assert source.name == raw.name
        assert source.parent.parent == scratch
        assert ignored == 1
        assert _files(source) == [
            "dados/a_2025@20261021.csv",
            "dados/b_2025@20261021.csv",
        ]
        # com links: o dado não foi copiado
        link = source / "dados" / "a_2025@20261021.csv"
        assert link.is_symlink() or os.path.samefile(
            link, raw / "dados" / "a_2025@20261021.csv"
        )
    assert list(scratch.iterdir()) == []
    assert _files(raw) == sorted(f"dados/{n}" for n in TWO_VERSIONS)  # bruto intacto


def test_view_is_removed_when_the_block_raises(tmp_path: Path, scratch: Path) -> None:
    raw = _raw(tmp_path, TWO_VERSIONS)
    with pytest.raises(RuntimeError, match="boom"):
        with vigente_view(raw):
            raise RuntimeError("boom")
    assert list(scratch.iterdir()) == []


def test_scratch_falls_back_to_tmpdir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("QUANTILICA_LAKE_SCRATCH", raising=False)
    tmpdir = tmp_path / "tmpdir"
    tmpdir.mkdir()
    monkeypatch.setenv("TMPDIR", str(tmpdir))
    import tempfile

    monkeypatch.setattr(tempfile, "tempdir", None)
    raw = _raw(tmp_path, TWO_VERSIONS)
    with vigente_view(raw) as (source, _):
        assert tmpdir in source.parents
    assert list(tmpdir.iterdir()) == []


def test_missing_folder_is_passed_through(tmp_path: Path) -> None:
    with vigente_view(tmp_path / "nao-existe") as (source, ignored):
        assert source == tmp_path / "nao-existe"
        assert ignored == 0


# --- pelos comandos do SDK -----------------------------------------------------


def _convert_app(seen: list[dict]):
    app = make_app()

    @app.command_convert
    def conv(input: Path, lake: Path) -> None:
        seen.append({"input": input, "files": _files(input)})

    return app


def test_convert_command_gives_the_converter_the_current_view(
    tmp_path: Path, scratch: Path
) -> None:
    raw = _raw(tmp_path, TWO_VERSIONS)
    seen: list[dict] = []
    app = _convert_app(seen)
    args = ["fd", "convert", "-i", str(raw), "--lake", str(tmp_path / "lake")]
    first = runner.invoke(_grouped(app.app), args)
    second = runner.invoke(_grouped(app.app), args)
    assert first.exit_code == 0, first.output
    assert second.exit_code == 0, second.output
    expected = ["dados/a_2025@20261021.csv", "dados/b_2025@20261021.csv"]
    assert [s["files"] for s in seen] == [expected, expected]
    assert all(s["input"] != raw for s in seen)
    assert list(scratch.iterdir()) == []


def test_convert_command_without_old_versions_keeps_the_folder(
    tmp_path: Path, scratch: Path
) -> None:
    raw = _raw(tmp_path, ["a@20261021.csv"])
    seen: list[dict] = []
    app = _convert_app(seen)
    result = runner.invoke(
        _grouped(app.app),
        ["fd", "convert", "-i", str(raw), "--lake", str(tmp_path / "lake")],
    )
    assert result.exit_code == 0, result.output
    assert seen[0]["input"] == raw


def test_convert_error_cleans_the_view(tmp_path: Path, scratch: Path) -> None:
    raw = _raw(tmp_path, TWO_VERSIONS)
    app = make_app()
    where: list[Path] = []

    @app.command_convert
    def conv(input: Path, lake: Path) -> None:
        where.append(input)
        raise ValueError("falhou")

    result = runner.invoke(
        _grouped(app.app),
        ["fd", "convert", "-i", str(raw), "--lake", str(tmp_path / "lake")],
    )
    assert result.exit_code != 0
    assert where and not where[0].exists()
    assert list(scratch.iterdir()) == []


def test_convert_unsupported_flag_does_not_leave_a_view(
    tmp_path: Path, scratch: Path
) -> None:
    raw = _raw(tmp_path, TWO_VERSIONS)
    app = _convert_app([])
    result = runner.invoke(
        _grouped(app.app),
        ["fd", "convert", "-i", str(raw), "--force", "--lake", str(tmp_path / "l")],
    )
    assert result.exit_code == 2
    assert not scratch.exists() or list(scratch.iterdir()) == []


def test_pipeline_converts_the_current_view(
    tmp_path: Path, scratch: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = _raw(tmp_path, TWO_VERSIONS)
    app = make_app()
    seen: list[list[str]] = []
    monkeypatch.setattr(
        app, "download_datasets", lambda entries, out, workers=4: (1, 1, [])
    )

    @app.command_pipeline
    def conv(data_dir: Path, parquet_dir: Path) -> None:
        seen.append(_files(data_dir))

    result = runner.invoke(
        _grouped(app.app),
        ["fd", "pipeline", "exp", "-o", str(raw), "--parquet-dir", str(tmp_path / "p")],
    )
    assert result.exit_code == 0, result.output
    assert seen == [["dados/a_2025@20261021.csv", "dados/b_2025@20261021.csv"]]
    assert list(scratch.iterdir()) == []
