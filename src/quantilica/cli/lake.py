"""Comandos `quantilica lake` — selecionar, construir e auditar o lake.

Fluxo: bruto local -> ``select`` (visão só com a versão vigente de cada
arquivo) -> ``build`` (visão -> Parquet) -> ``audit`` (prova que o lake está
completo e íntegro). O ledger do acervo é opcional em todas as etapas.

``select`` só depende do ``quantilica-core``. ``build`` e ``audit`` usam o
``quantilica-analytics`` (extra ``lake``), importado só quando o comando roda,
de modo que ``--help`` funciona sem ele.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.logging import RichHandler
from rich.markup import escape

app = typer.Typer(
    help="Montar e auditar o lake: bruto local → select → build → audit.",
    no_args_is_help=True,
)

console = Console()
err_console = Console(stderr=True)

ANALYTICS_HINT = (
    'pip install "quantilica-cli[lake]"  (ou: pip install quantilica-analytics)'
)
CORE_HINT = "pip install --upgrade quantilica-core"

EXIT_FAILURE = 1
EXIT_USAGE = 2
EXIT_INTERRUPTED = 130


class LinkMode(StrEnum):
    """Como os arquivos entram na visão (ver ``materialize_view``)."""

    hardlink = "hardlink"
    symlink = "symlink"
    copy = "copy"


def _fail(message: str, code: int = EXIT_FAILURE) -> typer.Exit:
    """Escreve ``message`` em stderr e devolve a saída a ser levantada."""
    err_console.print(f"[red]Erro:[/red] {escape(message)}", highlight=False)
    return typer.Exit(code)


def _missing_analytics(command: str, exc: ImportError) -> typer.Exit:
    return _fail(
        f"'quantilica lake {command}' requer o quantilica-analytics "
        f"({exc}). Instale com: {ANALYTICS_HINT}"
    )


def _split_sources(value: str | None) -> list[str] | None:
    """Converte ``"a,b"`` em ``["a", "b"]``; vazio vira ``None`` (todas)."""
    if value is None:
        return None
    sources = [part.strip() for part in value.split(",") if part.strip()]
    if not sources:
        raise _fail("--sources está vazio: informe fontes separadas por vírgula.", 2)
    return sources


def _read_list(path: Path) -> list[str]:
    """Lê um arquivo texto com um item por linha (vazias e ``#`` ignoradas)."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise _fail(f"não consegui ler {path}: {exc}") from exc
    items: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            items.append(line)
    return items


@contextmanager
def _log_to_stderr() -> Iterator[None]:
    """Mostra o andamento (INFO do logger ``quantilica``) em stderr.

    O logger ``quantilica`` (core e analytics) ganha um handler próprio e deixa
    de propagar para a raiz, cujo handler escreve em stdout e sujaria a saída
    ``--json``. O estado anterior é restaurado ao sair.
    """
    logger = logging.getLogger("quantilica")
    saved = (logger.handlers, logger.level, logger.propagate)
    logger.handlers = [
        RichHandler(
            console=err_console,
            show_path=False,
            show_time=True,
            show_level=False,
            rich_tracebacks=True,
        )
    ]
    logger.setLevel(logging.INFO)
    logger.propagate = False
    try:
        yield
    finally:
        logger.handlers, logger.level, logger.propagate = saved


@app.command("select")
def select(
    raw: Annotated[
        Path,
        typer.Option(
            "--raw",
            help="Diretório do bruto (uma pasta por fonte)",
            exists=True,
            file_okay=False,
        ),
    ],
    out: Annotated[
        Path,
        typer.Option(
            "--out",
            help="Diretório da visão vigente (usado só para a visão)",
            file_okay=False,
        ),
    ],
    mode: Annotated[
        LinkMode,
        typer.Option(
            "--mode",
            help="Como os arquivos entram na visão; se falhar, recua "
            "hardlink → symlink → cópia",
        ),
    ] = LinkMode.hardlink,
    exclude: Annotated[
        Path | None,
        typer.Option(
            "--exclude",
            help="Arquivo texto com um sha256 ou caminho relativo por linha "
            "(vazias e # ignoradas) que não devem ir ao lake",
            exists=True,
            dir_okay=False,
        ),
    ] = None,
) -> None:
    """Cria a visão vigente: só a versão mais recente de cada arquivo bruto."""
    try:
        from quantilica.core.lake import materialize_view
    except ImportError as exc:
        raise _fail(
            f"'quantilica lake select' requer um quantilica-core mais novo "
            f"({exc}). Atualize com: {CORE_HINT}"
        ) from None

    excluded = _read_list(exclude) if exclude is not None else None
    try:
        summary = materialize_view(raw, out, mode=mode.value, exclude=excluded)
    except (ValueError, OSError) as exc:
        raise _fail(str(exc)) from exc

    console.print(f"[green]✓[/green] Visão em [dim]{out}[/dim]", highlight=False)
    typer.echo(f"  arquivos:   {summary.files}")
    typer.echo(
        f"  criados:    hardlink={summary.hardlink} symlink={summary.symlink} "
        f"copy={summary.copy}"
    )
    typer.echo(f"  mantidos:   {summary.kept}")
    typer.echo(f"  excluídos:  {summary.excluded}")
    typer.echo(f"  removidos:  {summary.removed}")


@app.command("build")
def build(
    input: Annotated[
        Path,
        typer.Option(
            "-i",
            "--input",
            help="Visão vigente (saída de 'lake select'): uma pasta por fonte",
            exists=True,
            file_okay=False,
        ),
    ],
    lake: Annotated[
        Path,
        typer.Option(
            "--lake",
            "-o",
            help="Raiz do lake; o estado da construção fica em <lake>/.build/",
            file_okay=False,
        ),
    ],
    sources: Annotated[
        str | None,
        typer.Option(
            "--sources",
            help="Fontes a construir, separadas por vírgula (padrão: todas "
            "as que têm conversor)",
        ),
    ] = None,
    workers: Annotated[
        int | None,
        typer.Option("--workers", min=1, help="Processos em paralelo"),
    ] = None,
    memory: Annotated[
        str | None,
        typer.Option("--memory", help="Limite de memória do DuckDB por tarefa (12GB)"),
    ] = None,
    threads: Annotated[
        int | None,
        typer.Option("--threads", min=1, help="Threads do DuckDB por tarefa"),
    ] = None,
    scratch: Annotated[
        Path | None,
        typer.Option(
            "--scratch",
            help="Diretório temporário (extração e DuckDB); use um disco rápido",
            file_okay=False,
        ),
    ] = None,
    force: Annotated[
        bool,
        typer.Option("--force", help="Refaz todas as tarefas e reconverte tudo"),
    ] = False,
    retry_errors: Annotated[
        bool,
        typer.Option(
            "--retry-errors", help="Refaz também as tarefas que falharam antes"
        ),
    ] = False,
    no_disk_check: Annotated[
        bool,
        typer.Option(
            "--no-disk-check", help="Não compara o espaço livre com a estimativa"
        ),
    ] = False,
    allow_sleep: Annotated[
        bool,
        typer.Option(
            "--allow-sleep", help="Não impede a suspensão do sistema durante a obra"
        ),
    ] = False,
) -> None:
    """Converte a visão vigente em Parquet; retoma de onde parou."""
    try:
        from quantilica.analytics.lake_build import (
            InsufficientDiskError,
            LakeBuildError,
            lake_build,
        )
    except ImportError as exc:
        raise _missing_analytics("build", exc) from None

    wanted = _split_sources(sources)
    try:
        with _log_to_stderr():
            summary = lake_build(
                input,
                lake,
                sources=wanted,
                workers=workers,
                memory=memory,
                threads=threads,
                scratch=scratch,
                force=force,
                retry_errors=retry_errors,
                check_disk=not no_disk_check,
                prevent_sleep=not allow_sleep,
            )
    except KeyboardInterrupt:
        err_console.print(
            "[yellow]Interrompido.[/yellow] O que terminou ficou registrado; "
            "rode o mesmo comando de novo para retomar."
        )
        raise typer.Exit(EXIT_INTERRUPTED) from None
    except InsufficientDiskError as exc:
        raise _fail(f"espaço em disco insuficiente: {exc}") from exc
    except LakeBuildError as exc:
        raise _fail(str(exc)) from exc

    typer.echo(summary.format())
    if summary.has_errors:
        raise _fail("a construção terminou com erros (veja acima).")
    console.print(f"[green]✓[/green] Lake em [dim]{lake}[/dim].", highlight=False)


@app.command("audit")
def audit(
    lake: Annotated[
        Path,
        typer.Option(
            "--lake",
            help="Raiz do lake a auditar",
            exists=True,
            file_okay=False,
        ),
    ],
    input: Annotated[
        Path | None,
        typer.Option(
            "-i",
            "--input",
            help="Visão vigente (ou bruto) de onde o lake saiu; habilita as "
            "checagens de proveniência e cobertura",
            exists=True,
            file_okay=False,
        ),
    ] = None,
    ledger: Annotated[
        Path | None,
        typer.Option(
            "--ledger",
            help="Exportação do ledger (opcional) para conferir a proveniência",
            exists=True,
            dir_okay=False,
        ),
    ] = None,
    sources: Annotated[
        str | None,
        typer.Option(
            "--sources", help="Auditar só estas fontes (separadas por vírgula)"
        ),
    ] = None,
    no_readers: Annotated[
        bool,
        typer.Option(
            "--no-readers",
            help="Não roda check_dataset (DuckDB, Polars, PyArrow); é a etapa cara",
        ),
    ] = False,
    no_integrity: Annotated[
        bool,
        typer.Option(
            "--no-integrity",
            help="Lê só o rodapé dos Parquets, não o último row group",
        ),
    ] = False,
    exceptions: Annotated[
        Path | None,
        typer.Option(
            "--exceptions",
            help="Arquivo texto com caminhos brutos (aceita padrões fnmatch) "
            "sabidamente sem Parquet, como os corrompidos na fonte",
            exists=True,
            dir_okay=False,
        ),
    ] = None,
    rehash: Annotated[
        bool,
        typer.Option(
            "--rehash",
            help="Recalcula o sha256 do bruto em vez de confiar no manifesto",
        ),
    ] = False,
    workers: Annotated[
        int | None,
        typer.Option("--workers", min=1, help="Threads que leem os rodapés"),
    ] = None,
    json_stdout: Annotated[
        bool,
        typer.Option(
            "--json", help="Imprime o relatório em JSON no stdout, no lugar do texto"
        ),
    ] = False,
    json_file: Annotated[
        Path | None,
        typer.Option(
            "--json-file",
            help="Grava o relatório em JSON neste arquivo (o texto continua no "
            "stdout, a menos que --json também seja dado)",
            dir_okay=False,
        ),
    ] = None,
) -> None:
    """Audita o lake; sai com 0 se aprovado e com 1 se houver achados."""
    try:
        from quantilica.analytics.lake_audit import lake_audit
    except ImportError as exc:
        raise _missing_analytics("audit", exc) from None

    wanted = _split_sources(sources)
    known = _read_list(exceptions) if exceptions is not None else ()
    try:
        with _log_to_stderr():
            report = lake_audit(
                lake,
                input_dir=input,
                ledger=ledger,
                sources=wanted,
                readers=not no_readers,
                integrity=not no_integrity,
                known_exceptions=known,
                rehash=rehash,
                workers=workers,
            )
    except KeyboardInterrupt:
        err_console.print("[yellow]Interrompido.[/yellow]")
        raise typer.Exit(EXIT_INTERRUPTED) from None
    except (OSError, ValueError) as exc:
        raise _fail(str(exc)) from exc

    if json_file is not None:
        try:
            json_file.write_text(report.to_json() + "\n", encoding="utf-8")
        except OSError as exc:
            raise _fail(f"não consegui gravar {json_file}: {exc}") from exc
        err_console.print(f"Relatório JSON em [dim]{json_file}[/dim]", highlight=False)
    typer.echo(report.to_json() if json_stdout else report.to_text())
    if not report.ok:
        raise typer.Exit(EXIT_FAILURE)
