"""Comandos `quantilica docs` — a documentação dos dados dos fetchers.

Ferramentas da norma "Documentação dos dados": ``check`` confere a pasta
``docs/`` de um fetcher, ``schema`` grava os esquemas de referência a partir do
lake e ``update`` reescreve a ficha técnica das páginas.

Dependem do ``quantilica-core`` com o extra ``docs`` (``pyyaml`` e
``pyarrow``), importado só quando o comando roda, de modo que ``--help``
funciona sem ele.

Como o ``check`` descobre o que verificar:

- fonte: ``--source``; senão o entry point ``quantilica.lake`` declarado no
  ``pyproject.toml`` do repositório; senão o campo ``fonte`` das páginas;
- datasets: ``datasets()`` do conversor, se o fetcher estiver instalado
  (entry point ``quantilica.lake``); senão os arquivos de ``docs/datasets/``
  (e então a verificação "dataset sem página" não pode falhar).
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.markup import escape

app = typer.Typer(
    help="Documentação dos dados dos fetchers: verificar, gerar esquemas e fichas.",
    no_args_is_help=True,
)

console = Console()
err_console = Console(stderr=True)

DOCS_HINT = 'uv add "quantilica-core[docs]"  (ou: pip install "quantilica-core[docs]")'
LAKE_ENTRY_POINT_GROUP = "quantilica.lake"

EXIT_FAILURE = 1


def _fail(message: str, code: int = EXIT_FAILURE) -> typer.Exit:
    err_console.print(f"[red]Erro:[/red] {escape(message)}", highlight=False)
    return typer.Exit(code)


def _import_docs():
    try:
        from quantilica.core import docs
    except ImportError as exc:
        raise _fail(
            f"'quantilica docs' requer o quantilica-core com o extra docs "
            f"({exc}). Instale com: {DOCS_HINT}"
        ) from None
    return docs


def _source_from_pyproject(repo_dir: Path) -> str | None:
    """Nome do entry point ``quantilica.lake`` do ``pyproject.toml`` do repo."""
    path = repo_dir / "pyproject.toml"
    if not path.is_file():
        return None
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return None
    eps = data.get("project", {}).get("entry-points", {}).get(LAKE_ENTRY_POINT_GROUP)
    if isinstance(eps, dict) and len(eps) == 1:
        return next(iter(eps))
    return None


def _datasets_dir(repo_dir: Path) -> Path:
    return repo_dir / "docs" / "datasets"


def _page_stems(repo_dir: Path) -> list[str]:
    folder = _datasets_dir(repo_dir)
    return sorted(p.stem for p in folder.glob("*.md")) if folder.is_dir() else []


def _sources_in_pages(docs, repo_dir: Path) -> set[str]:
    found: set[str] = set()
    for stem in _page_stems(repo_dir):
        try:
            header, _ = docs.parse_dataset_page(_datasets_dir(repo_dir) / f"{stem}.md")
        except docs.DocFormatError:
            continue
        found.add(header.fonte)
    return found


@app.command("check")
def check(
    repo_dir: Annotated[
        Path,
        typer.Argument(
            help="Raiz do repositório do fetcher (a pasta que contém docs/)",
            exists=True,
            file_okay=False,
        ),
    ],
    source: Annotated[
        str | None,
        typer.Option(
            "--source",
            help="Id da fonte; padrão: entry point do pyproject.toml ou o campo "
            "'fonte' das páginas",
        ),
    ] = None,
) -> None:
    """Verifica a documentação dos dados de um fetcher (sai com 1 se houver erro)."""
    docs = _import_docs()
    docs_dir = repo_dir / "docs"
    if not docs_dir.is_dir():
        raise _fail(f"{repo_dir} não tem a pasta docs/.")

    source_id = source or _source_from_pyproject(repo_dir)
    if source_id is None:
        sources = _sources_in_pages(docs, repo_dir)
        if len(sources) == 1:
            source_id = next(iter(sources))
        else:
            raise _fail(
                "não consegui descobrir a fonte (pyproject.toml sem entry point "
                f"'{LAKE_ENTRY_POINT_GROUP}' e páginas com fontes {sorted(sources)}); "
                "informe --source."
            )

    datasets: list[str] | None = None
    origin = ""
    try:
        from quantilica.core.lake import load_lake_converter

        converter = load_lake_converter(source_id)
        if converter is not None:
            datasets = list(converter.datasets())
            origin = f"conversor '{source_id}'"
    except Exception as exc:
        err_console.print(
            f"[yellow]Aviso:[/yellow] não carreguei o conversor '{escape(source_id)}' "
            f"({escape(str(exc))}); uso os arquivos de docs/datasets/.",
            highlight=False,
        )
    if datasets is None:
        datasets = _page_stems(repo_dir)
        origin = "arquivos de docs/datasets/"
        err_console.print(
            f"[dim]Datasets descobertos pelos {origin} (fetcher não instalado): "
            "dataset sem página não é detectado.[/dim]",
            highlight=False,
        )

    problems = docs.check_fetcher_docs(docs_dir, datasets, source_id)
    errors = [p for p in problems if p.severity == "error"]
    warnings = [p for p in problems if p.severity == "warning"]
    for problem in problems:
        color = "yellow" if problem.severity == "warning" else "red"
        label = "aviso" if problem.severity == "warning" else "erro"
        console.print(
            f"[{color}]{label}[/{color}] {escape(problem.path)}: "
            f"{escape(problem.message)}",
            highlight=False,
        )
    if errors:
        console.print(
            f"[red]✗[/red] {len(errors)} erro(s), {len(warnings)} aviso(s) em "
            f"{len(datasets)} dataset(s) de {escape(source_id)}.",
            highlight=False,
        )
        raise typer.Exit(EXIT_FAILURE)
    console.print(
        f"[green]✓[/green] {len(datasets)} dataset(s) de {escape(source_id)} "
        f"documentado(s); {len(warnings)} aviso(s).",
        highlight=False,
    )


@app.command("schema")
def schema(
    source_id: Annotated[str, typer.Argument(help="Id da fonte (pasta no lake)")],
    lake: Annotated[
        Path,
        typer.Option("--lake", help="Raiz do lake", exists=True, file_okay=False),
    ],
    out: Annotated[
        Path,
        typer.Option(
            "--out",
            help="Pasta docs/datasets do fetcher onde gravar os <id>.schema.json",
            file_okay=False,
        ),
    ],
    dataset: Annotated[
        list[str] | None,
        typer.Option(
            "--dataset",
            help="Dataset a atualizar (repetível); padrão: todos os da fonte no lake",
        ),
    ] = None,
) -> None:
    """Grava os esquemas de referência (<id>.schema.json) lendo o lake."""
    docs = _import_docs()
    source_dir = lake / source_id
    if not source_dir.is_dir():
        raise _fail(f"a fonte '{source_id}' não está no lake: {source_dir} não existe.")
    wanted = dataset or sorted(
        p.name
        for p in source_dir.iterdir()
        if p.is_dir() and not p.name.startswith(("_", "."))
    )
    if not wanted:
        raise _fail(f"nenhum dataset em {source_dir}.")

    failed = 0
    for dataset_id in wanted:
        try:
            data = docs.schema_from_lake(lake, source_id, dataset_id)
        except (FileNotFoundError, OSError) as exc:
            err_console.print(
                f"[red]erro[/red] {escape(dataset_id)}: {escape(str(exc))}",
                highlight=False,
            )
            failed += 1
            continue
        path = out / f"{dataset_id}.schema.json"
        changed = docs.write_schema(path, data)
        state = "gravado" if changed else "sem mudança"
        console.print(
            f"[green]✓[/green] {escape(dataset_id)}: {state} "
            f"({len(data['colunas'])} colunas, {data['arquivos']} arquivos)",
            highlight=False,
        )
    if failed:
        raise typer.Exit(EXIT_FAILURE)


@app.command("update")
def update(
    repo_dir: Annotated[
        Path,
        typer.Argument(
            help="Raiz do repositório do fetcher (a pasta que contém docs/)",
            exists=True,
            file_okay=False,
        ),
    ],
) -> None:
    """Reescreve a ficha técnica das páginas a partir do cabeçalho."""
    docs = _import_docs()
    folder = _datasets_dir(repo_dir)
    pages = sorted(folder.glob("*.md")) if folder.is_dir() else []
    if not pages:
        raise _fail(f"nenhuma página em {folder}.")
    failed = 0
    for page in pages:
        try:
            changed = docs.update_ficha(page)
        except docs.DocFormatError as exc:
            err_console.print(
                f"[red]erro[/red] {escape(page.name)}: {escape(str(exc))}",
                highlight=False,
            )
            failed += 1
            continue
        state = "atualizada" if changed else "sem mudança"
        console.print(f"[green]✓[/green] {escape(page.name)}: {state}", highlight=False)
    if failed:
        raise typer.Exit(EXIT_FAILURE)
