"""O conversor só vê a versão vigente de cada arquivo bruto."""

from __future__ import annotations

import json
import os
import socket
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from quantilica.cli import vigente as vigente_mod
from quantilica.cli.vigente import (
    ViewUnavailableError,
    converter_selects_versions,
    remove_orphan_views,
    superseded_versions,
    vigente_view,
)
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
        assert source.parent.parent.parent == scratch
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


def test_converter_that_selects_its_own_versions_gets_the_original_folder(
    tmp_path: Path, scratch: Path
) -> None:
    raw = _raw(tmp_path, TWO_VERSIONS)
    seen: list[dict] = []
    app = make_app()

    @app.command_convert
    def conv(input: Path, lake: Path) -> None:
        seen.append({"input": input, "files": _files(input)})

    conv.selects_versions = True  # type: ignore[attr-defined]
    result = runner.invoke(
        _grouped(app.app),
        ["fd", "convert", "-i", str(raw), "--lake", str(tmp_path / "lake")],
    )
    assert result.exit_code == 0, result.output
    assert seen[0]["input"] == raw
    assert seen[0]["files"] == sorted(f"dados/{n}" for n in TWO_VERSIONS)
    assert not scratch.exists() or list(scratch.iterdir()) == []


def test_pipeline_with_a_self_selecting_converter_gets_the_original_folder(
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

    conv.selects_versions = True  # type: ignore[attr-defined]
    result = runner.invoke(
        _grouped(app.app),
        ["fd", "pipeline", "exp", "-o", str(raw), "--parquet-dir", str(tmp_path / "p")],
    )
    assert result.exit_code == 0, result.output
    assert seen == [sorted(f"dados/{n}" for n in TWO_VERSIONS)]


def test_converter_selects_versions_reads_the_declaration() -> None:
    class Declares:
        selects_versions = True

        def convert(self) -> None: ...

    class Silent:
        def convert(self) -> None: ...

    assert converter_selects_versions(Declares())
    assert converter_selects_versions(Declares().convert)  # método ligado: pelo dono
    assert not converter_selects_versions(Silent(), Silent().convert, None)
    assert not converter_selects_versions(lambda: None)


def test_converter_selects_versions_follows_partial_and_wrapped() -> None:
    import functools

    def declared(input: Path, lake: Path) -> None: ...

    declared.selects_versions = True  # type: ignore[attr-defined]

    def silent(input: Path, lake: Path) -> None: ...

    assert converter_selects_versions(functools.partial(declared, lake=None))
    assert converter_selects_versions(
        functools.partial(functools.partial(declared, 1), 2)
    )
    assert not converter_selects_versions(functools.partial(silent, lake=None))

    @functools.wraps(declared)
    def wrapper(*args: object, **kwargs: object) -> None:
        return declared(*args, **kwargs)  # type: ignore[arg-type]

    del wrapper.selects_versions  # wraps copia __dict__: força o caminho __wrapped__
    assert wrapper.__wrapped__ is declared  # type: ignore[attr-defined]
    assert converter_selects_versions(wrapper)

    @functools.wraps(silent)
    def plain(*args: object, **kwargs: object) -> None:
        return silent(*args, **kwargs)  # type: ignore[arg-type]

    assert not converter_selects_versions(plain)


def test_skip_passes_the_original_folder(tmp_path: Path, scratch: Path) -> None:
    raw = _raw(tmp_path, TWO_VERSIONS)
    with vigente_view(raw, skip=True) as (source, ignored):
        assert (source, ignored) == (raw, 0)


def test_view_uses_the_absolute_name_not_the_symlink_target(
    tmp_path: Path, scratch: Path
) -> None:
    raw = _raw(tmp_path, TWO_VERSIONS)
    alias = tmp_path / "apelido"
    alias.symlink_to(raw, target_is_directory=True)
    with vigente_view(alias) as (source, _):
        assert source.name == "apelido"


def _owned(base: Path, name: str, *, pid: int, host: str | None = None) -> Path:
    folder = base / f".quantilica-vigente-{name}"
    (folder / "visao").mkdir(parents=True)
    owner = {"pid": pid, "hostname": host or socket.gethostname()}
    (folder / "owner.json").write_text(json.dumps(owner))
    return folder


def test_view_records_its_owner(tmp_path: Path, scratch: Path) -> None:
    raw = _raw(tmp_path, TWO_VERSIONS)
    with vigente_view(raw) as (source, _):
        owner = json.loads((source.parent.parent / "owner.json").read_text())
        assert owner == {"pid": os.getpid(), "hostname": socket.gethostname()}


def test_orphan_views_of_dead_processes_are_removed(
    tmp_path: Path, scratch: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scratch.mkdir()
    monkeypatch.setattr(vigente_mod, "_pid_alive", lambda pid: pid == 111)
    dead = _owned(scratch, "morto", pid=222)
    alive = _owned(scratch, "vivo", pid=111)
    other_host = _owned(scratch, "outro-host", pid=222, host="outra-maquina")
    foreign = scratch / "lake-stage-xyz"  # de outro prefixo: nunca é tocado
    foreign.mkdir()
    raw = _raw(tmp_path, TWO_VERSIONS)
    with vigente_view(raw):
        assert not dead.exists()
        assert alive.exists() and other_host.exists() and foreign.exists()
    assert _files(raw) == sorted(f"dados/{n}" for n in TWO_VERSIONS)


def test_orphan_view_without_owner_waits_seven_days(scratch: Path) -> None:
    scratch.mkdir()
    fresh = scratch / ".quantilica-vigente-novo"
    old = scratch / ".quantilica-vigente-velho"
    fresh.mkdir()
    old.mkdir()
    past = time.time() - 8 * 24 * 3600
    os.utime(old, (past, past))
    assert remove_orphan_views(scratch) == 1
    assert fresh.exists() and not old.exists()


def test_orphan_cleanup_never_touches_the_raw_through_links(
    tmp_path: Path, scratch: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = _raw(tmp_path, TWO_VERSIONS)
    dead = _owned(scratch, "morto", pid=222)
    (dead / "visao" / "a.csv").symlink_to(raw / "dados" / "a_2025@20261021.csv")
    monkeypatch.setattr(vigente_mod, "_pid_alive", lambda pid: False)
    assert remove_orphan_views(scratch) == 1
    assert _files(raw) == sorted(f"dados/{n}" for n in TWO_VERSIONS)


def test_refuses_to_copy_when_links_are_not_possible(
    tmp_path: Path, scratch: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = _raw(tmp_path, TWO_VERSIONS)

    def no_link(*_args: object, **_kwargs: object) -> None:
        raise OSError("sem link")

    monkeypatch.setattr(os, "link", no_link)
    monkeypatch.setattr(os, "symlink", no_link)
    with pytest.raises(ViewUnavailableError, match="lake select"):
        with vigente_view(raw):
            raise AssertionError("não deveria montar")
    assert list(scratch.iterdir()) == []


def test_convert_command_refusing_copy_is_a_clear_error(
    tmp_path: Path, scratch: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = _raw(tmp_path, TWO_VERSIONS)

    def no_link(*_args: object, **_kwargs: object) -> None:
        raise OSError("sem link")

    monkeypatch.setattr(os, "link", no_link)
    monkeypatch.setattr(os, "symlink", no_link)
    result = runner.invoke(
        _grouped(_convert_app([]).app),
        ["fd", "convert", "-i", str(raw), "--lake", str(tmp_path / "lake")],
    )
    assert result.exit_code == 1
    assert "lake select" in " ".join(result.output.split())


def test_orphan_cleanup_also_accepts_the_old_prefix_without_dot(
    tmp_path: Path, scratch: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scratch.mkdir()
    monkeypatch.setattr(vigente_mod, "_pid_alive", lambda pid: False)
    legacy = scratch / "quantilica-vigente-antigo"
    (legacy / "visao").mkdir(parents=True)
    (legacy / "owner.json").write_text(
        json.dumps({"pid": 222, "hostname": socket.gethostname()})
    )
    assert remove_orphan_views(scratch) == 1
    assert not legacy.exists()


def test_view_folder_name_starts_with_a_dot(tmp_path: Path, scratch: Path) -> None:
    raw = _raw(tmp_path, TWO_VERSIONS)
    with vigente_view(raw) as (source, _):
        assert source.parent.parent.name.startswith(".quantilica-vigente-")


def test_scratch_inside_the_raw_is_a_clear_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = _raw(tmp_path, TWO_VERSIONS)
    monkeypatch.setenv("QUANTILICA_LAKE_SCRATCH", str(raw / "scratch"))
    with pytest.raises(ViewUnavailableError) as info:
        with vigente_view(raw):
            raise AssertionError("não deveria montar")
    message = " ".join(str(info.value).split())
    assert "mesmo volume do bruto" in message
    assert "fora da pasta do bruto" in message
    assert "para o volume do bruto" not in message
    assert not list((raw / "scratch").iterdir())  # a visão foi removida


def test_convert_command_with_scratch_inside_the_raw_exits_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = _raw(tmp_path, TWO_VERSIONS)
    monkeypatch.setenv("QUANTILICA_LAKE_SCRATCH", str(raw / "scratch"))
    result = runner.invoke(
        _grouped(_convert_app([]).app),
        ["fd", "convert", "-i", str(raw), "--lake", str(tmp_path / "lake")],
    )
    assert result.exit_code == 1
    assert "fora da pasta do bruto" in " ".join(result.output.split())
