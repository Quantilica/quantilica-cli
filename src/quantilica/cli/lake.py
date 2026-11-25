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


def _selects_versions(source: str) -> bool:
    """Se o conversor instalado de ``source`` declara ``selects_versions``.

    Esse conversor escolhe a versão vigente por conta própria e recebe de
    propósito várias versões do mesmo arquivo. Sem conversor instalado (ou com
    erro ao carregá-lo) devolve ``False``: vale o comportamento geral.
    """
    from quantilica.cli.vigente import converter_selects_versions

    try:
        from quantilica.core.lake import load_lake_converter

        converter = load_lake_converter(source)
    except Exception:
        return False
    return converter_selects_versions(converter, getattr(converter, "convert", None))


def _self_selecting_sources(raw: Path) -> list[str]:
    """Pastas de fonte de ``raw`` cujo conversor declara ``selects_versions``."""
    return sorted(
        entry.name
        for entry in raw.iterdir()
        if entry.is_dir() and _selects_versions(entry.name)
    )


def _files_to_link(root: Path) -> list[str]:
    """Arquivos de ``root`` (relativos, manifestos incluídos, sem ``*.tmp``): o que
    ``_link_all_files`` liga."""
    import os

    rels: list[str] = []
    for current, _, names in os.walk(root):
        base = Path(current).relative_to(root)
        rels.extend(
            (base / name).as_posix() for name in names if not name.endswith(".tmp")
        )
    return rels


def _current_data_files(rels: list[str]) -> list[str]:
    """Os arquivos de dados (sem manifestos) que ``select_current`` escolheria."""
    from quantilica.core.lake import MANIFEST_SIDECAR_SUFFIX, select_current

    return [r for r in select_current(rels) if not r.endswith(MANIFEST_SIDECAR_SUFFIX)]


def _link_all_files(source: Path, dest: Path, mode: str) -> dict[str, int]:
    """Liga em ``dest`` todos os arquivos de ``source``, de todas as versões.

    Mesmo recuo de ``materialize_view`` (hardlink, symlink, cópia). Ignora
    ``*.tmp``. Devolve quantos arquivos entraram por modo e ``files`` no total.
    """
    import os
    import shutil

    chain = ["hardlink", "symlink", "copy"]
    counts = {"hardlink": 0, "symlink": 0, "copy": 0, "files": 0}
    for current, _, names in os.walk(source):
        for name in sorted(names):
            if name.endswith(".tmp"):
                continue
            src = Path(current) / name
            dst = dest / src.relative_to(source)
            counts["files"] += 1
            if dst.exists() and os.path.samefile(src, dst):
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            tmp = dst.with_name(f".{dst.name}.{os.getpid()}.tmp")
            for attempt in chain[chain.index(mode) :]:
                tmp.unlink(missing_ok=True)
                try:
                    if attempt == "hardlink":
                        os.link(src, tmp)
                    elif attempt == "symlink":
                        os.symlink(os.path.abspath(src), tmp)
                    else:
                        shutil.copy2(src, tmp)
                except OSError:
                    tmp.unlink(missing_ok=True)
                    if attempt == "copy":
                        raise
                    continue
                os.replace(tmp, dst)
                counts[attempt] += 1
                break
    return counts


def _warn_multiple_versions(input: Path, sources: list[str] | None) -> None:
    """Avisa em stderr quando uma fonte de ``--sources`` tem mais de uma versão
    do mesmo arquivo em ``input``.

    O build converte tudo o que está na entrada; ela deveria ser a visão
    vigente (``lake select``). Só avisa: não muda o que o build faz. Sem
    ``--sources`` não varre nada: seria percorrer o input inteiro (o lake todo)
    só para avisar, e o build já lê essas pastas. Fontes cujo conversor declara
    ``selects_versions`` ficam de fora: elas recebem várias versões de propósito.
    """
    from quantilica.cli.vigente import superseded_versions

    if not sources:
        return
    names = sources
    old: list[str] = []
    for name in names:
        folder = input / name
        if folder.is_dir() and not _selects_versions(name):
            old.extend(f"{name}/{rel}" for rel in superseded_versions(folder))
    if not old:
        return
    examples = ", ".join(old[:3]) + (", ..." if len(old) > 3 else "")
    err_console.print(
        f"[yellow]Aviso:[/yellow] {escape(str(input))} tem {len(old)} arquivo(s) "
        "com versão mais nova do mesmo arquivo (por exemplo: "
        f"{escape(examples)}). O build converte todos; a entrada deveria ser a "
        "visão vigente: rode 'quantilica lake select' e use a pasta dela em "
        "--input.",
        highlight=False,
    )


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
    """Cria a visão vigente: só a versão mais recente de cada arquivo bruto.

    Fontes cujo conversor instalado declara ``selects_versions`` (o conversor
    escolhe a versão vigente por conta própria, como o antt) entram na visão
    com todas as versões, sem seleção, e ``--exclude`` não vale para elas.
    """
    try:
        from quantilica.core.lake import materialize_view
    except ImportError as exc:
        raise _fail(
            f"'quantilica lake select' requer um quantilica-core mais novo "
            f"({exc}). Atualize com: {CORE_HINT}"
        ) from None

    excluded = _read_list(exclude) if exclude is not None else None
    own: list[str] = _self_selecting_sources(raw)
    own_files: list[str] = []
    for name in own:
        own_files.extend(f"{name}/{rel}" for rel in _files_to_link(raw / name))
    try:
        summary = materialize_view(
            raw,
            out,
            mode=mode.value,
            # as fontes de seleção própria saem da seleção e entram inteiras abaixo
            exclude=[*(excluded or []), *own_files] if own else excluded,
        )
        linked = (
            {name: _link_all_files(raw / name, out / name, mode.value) for name in own}
            if own
            else {}
        )
    except (ValueError, OSError) as exc:
        raise _fail(str(exc)) from exc

    held_back = len(_current_data_files(own_files)) if own else 0
    console.print(f"[green]✓[/green] Visão em [dim]{out}[/dim]", highlight=False)
    typer.echo(f"  arquivos:   {summary.files}")
    typer.echo(
        f"  criados:    hardlink={summary.hardlink} symlink={summary.symlink} "
        f"copy={summary.copy}"
    )
    typer.echo(f"  mantidos:   {summary.kept}")
    typer.echo(f"  excluídos:  {max(summary.excluded - held_back, 0)}")
    typer.echo(f"  removidos:  {summary.removed}")
    for name, counts in linked.items():
        typer.echo(
            f"  {name}: seleção própria do conversor, {counts['files']} arquivo(s) "
            "de todas as versões (sem escolher a vigente)"
        )


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
    dataset: Annotated[
        str | None,
        typer.Option(
            "--dataset",
            "--datasets",
            help="Datasets a construir, separados por vírgula (padrão: todos da fonte)",
        ),
    ] = None,
    profile: Annotated[
        str | None,
        typer.Option(
            "--profile",
            help=(
                "Perfil de recursos: 'batch' (6 workers x 14GB) ou "
                "'monolithic' (1 worker x 64GB). Os nomes antigos "
                "'96gb-batch' e '96gb-monolithic' ainda são aceitos"
            ),
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
            "--retry-errors",
            help="Refaz só as tarefas que falharam na execução mais recente",
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
    """Converte a visão vigente em Parquet; retoma uma execução interrompida."""
    try:
        # extra opcional `lake`: ausente do ambiente de dev padrão
        from quantilica.analytics.lake_build import (  # pyright: ignore[reportMissingImports]
            InsufficientDiskError,
            LakeBuildError,
            lake_build,
        )
    except ImportError as exc:
        raise _missing_analytics("build", exc) from None

    wanted = _split_sources(sources)
    _warn_multiple_versions(input, wanted)
    wanted_datasets = (
        [part.strip() for part in dataset.split(",") if part.strip()]
        if dataset
        else None
    )
    try:
        with _log_to_stderr():
            summary = lake_build(
                input,
                lake,
                sources=wanted,
                datasets=wanted_datasets,
                profile=profile,
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
        # extra opcional `lake`: ausente do ambiente de dev padrão
        from quantilica.analytics.lake_audit import (  # pyright: ignore[reportMissingImports]
            lake_audit,
        )
    except ImportError as exc:
        raise _missing_analytics("audit", exc) from None

    wanted = _split_sources(sources)
    known = _read_list(exceptions) if exceptions is not None else []
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
