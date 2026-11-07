"""Testes dos comandos `quantilica docs` (check, schema, update)."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

pytest.importorskip("yaml")
pytest.importorskip("pyarrow")
pl = pytest.importorskip("polars")
pytest.importorskip("quantilica.core.docs")

from quantilica.cli.cli import app  # noqa: E402

runner = CliRunner()

SECTIONS = [
    "Resumo",
    "Ficha técnica",
    "Origem e coleta",
    "Conteúdo",
    "Variáveis",
    "No lake",
    "Cuidados de uso",
    "Exemplos",
    "Referências",
]

HEADER = """\
---
dataset:
  id: vendas
  fonte: alfa
  titulo: Vendas
  resumo: Vendas mensais.
  orgao: Órgão X
  pagina_oficial: https://example.org
  licenca: CC-BY 4.0
  periodicidade: mensal
  cobertura: {inicio: 2020-01, fim: em-curso}
  unidade: uma venda
  granularidade: {espacial: Brasil, temporal: mês}
  formato_bruto: CSV
  lake: {estado: convertido, particoes: [ano], chave: [id_venda], colunas: 2}
  unidades_de_medida: {valor: R$}
  dados_pessoais: nenhum
  eras: []
  revisado_em: 2026-11-07
  versao_fetcher: 1.0.0
---
"""

VARS = (
    "| Coluna no lake | Na fonte | Tipo | Descrição | Unidade / códigos | Período |\n"
    "|---|---|---|---|---|---|\n"
    "| `id_venda` | ID | int | id | | |\n"
    "| `valor` | VLR | double | valor | R$ | |\n"
)


def page_text(skip: str | None = None) -> str:
    text = HEADER + "\n# Vendas\n\n"
    for title in SECTIONS:
        if title == skip:
            continue
        text += f"## {title}\n\n"
        if title == "Ficha técnica":
            text += "<!-- gerado:ficha -->\n\n"
        if title == "Variáveis":
            text += VARS + "\n"
    return text


def make_repo(root: Path, *, skip: str | None = None, entry_point: bool = True) -> Path:
    datasets = root / "docs" / "datasets"
    datasets.mkdir(parents=True)
    (root / "docs" / "README.md").write_text("# Alfa\n", encoding="utf-8")
    (datasets / "vendas.md").write_text(page_text(skip), encoding="utf-8")
    schema = {
        "fonte": "alfa",
        "dataset": "vendas",
        "gerado_em": "2026-11-07T00:00:00+00:00",
        "particoes": ["ano"],
        "colunas": [
            {"nome": "id_venda", "tipos": ["int64"]},
            {"nome": "valor", "tipos": ["double"]},
        ],
        "arquivos": 1,
    }
    (datasets / "vendas.schema.json").write_text(json.dumps(schema), encoding="utf-8")
    if entry_point:
        (root / "pyproject.toml").write_text(
            '[project]\nname = "alfa-fetcher"\n'
            '[project.entry-points."quantilica.lake"]\nalfa = "alfa.lake:converter"\n',
            encoding="utf-8",
        )
    return root


def no_converter(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("quantilica.core.lake.load_lake_converter", lambda _s: None)


def test_docs_help_lists_commands() -> None:
    result = runner.invoke(app, ["docs", "--help"])
    assert result.exit_code == 0
    for name in ("check", "schema", "update"):
        assert name in result.output


def test_check_ok_from_files(tmp_path: Path, monkeypatch) -> None:
    no_converter(monkeypatch)
    repo = make_repo(tmp_path)
    result = runner.invoke(app, ["docs", "check", str(repo)])
    assert result.exit_code == 0, result.output
    assert "1 dataset(s) de alfa" in result.output


def test_check_reports_problems_and_exits_1(tmp_path: Path, monkeypatch) -> None:
    no_converter(monkeypatch)
    repo = make_repo(tmp_path, skip="Exemplos")
    result = runner.invoke(app, ["docs", "check", str(repo)])
    assert result.exit_code == 1
    assert "seções ausentes: Exemplos" in result.output


def test_check_uses_installed_converter_datasets(tmp_path: Path, monkeypatch) -> None:
    converter = SimpleNamespace(source_id="alfa", datasets=lambda: ["vendas", "extra"])
    monkeypatch.setattr(
        "quantilica.core.lake.load_lake_converter", lambda _s: converter
    )
    repo = make_repo(tmp_path)
    result = runner.invoke(app, ["docs", "check", str(repo)])
    assert result.exit_code == 1
    assert "dataset declarado sem página" in result.output


def test_check_source_from_pages_and_option(tmp_path: Path, monkeypatch) -> None:
    no_converter(monkeypatch)
    repo = make_repo(tmp_path, entry_point=False)
    assert runner.invoke(app, ["docs", "check", str(repo)]).exit_code == 0
    wrong = runner.invoke(app, ["docs", "check", str(repo), "--source", "beta"])
    assert wrong.exit_code == 1
    assert "fonte 'alfa' diferente" in wrong.output


def test_check_without_docs_dir(tmp_path: Path) -> None:
    result = runner.invoke(app, ["docs", "check", str(tmp_path)])
    assert result.exit_code == 1


def test_update_rewrites_ficha_and_is_idempotent(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    page = repo / "docs" / "datasets" / "vendas.md"
    first = runner.invoke(app, ["docs", "update", str(repo)])
    assert first.exit_code == 0, first.output
    assert "atualizada" in first.output
    assert "| Dataset | `alfa/vendas` |" in page.read_text(encoding="utf-8")
    second = runner.invoke(app, ["docs", "update", str(repo)])
    assert second.exit_code == 0
    assert "sem mudança" in second.output


def test_update_fails_on_invalid_page(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    (repo / "docs" / "datasets" / "vendas.md").write_text("sem cabeçalho", "utf-8")
    result = runner.invoke(app, ["docs", "update", str(repo)])
    assert result.exit_code == 1


def make_lake(root: Path) -> Path:
    for mes in (1, 2):
        folder = root / "alfa" / "vendas" / "ano=2024" / f"mes={mes}"
        folder.mkdir(parents=True)
        pl.DataFrame({"id_venda": [1], "valor": [1.5]}).write_parquet(
            folder / "p.parquet"
        )
    (root / "alfa" / "vendas" / "_dataset.json").write_text("{}", encoding="utf-8")
    return root


def test_schema_writes_files(tmp_path: Path) -> None:
    lake = make_lake(tmp_path / "lake")
    out = tmp_path / "out"
    result = runner.invoke(
        app, ["docs", "schema", "alfa", "--lake", str(lake), "--out", str(out)]
    )
    assert result.exit_code == 0, result.output
    data = json.loads((out / "vendas.schema.json").read_text(encoding="utf-8"))
    assert data["particoes"] == ["ano", "mes"]
    assert data["arquivos"] == 2
    assert {c["nome"] for c in data["colunas"]} == {"id_venda", "valor"}
    again = runner.invoke(
        app, ["docs", "schema", "alfa", "--lake", str(lake), "--out", str(out)]
    )
    assert "sem mudança" in again.output


def test_schema_dataset_filter_and_errors(tmp_path: Path) -> None:
    lake = make_lake(tmp_path / "lake")
    out = tmp_path / "out"
    bad = runner.invoke(
        app,
        ["docs", "schema", "alfa", "--lake", str(lake), "--out", str(out),
         "--dataset", "nada"],
    )  # fmt: skip
    assert bad.exit_code == 1
    missing = runner.invoke(
        app, ["docs", "schema", "zeta", "--lake", str(lake), "--out", str(out)]
    )
    assert missing.exit_code == 1
