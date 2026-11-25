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
import re
from collections.abc import Iterator
from contextlib import contextmanager
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Annotated, Any

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


_SHA256 = re.compile(r"[0-9a-fA-F]{64}")
_NAME_STAMP = re.compile(r"@(\d{8})(?:T(\d{6}))?")
_RESOURCE_UUID = re.compile(
    r"/resource/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/",
    re.IGNORECASE,
)


def _in_place(src: Path, dst: Path) -> bool:
    """``dst`` já é ``src``: o mesmo arquivo ou uma cópia dele (tamanho e mtime)."""
    import os

    if not os.path.lexists(dst):
        return False
    try:
        if os.path.samefile(src, dst):
            return True
        if os.path.islink(dst) or not os.path.isfile(dst):
            return False
        a, b = src.stat(), dst.stat()
    except OSError:
        return False
    # copy2 preserva o mtime; 2 s de folga para sistemas que o arredondam (FAT)
    return a.st_size == b.st_size and abs(a.st_mtime_ns - b.st_mtime_ns) < 2_000_000_000


def _place_file(src: Path, dst: Path, mode: str) -> str:
    """Põe ``src`` em ``dst`` pelo ``mode``, recuando hardlink → symlink → cópia.

    O vínculo nasce com nome temporário e é renomeado sobre ``dst``: quem lê
    nunca vê o arquivo faltando. Devolve o modo que funcionou.
    """
    import os
    import shutil

    chain = ["hardlink", "symlink", "copy"]
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.is_dir() and not dst.is_symlink():
        shutil.rmtree(dst)
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
        return attempt
    raise AssertionError("unreachable")  # pragma: no cover


def _read_manifest_url_sha(path: Path) -> tuple[str, str]:
    """``(url, sha256)`` do manifesto ao lado de ``path``; vazios sem manifesto."""
    from quantilica.core.manifests import read_manifest_sidecar

    manifest = read_manifest_sidecar(path)
    if manifest is None:
        return "", ""
    return (manifest.url or "").strip(), (manifest.sha256 or "").lower()


def _resource_identity(rel: str, url: str) -> tuple[str, str]:
    """Identidade do recurso de uma versão (a mesma regra do conversor de seleção
    própria e do ledger): na mesma pasta, o id do recurso no CKAN (da URL) ou a
    URL sem consulta; sem URL, o nome sem carimbo."""
    from urllib.parse import urlsplit

    from quantilica.core.lake import file_key

    folder = PurePosixPath(rel).parent.as_posix()
    if not url:
        return folder, "n:" + file_key(PurePosixPath(rel).name).lower()
    match = _RESOURCE_UUID.search(url)
    if match:
        return folder, "r:" + match[1].lower()
    parts = urlsplit(url)
    return folder, (
        f"u:{parts.scheme.lower()}://{parts.netloc.lower()}{parts.path.rstrip('/')}"
    )


def _name_stamp(rel: str) -> str:
    match = _NAME_STAMP.search(PurePosixPath(rel).name)
    return f"{match[1]}{match[2] or '000000'}" if match else ""


def _own_source_exclusions(
    root: Path, name: str, rels: list[str], exclude: list[str]
) -> tuple[set[str], set[str]]:
    """Exclusões de ``--exclude`` numa fonte de seleção própria.

    Devolve ``(excluídos, anteriores)``: os arquivos de dados que o ``--exclude``
    aponta (caminho ``<fonte>/<rel>`` ou sha256) e as versões anteriores do mesmo
    recurso que saem junto. Como o conversor converte a versão mais nova de cada
    recurso entre as que estão na visão, excluir a mais nova (todas as versões do
    carimbo mais novo) traria a anterior de volta; então as anteriores saem
    também, a regra do ledger (exclusão comum faz o mês sumir). O recurso é o da
    URL do manifesto; a "mais nova" é a do carimbo ``@AAAAMMDD`` do nome.
    """
    from quantilica.core.lake import MANIFEST_SIDECAR_SUFFIX

    digests: set[str] = set()
    paths: set[str] = set()
    prefix = f"{name}/"
    for entry in exclude:
        text = entry.strip().replace("\\", "/")
        if _SHA256.fullmatch(text):
            digests.add(text.lower())
        elif text.startswith(prefix):
            paths.add(PurePosixPath(text[len(prefix) :]).as_posix())
    if not digests and not paths:
        return set(), set()
    data = [r for r in rels if not r.endswith(MANIFEST_SIDECAR_SUFFIX)]
    info = {r: _read_manifest_url_sha(root / r) for r in data}
    excluded: set[str] = set()
    for rel in data:
        sha = info[rel][1]
        if digests and not sha:
            from quantilica.core.files import sha256_file

            sha = sha256_file(root / rel).lower()
        if rel in paths or (sha and sha in digests):
            excluded.add(rel)
    if not excluded:
        return set(), set()
    groups: dict[tuple[str, str], list[str]] = {}
    for rel in data:
        groups.setdefault(_resource_identity(rel, info[rel][0]), []).append(rel)
    behind: set[str] = set()
    for members in groups.values():
        if not excluded.intersection(members):
            continue
        newest = max(_name_stamp(r) for r in members)
        top = [r for r in members if _name_stamp(r) == newest]
        if all(r in excluded for r in top):
            behind.update(r for r in members if _name_stamp(r) != newest)
    return excluded, behind - excluded


def _sync_all_files(
    source: Path, dest: Path, mode: str, skip: set[str] | None = None
) -> dict[str, int]:
    """Sincroniza ``dest`` com todos os arquivos de ``source``, de todas as versões.

    Incremental: liga o que falta, mantém o que já está no lugar (o mesmo
    arquivo, ou uma cópia de mesmo tamanho e mtime) e remove só o que sumiu do
    bruto (ou entrou em ``skip``, junto do manifesto), mais as pastas que ficaram
    vazias. Nunca apaga a pasta para recriá-la. Mesmo recuo de
    ``materialize_view`` (hardlink, symlink, cópia); ignora ``*.tmp``. Devolve
    ``files`` (total na pasta), um contador por modo, ``kept`` e ``removed``.
    """
    import os

    from quantilica.core.lake import MANIFEST_SIDECAR_SUFFIX

    drop = set(skip or ())
    drop |= {f"{rel}{MANIFEST_SIDECAR_SUFFIX}" for rel in drop}
    wanted = [rel for rel in _files_to_link(source) if rel not in drop]
    counts = {"hardlink": 0, "symlink": 0, "copy": 0, "kept": 0, "removed": 0}
    for rel in sorted(wanted):
        src, dst = source / rel, dest / rel
        if _in_place(src, dst):
            counts["kept"] += 1
        else:
            counts[_place_file(src, dst, mode)] += 1
    counts["files"] = len(wanted)
    keep = set(wanted)
    if dest.is_dir():
        for current, dirs, names in os.walk(dest, topdown=False):
            base = Path(current).relative_to(dest)
            for entry in names:
                if (base / entry).as_posix() not in keep:
                    (Path(current) / entry).unlink()
                    counts["removed"] += 1
            for entry in dirs:
                directory = Path(current) / entry
                if directory.is_symlink():
                    directory.unlink()
                    counts["removed"] += 1
                else:
                    try:
                        directory.rmdir()
                    except OSError:  # não vazia: guarda parte da visão
                        pass
    return counts


def _check_no_overlap(raw: Path, out: Path) -> None:
    """``raw`` e ``out`` não podem ser a mesma pasta nem uma conter a outra."""
    raw_real, out_real = raw.resolve(), out.resolve()
    if (
        raw_real == out_real
        or raw_real in out_real.parents
        or out_real in raw_real.parents
    ):
        raise ValueError(f"raw ({raw}) and out ({out}) must not overlap.")


def _add_summaries(parts: list[Any]) -> dict[str, int]:
    keys = ("files", "hardlink", "symlink", "copy", "kept", "excluded", "removed")
    return {k: sum(getattr(p, k) for p in parts) for k in keys}


def _select_with_own(
    raw: Path,
    out: Path,
    mode: str,
    exclude: list[str],
    own: list[str],
) -> tuple[dict[str, int], dict[str, dict[str, int]]]:
    """``lake select`` quando há fontes de seleção própria.

    Cada pasta de fonte tem a sua rodada: as comuns passam por ``materialize_view``
    (só a versão vigente de cada arquivo) e as de seleção própria são sincronizadas
    de forma incremental (``_sync_all_files``). Assim ``out/<fonte>`` das últimas
    nunca é apagado para ser recriado (nem some da visão durante a execução, nem
    copia tudo de novo em ``--mode copy``). Entradas de ``--exclude`` valem nas duas;
    nas de seleção própria, excluir a versão mais nova de um recurso tira também as
    anteriores do mesmo recurso (``_own_source_exclusions``).
    """
    import os
    import shutil

    from quantilica.core.lake import materialize_view

    sources = sorted(e.name for e in raw.iterdir() if e.is_dir())
    stray = sorted(e.name for e in raw.iterdir() if not e.is_dir())
    if stray:
        err_console.print(
            f"[yellow]Aviso:[/yellow] {len(stray)} arquivo(s) soltos na raiz de "
            f"{escape(str(raw))} (por exemplo: {escape(', '.join(stray[:3]))}) ficam "
            "fora da visão: o bruto é uma pasta por fonte.",
            highlight=False,
        )
    parts: list[Any] = []
    removed_extra = 0
    for name in sources:
        if name in own:
            continue
        prefix = f"{name}/"
        local: list[str] = []
        for entry in exclude:
            text = entry.strip().replace("\\", "/")
            if _SHA256.fullmatch(text):
                local.append(text)
            elif text.startswith(prefix):
                local.append(text[len(prefix) :])
        parts.append(
            materialize_view(raw / name, out / name, mode=mode, exclude=local or None)
        )
    # pastas da visão que não são mais fonte do bruto
    if out.is_dir():
        for entry in out.iterdir():
            if entry.name in sources:
                continue
            if entry.is_dir() and not entry.is_symlink():
                removed_extra += sum(len(f) for _, _, f in os.walk(entry)) or 1
                shutil.rmtree(entry)
            else:
                entry.unlink()
                removed_extra += 1
    totals = _add_summaries(parts)
    totals["removed"] += removed_extra
    linked: dict[str, dict[str, int]] = {}
    for name in own:
        rels = _files_to_link(raw / name)
        gone, behind = _own_source_exclusions(raw / name, name, rels, exclude)
        counts = _sync_all_files(raw / name, out / name, mode, gone | behind)
        counts["excluded"] = len(gone)
        counts["behind"] = len(behind)
        if behind:
            examples = ", ".join(sorted(f"{name}/{r}" for r in behind)[:3])
            err_console.print(
                f"[yellow]Aviso:[/yellow] --exclude tirou a versão mais nova de "
                f"{len(gone)} recurso(s) de {escape(name)}; {len(behind)} versão(ões) "
                f"anterior(es) do mesmo recurso saem junto (por exemplo: "
                f"{escape(examples)}), para o conversor não converter a anterior.",
                highlight=False,
            )
        linked[name] = counts
        for key in ("hardlink", "symlink", "copy", "kept", "removed", "files"):
            totals[key] += counts[key]
        totals["excluded"] += counts["excluded"]
    return totals, linked


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
    com todas as versões, sem seleção, sincronizadas de forma incremental (a
    pasta delas na visão nunca é apagada para ser recriada). ``--exclude`` vale
    para elas: excluir a versão mais nova de um recurso tira também as
    anteriores do mesmo recurso.
    """
    try:
        from quantilica.core.lake import materialize_view
    except ImportError as exc:
        raise _fail(
            f"'quantilica lake select' requer um quantilica-core mais novo "
            f"({exc}). Atualize com: {CORE_HINT}"
        ) from None

    excluded = _read_list(exclude) if exclude is not None else []
    own: list[str] = _self_selecting_sources(raw)
    linked: dict[str, dict[str, int]] = {}
    try:
        _check_no_overlap(raw, out)
        if own:
            totals, linked = _select_with_own(raw, out, mode.value, excluded, own)
        else:
            totals = _add_summaries(
                [materialize_view(raw, out, mode=mode.value, exclude=excluded or None)]
            )
    except (ValueError, OSError) as exc:
        raise _fail(str(exc)) from exc

    console.print(f"[green]✓[/green] Visão em [dim]{out}[/dim]", highlight=False)
    typer.echo(f"  arquivos:   {totals['files']}")
    typer.echo(
        f"  criados:    hardlink={totals['hardlink']} symlink={totals['symlink']} "
        f"copy={totals['copy']}"
    )
    typer.echo(f"  mantidos:   {totals['kept']}")
    typer.echo(f"  excluídos:  {totals['excluded']}")
    typer.echo(f"  removidos:  {totals['removed']}")
    for name, counts in linked.items():
        line = (
            f"  {name}: seleção própria do conversor, {counts['files']} arquivo(s) "
            "de todas as versões (sem escolher a vigente)"
        )
        if counts["behind"]:
            line += f"; {counts['behind']} versão(ões) anterior(es) retirada(s)"
        typer.echo(line)


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
