"""`quantilica lake select` com fontes de seleção própria (``selects_versions``).

A pasta delas na visão é sincronizada de forma incremental: nunca apagada para
ser recriada. ``--exclude`` vale para elas, e excluir a versão mais nova de um
recurso tira as anteriores do mesmo recurso.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from quantilica.cli.cli import app

runner = CliRunner()
_ANSI = re.compile(r"\x1b\[[0-9;]*m")

RES_A = "11111111-1111-1111-1111-111111111111"
RES_B = "22222222-2222-2222-2222-222222222222"


def _plain(text: str) -> str:
    return _ANSI.sub("", text)


class _SelfSelecting:
    selects_versions = True


@pytest.fixture
def antt_selects_versions(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("quantilica.core.lake")

    def load(source_id: str):
        return _SelfSelecting() if source_id == "antt" else None

    monkeypatch.setattr("quantilica.core.lake.load_lake_converter", load)


def _write(path: Path, url: str | None) -> str:
    """Cria o arquivo bruto (com manifesto, se há URL); devolve o sha256."""
    from quantilica.core.manifests import DownloadManifest, write_manifest_sidecar

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"valor\n{path.name}\n", encoding="utf-8")
    manifest = DownloadManifest.from_file(
        source_id=path.parent.parent.name,
        dataset_id=path.parent.name,
        url=url or "https://example.invalid/x",
        file_path=path,
    )
    write_manifest_sidecar(path, manifest)
    return manifest.sha256


def _url(resource: str) -> str:
    return f"https://dados.example.invalid/dataset/x/resource/{resource}/download/a.csv"


@pytest.fixture
def raw(tmp_path: Path) -> Path:
    """antt: dois recursos (A com 2 versões, B com 1); beta: uma fonte comum."""
    pytest.importorskip("quantilica.core.lake")
    root = tmp_path / "raw"
    base = root / "antt" / "dados"
    _write(base / "trafego-aaaaaaaa@20260101.csv", _url(RES_A))
    _write(base / "trafego-aaaaaaaa@20261021.csv", _url(RES_A))
    _write(base / "frota-bbbbbbbb@20260301.csv", _url(RES_B))
    _write(root / "beta" / "precos" / "precos_202501@20261021.csv", None)
    return root


def _select(raw: Path, out: Path, *extra: str):
    return runner.invoke(
        app, ["lake", "select", "--raw", str(raw), "--out", str(out), *extra]
    )


def _tree(folder: Path) -> list[str]:
    return sorted(
        p.relative_to(folder).as_posix() for p in folder.rglob("*") if p.is_file()
    )


def test_own_source_folder_is_never_recreated(
    tmp_path: Path,
    raw: Path,
    antt_selects_versions: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import quantilica.core.lake as core_lake

    calls: list[Path] = []
    real = core_lake.materialize_view

    def spy(src, dst, **kwargs):
        calls.append(Path(src))
        return real(src, dst, **kwargs)

    monkeypatch.setattr(core_lake, "materialize_view", spy)
    out = tmp_path / "out"
    first = _select(raw, out)
    assert first.exit_code == 0, first.output
    # a seleção própria não passa por materialize_view; as comuns passam, por pasta
    assert [p.name for p in calls] == ["beta"]
    assert len(_tree(out / "antt")) == 6  # 3 dados + 3 manifestos
    snapshot = {rel: (out / "antt" / rel).stat() for rel in _tree(out / "antt")}

    second = _select(raw, out)
    assert second.exit_code == 0, second.output
    text = _plain(second.output)
    assert "removidos:  0" in text
    assert "criados:    hardlink=0 symlink=0 copy=0" in text
    for rel, before in snapshot.items():
        after = (out / "antt" / rel).stat()
        assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)


def test_own_source_in_copy_mode_does_not_copy_again(
    tmp_path: Path, raw: Path, antt_selects_versions: None
) -> None:
    out = tmp_path / "out"
    first = _select(raw, out, "--mode", "copy")
    assert first.exit_code == 0, first.output
    assert "copy=8" in _plain(first.output)  # 6 da antt + 2 da beta
    again = _select(raw, out, "--mode", "copy")
    text = _plain(again.output)
    assert "copy=0" in text
    assert "removidos:  0" in text


def test_own_source_removes_only_what_vanished(
    tmp_path: Path, raw: Path, antt_selects_versions: None
) -> None:
    out = tmp_path / "out"
    assert _select(raw, out).exit_code == 0
    gone = raw / "antt" / "dados" / "frota-bbbbbbbb@20260301.csv"
    gone.unlink()
    (gone.parent / (gone.name + ".manifest.json")).unlink()
    kept = out / "antt" / "dados" / "trafego-aaaaaaaa@20260101.csv"
    inode = kept.stat().st_ino
    result = _select(raw, out)
    assert result.exit_code == 0, result.output
    assert "removidos:  2" in _plain(result.output)
    assert not (out / "antt" / "dados" / gone.name).exists()
    assert kept.stat().st_ino == inode
    assert len(_tree(out / "antt")) == 4


def test_source_gone_from_raw_leaves_the_view(
    tmp_path: Path, raw: Path, antt_selects_versions: None
) -> None:
    out = tmp_path / "out"
    assert _select(raw, out).exit_code == 0
    (out / "velha" / "x").mkdir(parents=True)
    (out / "velha" / "x" / "a@20260101.csv").write_text("x", encoding="utf-8")
    assert _select(raw, out).exit_code == 0
    assert not (out / "velha").exists()
    assert (out / "antt").is_dir() and (out / "beta").is_dir()


def test_exclude_newest_of_a_resource_drops_the_earlier_ones(
    tmp_path: Path, raw: Path, antt_selects_versions: None
) -> None:
    out = tmp_path / "out"
    assert _select(raw, out).exit_code == 0
    excl = tmp_path / "excl.txt"
    excl.write_text("antt/dados/trafego-aaaaaaaa@20261021.csv\n", encoding="utf-8")
    result = _select(raw, out, "--exclude", str(excl))
    assert result.exit_code == 0, result.output
    text = _plain(result.output)
    assert "Aviso" in text and "trafego-aaaaaaaa@20260101.csv" in text
    assert "excluídos:  1" in text
    assert "1 versão(ões) anterior(es) retirada(s)" in text
    # o recurso A some inteiro; o B fica
    assert _tree(out / "antt") == [
        "dados/frota-bbbbbbbb@20260301.csv",
        "dados/frota-bbbbbbbb@20260301.csv.manifest.json",
    ]
    # sem a exclusão, tudo volta
    assert _select(raw, out).exit_code == 0
    assert len(_tree(out / "antt")) == 6


def test_exclude_by_sha_of_the_newest_version(
    tmp_path: Path, raw: Path, antt_selects_versions: None
) -> None:
    from quantilica.core.manifests import read_manifest_sidecar

    newest = raw / "antt" / "dados" / "trafego-aaaaaaaa@20261021.csv"
    sha = read_manifest_sidecar(newest).sha256
    excl = tmp_path / "excl.txt"
    excl.write_text(f"# corrompido\n{sha}\n", encoding="utf-8")
    out = tmp_path / "out"
    result = _select(raw, out, "--exclude", str(excl))
    assert result.exit_code == 0, result.output
    assert _tree(out / "antt") == [
        "dados/frota-bbbbbbbb@20260301.csv",
        "dados/frota-bbbbbbbb@20260301.csv.manifest.json",
    ]


def test_exclude_of_an_older_version_only_removes_that_one(
    tmp_path: Path, raw: Path, antt_selects_versions: None
) -> None:
    out = tmp_path / "out"
    excl = tmp_path / "excl.txt"
    excl.write_text("antt/dados/trafego-aaaaaaaa@20260101.csv\n", encoding="utf-8")
    result = _select(raw, out, "--exclude", str(excl))
    assert result.exit_code == 0, result.output
    assert "Aviso" not in _plain(result.output)
    assert "dados/trafego-aaaaaaaa@20261021.csv" in _tree(out / "antt")
    assert "dados/trafego-aaaaaaaa@20260101.csv" not in _tree(out / "antt")
    assert len(_tree(out / "antt")) == 4


def test_exclude_still_applies_to_common_sources(
    tmp_path: Path, raw: Path, antt_selects_versions: None
) -> None:
    out = tmp_path / "out"
    excl = tmp_path / "excl.txt"
    excl.write_text("beta/precos/precos_202501@20261021.csv\n", encoding="utf-8")
    result = _select(raw, out, "--exclude", str(excl))
    assert result.exit_code == 0, result.output
    assert "excluídos:  1" in _plain(result.output)
    assert not (out / "beta" / "precos" / "precos_202501@20261021.csv").exists()
    assert len(_tree(out / "antt")) == 6


def test_select_still_rejects_out_inside_raw_with_own_sources(
    tmp_path: Path, raw: Path, antt_selects_versions: None
) -> None:
    result = _select(raw, raw / "antt" / "visao")
    assert result.exit_code == 1
    assert "overlap" in _plain(result.output)
    assert not os.path.exists(raw / "antt" / "visao")
