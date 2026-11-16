"""Marcador de atualização, manifesto do build e estado de publicação (leitores)."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from quantilica.cli.cli import app
from quantilica.cli.lake_estado import (
    ESTADO_ENV,
    MANIFESTO,
    MARCADOR,
    EstadoInvalido,
    PoliticaDePublicacao,
    ler_estado,
)

runner = CliRunner()
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _plain(text: str) -> str:
    # Sem ANSI e com o espaço normalizado: o rich quebra linhas longas (caminhos
    # do Windows) no meio das mensagens.
    return " ".join(_ANSI.sub("", text).split())


FONTES = ("cvm", "anp", "anac")


@pytest.fixture(autouse=True)
def _registro(monkeypatch: pytest.MonkeyPatch) -> None:
    from quantilica.core.registry import FetcherInfo

    registro = {fid: FetcherInfo(id=fid, nome=fid.upper()) for fid in FONTES}
    monkeypatch.setattr(
        "quantilica.cli.catalog.load_registry", lambda *a, **k: registro
    )
    monkeypatch.delenv(ESTADO_ENV, raising=False)


def _lake(root: Path) -> Path:
    """Um lake de descritores (sem Parquet) com as três fontes."""
    for fonte in FONTES:
        pasta = root / fonte / "dados"
        pasta.mkdir(parents=True)
        (pasta / "_dataset.json").write_text(
            json.dumps(
                {
                    "source_id": fonte,
                    "dataset_id": "dados",
                    "alias": f"{fonte}-dados",
                    "partition_keys": [],
                }
            ),
            encoding="utf-8",
        )
    # O que mais mora na raiz do lake e não é fonte.
    (root / "ledger").mkdir()
    (root / "ledger" / "retrato.jsonl.gz").write_bytes(b"x")
    return root


def _manifesto(lake: Path, fonte: str, build_id: str) -> None:
    (lake / fonte / MANIFESTO).write_text(
        json.dumps({"build_id": build_id, "parquets": {}}), encoding="utf-8"
    )


def _marcar(lake: Path, fonte: str, build_id: str = "B2") -> None:
    (lake / fonte / MARCADOR).write_text(
        json.dumps({"build_id": build_id}), encoding="utf-8"
    )


def _estado(path: Path, **fontes: tuple[str | None, str]) -> Path:
    path.write_text(
        json.dumps(
            {
                "versao": 1,
                "fontes": {
                    fonte: {
                        "build_id": build,
                        "resultado": resultado,
                        "auditado_em": "2026-11-16T10:00:00+00:00",
                    }
                    for fonte, (build, resultado) in fontes.items()
                },
            }
        ),
        encoding="utf-8",
    )
    return path


def _sync(lake: Path, *extra: str):
    return runner.invoke(
        app, ["catalog", "sync", "--lake-dir", str(lake), "--dry-run", *extra]
    )


# --- a política --------------------------------------------------------------------


def test_policy_without_state_only_refuses_the_marked_source(tmp_path: Path) -> None:
    lake = _lake(tmp_path / "lake")
    _marcar(lake, "anp")
    _manifesto(lake, "cvm", "B1")  # com manifesto e sem estado: segue como hoje

    politica = PoliticaDePublicacao(lake)

    assert politica.motivo("cvm") is None
    assert politica.motivo("anac") is None  # lake antigo, sem _build.json
    assert "em atualização" in (politica.motivo("anp") or "")


def test_policy_with_state_requires_published_and_same_build(tmp_path: Path) -> None:
    lake = _lake(tmp_path / "lake")
    _manifesto(lake, "cvm", "B1")
    _manifesto(lake, "anp", "B2")
    _manifesto(lake, "anac", "B1")
    estado = ler_estado(
        _estado(
            tmp_path / "e.json",
            cvm=("B1", "publicado"),
            anp=("B1", "publicado"),  # o lake já está no B2
            anac=("B1", "incompleto"),
        )
    )

    politica = PoliticaDePublicacao(lake, estado)

    assert politica.motivo("cvm") is None
    assert "B1" in politica.motivo("anp") and "B2" in politica.motivo("anp")
    assert "incompleto" in politica.motivo("anac")
    assert "sem registro" in politica.motivo("outra")


def test_policy_marker_beats_a_published_state(tmp_path: Path) -> None:
    lake = _lake(tmp_path / "lake")
    _manifesto(lake, "cvm", "B1")
    _marcar(lake, "cvm", "B2")
    estado = ler_estado(_estado(tmp_path / "e.json", cvm=("B1", "publicado")))

    assert "em atualização" in PoliticaDePublicacao(lake, estado).motivo("cvm")


def test_policy_source_without_manifest_needs_a_state_without_build(
    tmp_path: Path,
) -> None:
    lake = _lake(tmp_path / "lake")
    estado = ler_estado(
        _estado(
            tmp_path / "e.json",
            cvm=(None, "publicado"),  # lake antigo, auditado assim
            anp=("B1", "publicado"),  # o estado cita um build que não chegou
        )
    )

    politica = PoliticaDePublicacao(lake, estado)

    assert politica.motivo("cvm") is None
    assert "não há" in politica.motivo("anp")


@pytest.mark.parametrize(
    "conteudo", ["não é json", "[1]", '{"versao": 9, "fontes": {}}']
)
def test_invalid_state_is_an_error(tmp_path: Path, conteudo: str) -> None:
    arquivo = tmp_path / "e.json"
    arquivo.write_text(conteudo, encoding="utf-8")
    with pytest.raises(EstadoInvalido):
        ler_estado(arquivo)
    with pytest.raises(EstadoInvalido, match="não encontrado"):
        ler_estado(tmp_path / "nada.json")


# --- catalog sync ------------------------------------------------------------------


def test_sync_without_state_skips_the_marked_source_with_a_warning(
    tmp_path: Path,
) -> None:
    lake = _lake(tmp_path / "lake")
    _marcar(lake, "anp")

    res = _sync(lake)

    out = _plain(res.output)
    assert res.exit_code == 0, out
    assert "cvm/dados" in out and "anac/dados" in out
    assert "anp/dados" not in out
    assert "fonte anp pulada" in out and "em atualização" in out
    assert "ledger" not in out  # a pasta da raiz que não é fonte não vira aviso


def test_sync_without_state_and_without_marker_reads_everything(
    tmp_path: Path,
) -> None:
    lake = _lake(tmp_path / "lake")
    _manifesto(lake, "cvm", "B1")

    res = _sync(lake)

    out = _plain(res.output)
    assert res.exit_code == 0
    assert all(f"{f}/dados" in out for f in FONTES)
    assert "pulada" not in out


def test_sync_with_state_reads_only_published_sources_of_the_current_build(
    tmp_path: Path,
) -> None:
    lake = _lake(tmp_path / "lake")
    _manifesto(lake, "cvm", "B1")
    _manifesto(lake, "anp", "B2")
    _manifesto(lake, "anac", "B1")
    estado = _estado(
        tmp_path / "e.json",
        cvm=("B1", "publicado"),
        anp=("B1", "publicado"),
        anac=("B1", "em_atualizacao"),
    )

    res = _sync(lake, "--estado", str(estado))

    out = _plain(res.output)
    assert res.exit_code == 0, out
    assert "cvm/dados" in out
    assert "anp/dados" not in out and "anac/dados" not in out
    assert "fonte anp pulada" in out and "auditou o build B1" in out
    assert "fonte anac pulada" in out and "em_atualizacao" in out


def test_sync_state_from_the_environment(tmp_path: Path, monkeypatch) -> None:
    lake = _lake(tmp_path / "lake")
    estado = _estado(tmp_path / "e.json", cvm=(None, "publicado"))
    monkeypatch.setenv(ESTADO_ENV, str(estado))

    out = _plain(_sync(lake).output)

    assert "cvm/dados" in out and "anp/dados" not in out
    assert "fonte anp pulada" in out and "sem registro" in out


def test_sync_with_a_missing_state_file_fails(tmp_path: Path) -> None:
    lake = _lake(tmp_path / "lake")

    res = _sync(lake, "--estado", str(tmp_path / "nada.json"))

    assert res.exit_code == 1
    assert "não encontrado" in _plain(res.output)


def test_sync_does_not_walk_into_a_skipped_source(tmp_path: Path) -> None:
    lake = _lake(tmp_path / "lake")
    _marcar(lake, "anp")
    # Um descritor ilegível dentro da fonte pulada não pode nem ser lido.
    (lake / "anp" / "dados" / "_dataset.json").write_text("{quebrado", "utf-8")

    res = _sync(lake)

    assert res.exit_code == 0, res.output
    assert "Traceback" not in res.output


# --- docs schema -------------------------------------------------------------------


def _docs_lake(root: Path) -> Path:
    pl = pytest.importorskip("polars")
    pytest.importorskip("pyarrow")
    pytest.importorskip("quantilica.core.docs")
    folder = root / "alfa" / "vendas" / "ano=2024"
    folder.mkdir(parents=True)
    pl.DataFrame({"id_venda": [1]}).write_parquet(folder / "p.parquet")
    (root / "alfa" / "vendas" / "_dataset.json").write_text("{}", encoding="utf-8")
    return root


def _schema(lake: Path, out: Path, *extra: str):
    return runner.invoke(
        app,
        ["docs", "schema", "alfa", "--lake", str(lake), "--out", str(out), *extra],
    )


def test_docs_schema_refuses_a_source_being_updated(tmp_path: Path) -> None:
    lake = _docs_lake(tmp_path / "lake")
    _marcar(lake, "alfa")

    res = _schema(lake, tmp_path / "out")

    assert res.exit_code == 1
    assert "pulada" in _plain(res.output) and "em atualização" in _plain(res.output)
    assert not (tmp_path / "out").exists()


def test_docs_schema_with_state(tmp_path: Path) -> None:
    lake = _docs_lake(tmp_path / "lake")
    tamanhos = {
        p.relative_to(lake / "alfa").as_posix(): p.stat().st_size
        for p in (lake / "alfa").rglob("*.parquet")
    }
    (lake / "alfa" / MANIFESTO).write_text(
        json.dumps({"build_id": "B1", "parquets": tamanhos}), encoding="utf-8"
    )
    bom = _estado(tmp_path / "bom.json", alfa=("B1", "publicado"))
    velho = _estado(tmp_path / "velho.json", alfa=("B0", "publicado"))

    assert _schema(lake, tmp_path / "out", "--estado", str(bom)).exit_code == 0
    assert (tmp_path / "out" / "vendas.schema.json").exists()
    res = _schema(lake, tmp_path / "out2", "--estado", str(velho))
    assert res.exit_code == 1 and "auditou o build B0" in _plain(res.output)
    assert not (tmp_path / "out2").exists()


# --- lake audit --------------------------------------------------------------------


def test_audit_help_lists_the_new_options() -> None:
    res = runner.invoke(app, ["lake", "audit", "--help"], terminal_width=200)
    assert res.exit_code == 0
    out = _plain(res.output)
    for opcao in ("--somente-leitura", "--estado", "--limpar-marcador"):
        assert opcao in out


def test_audit_read_only_and_clear_marker_are_incompatible(tmp_path: Path) -> None:
    lake = _lake(tmp_path / "lake")
    res = runner.invoke(
        app,
        [
            "lake",
            "audit",
            "--lake",
            str(lake),
            "--somente-leitura",
            "--limpar-marcador",
        ],
    )
    assert res.exit_code == 2
    assert "não vale com --somente-leitura" in _plain(res.output)


@pytest.mark.parametrize("flag", ["--estado", "--json-file"])
def test_audit_read_only_refuses_outputs_inside_the_lake(
    tmp_path: Path, flag: str
) -> None:
    lake = _lake(tmp_path / "lake")
    res = runner.invoke(
        app,
        ["lake", "audit", "--lake", str(lake), "--somente-leitura", flag,
         str(lake / "ledger" / "saida.json")],
    )  # fmt: skip
    assert res.exit_code == 2
    assert "dentro do lake" in _plain(res.output)
    assert not (lake / "ledger" / "saida.json").exists()


def test_audit_options_reach_the_engine(tmp_path: Path, monkeypatch) -> None:
    pytest.importorskip("quantilica.analytics.lake_audit")
    engine = sys.modules["quantilica.analytics.lake_audit"]
    seen: dict = {}

    def spy(lake_dir, **kwargs):
        seen.update(kwargs)
        raise OSError("para aqui")

    monkeypatch.setattr(engine, "lake_audit", spy)
    lake = _lake(tmp_path / "lake")
    estado = tmp_path / "estado" / "e.json"

    runner.invoke(
        app,
        ["lake", "audit", "--lake", str(lake), "--somente-leitura", "--estado",
         str(estado)],
    )  # fmt: skip

    assert seen["read_only"] is True
    assert "clear_marker" not in seen
    assert seen["state"] == estado


def test_audit_state_feeds_catalog_sync_end_to_end(tmp_path: Path, monkeypatch) -> None:
    pytest.importorskip("quantilica.analytics.lake")
    core_lake = pytest.importorskip("quantilica.core.lake")
    if not hasattr(core_lake, "materialize_view"):
        pytest.skip("quantilica-core sem materialize_view")
    from quantilica.analytics.lake_audit import list_source_parquets
    from quantilica.analytics.lake_publish import write_build_manifest

    from tests.lake_fixtures import build_corpus

    corpus = build_corpus(tmp_path)
    fontes = sorted(p.name for p in corpus.lake.iterdir() if p.is_dir())
    assert len(fontes) >= 2
    from quantilica.core.registry import FetcherInfo

    registro = {fid: FetcherInfo(id=fid, nome=fid.upper()) for fid in fontes}
    monkeypatch.setattr(
        "quantilica.cli.catalog.load_registry", lambda *a, **k: registro
    )
    for fonte in fontes:
        write_build_manifest(
            corpus.lake / fonte, "B1", list_source_parquets(corpus.lake, fonte)
        )
    # Uma fonte ainda chegando: o marcador já está lá, os Parquets não.
    atualizando, *prontas = fontes
    _marcar(corpus.lake, atualizando, "B2")
    estado = tmp_path / "estado" / "lake.json"

    res = runner.invoke(
        app,
        ["lake", "audit", "--lake", str(corpus.lake), "--somente-leitura",
         "--estado", str(estado), "--no-readers"],
    )  # fmt: skip

    assert res.exit_code == 0, res.output
    dados = json.loads(estado.read_text(encoding="utf-8"))["fontes"]
    assert dados[atualizando]["resultado"] == "em_atualizacao"
    assert {dados[f]["resultado"] for f in prontas} == {"publicado"}
    assert all(dados[f]["build_id"] == "B1" for f in prontas)

    sync = _sync(corpus.lake, "--estado", str(estado))

    out = _plain(sync.output)
    assert sync.exit_code == 0, out
    assert f"fonte {atualizando} pulada" in out
    assert all(f"{f}/" in out for f in prontas)


# --- revisão: o disco precisa bater com o manifesto na hora de ler ----------------


def _com_parquets(lake: Path, fonte: str, build_id: str, tamanhos: dict[str, int]):
    """Escreve os Parquets (só bytes) e o manifesto que os lista."""
    for rel, tam in tamanhos.items():
        arquivo = lake / fonte / rel
        arquivo.parent.mkdir(parents=True, exist_ok=True)
        arquivo.write_bytes(b"x" * tam)
    (lake / fonte / MANIFESTO).write_text(
        json.dumps({"build_id": build_id, "parquets": tamanhos}), encoding="utf-8"
    )


@pytest.mark.parametrize(
    ("estrago", "trecho"),
    [
        (
            lambda p: (p / "dados" / "ano=1" / "novo.parquet").write_bytes(b"y"),
            "fora do",
        ),
        (lambda p: (p / "dados" / "ano=1" / "a.parquet").unlink(), "faltando"),
        (
            lambda p: (p / "dados" / "ano=1" / "a.parquet").write_bytes(b"xx"),
            "outro tamanho",
        ),
    ],
    ids=["parquet-novo-antes-do-marcador", "arquivo-faltando", "tamanho-diferente"],
)
def test_policy_skips_a_source_whose_disk_does_not_match_the_manifest(
    tmp_path: Path, estrago, trecho: str
) -> None:
    lake = _lake(tmp_path / "lake")
    _com_parquets(lake, "cvm", "B1", {"dados/ano=1/a.parquet": 5})
    estado = ler_estado(_estado(tmp_path / "e.json", cvm=("B1", "publicado")))
    politica = PoliticaDePublicacao(lake, estado)
    assert politica.motivo("cvm") is None  # o conjunto está inteiro

    estrago(lake / "cvm")
    motivo = PoliticaDePublicacao(lake, estado).motivo("cvm")

    assert motivo is not None and trecho in motivo and MANIFESTO in motivo


def test_policy_ignores_hidden_folders_when_checking_the_manifest(
    tmp_path: Path,
) -> None:
    lake = _lake(tmp_path / "lake")
    _com_parquets(lake, "cvm", "B1", {"dados/a.parquet": 3})
    (lake / "cvm" / "dados" / ".tmp").mkdir()
    (lake / "cvm" / "dados" / ".tmp" / "resto.parquet").write_bytes(b"z")
    estado = ler_estado(_estado(tmp_path / "e.json", cvm=("B1", "publicado")))

    assert PoliticaDePublicacao(lake, estado).motivo("cvm") is None


def test_policy_without_state_does_not_compare_the_disk(tmp_path: Path) -> None:
    # Sem estado o lake segue como antes: Parquets de fora do build são normais.
    lake = _lake(tmp_path / "lake")
    _com_parquets(lake, "cvm", "B1", {"dados/a.parquet": 3})
    (lake / "cvm" / "dados" / "de-um-convert.parquet").write_bytes(b"y")

    assert PoliticaDePublicacao(lake).motivo("cvm") is None


def test_sync_skips_a_source_with_a_half_synced_build(tmp_path: Path) -> None:
    lake = _lake(tmp_path / "lake")
    _com_parquets(lake, "cvm", "B1", {"dados/a.parquet": 5})
    _com_parquets(lake, "anp", "B1", {"dados/a.parquet": 5})
    (lake / "anp" / "dados" / "b2.parquet").write_bytes(b"do build B2")
    estado = _estado(
        tmp_path / "e.json", cvm=("B1", "publicado"), anp=("B1", "publicado")
    )

    out = _plain(_sync(lake, "--estado", str(estado)).output)

    assert "cvm/dados" in out and "anp/dados" not in out
    assert "fonte anp pulada" in out and "fora do manifesto" in out


def test_audit_state_requires_read_only(tmp_path: Path) -> None:
    lake = _lake(tmp_path / "lake")

    res = runner.invoke(
        app,
        ["lake", "audit", "--lake", str(lake), "--estado", str(tmp_path / "e.json")],
    )

    assert res.exit_code == 2
    assert "--somente-leitura" in _plain(res.output)
    assert not (tmp_path / "e.json").exists()


def test_audit_with_an_old_analytics_asks_to_update(
    tmp_path: Path, monkeypatch
) -> None:
    pytest.importorskip("quantilica.analytics.lake_audit")
    engine = sys.modules["quantilica.analytics.lake_audit"]

    def velho(lake_dir, *, input_dir=None, workers=None, **kwargs):
        # como um analytics sem as opções novas: não aceita read_only
        if "read_only" in kwargs:
            raise TypeError(
                "lake_audit() got an unexpected keyword argument 'read_only'"
            )
        raise AssertionError("não devia chegar aqui")

    monkeypatch.setattr(engine, "lake_audit", velho)
    lake = _lake(tmp_path / "lake")

    res = runner.invoke(
        app, ["lake", "audit", "--lake", str(lake), "--somente-leitura"]
    )

    out = _plain(res.output)
    assert res.exit_code == 2
    assert "Atualize" in out and "quantilica-analytics" in out
    assert "Traceback" not in out
