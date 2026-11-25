"""Visão vigente do bruto para quem lê uma pasta de arquivos brutos.

O bruto guarda versões carimbadas (``nome@AAAAMMDD.ext``). Um conversor que
recebe a pasta inteira converteria todas elas. ``vigente_view`` entrega ao
conversor uma árvore só com a versão vigente de cada arquivo (a escolha é de
``quantilica.core.lake.select_current``), montada com links numa pasta
temporária e removida ao final, inclusive em erro.

A visão é opcional por conversor. Um conversor que escolhe a versão vigente por
conta própria (por exemplo, pelo recurso do manifesto, e não pelo nome do
arquivo) declara ``selects_versions = True`` no objeto do entry point
``quantilica.lake`` e na função que o fetcher registra em ``convert``; quem
chama então lhe entrega a pasta original (``converter_selects_versions``).
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import socket
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

from quantilica.core.lake import (
    MANIFEST_SIDECAR_SUFFIX,
    materialize_view,
    select_current,
)

SCRATCH_ENV = "QUANTILICA_LAKE_SCRATCH"
_TEMP_SUFFIX = ".tmp"
_VIEW_PREFIX = ".quantilica-vigente-"  # oculta: não aparece em listagens do scratch
_LEGACY_VIEW_PREFIX = "quantilica-vigente-"  # prefixo antigo (sem ponto), só na limpeza
_OWNER_FILE = "owner.json"
_ORPHAN_SECONDS = 7 * 24 * 3600  # visão sem owner.json só some depois disso


class ViewUnavailableError(RuntimeError):
    """A visão vigente não pôde ser montada sem copiar dados."""


_MAX_UNWRAP = 8  # profundidade máxima ao seguir partial/__wrapped__


def _declares_selects_versions(obj: object, depth: int = 0) -> bool:
    """Se ``obj``, seu dono (método ligado), ``partial.func`` ou ``__wrapped__``
    declara ``selects_versions = True``."""
    if obj is None or depth > _MAX_UNWRAP:
        return False
    if getattr(obj, "selects_versions", False) is True:
        return True
    return any(
        _declares_selects_versions(getattr(obj, attr, None), depth + 1)
        for attr in ("__self__", "func", "__wrapped__")
    )


def converter_selects_versions(*candidates: object) -> bool:
    """Se algum dos objetos declara ``selects_versions = True``.

    Passe a função registrada em ``convert`` e/ou o objeto do entry point
    ``quantilica.lake``. A declaração também vale quando está no objeto dono de
    um método ligado, na função embrulhada por ``functools.partial`` (``.func``)
    ou na decorada com ``functools.wraps`` (``__wrapped__``).
    """
    return any(_declares_selects_versions(candidate) for candidate in candidates)


def _relative_files(root: Path) -> list[str]:
    rels: list[str] = []
    for current, _, names in os.walk(root):
        base = Path(current).relative_to(root)
        rels.extend((base / name).as_posix() for name in names)
    return rels


def superseded_versions(root: Path) -> list[str]:
    """Arquivos de ``root`` que não são a versão vigente do seu arquivo.

    Caminhos relativos, em ordem. Manifestos e ``*.tmp`` não contam. Vazio
    quando nenhum arquivo tem mais de uma versão.
    """
    rels = _relative_files(root)
    data = [
        rel
        for rel in rels
        if not rel.endswith(MANIFEST_SIDECAR_SUFFIX) and not rel.endswith(_TEMP_SUFFIX)
    ]
    current = set(select_current(rels))
    return sorted(rel for rel in data if rel not in current)


def scratch_base() -> Path:
    """Pasta onde a visão é montada: ``QUANTILICA_LAKE_SCRATCH`` ou o temporário
    do sistema (``TMPDIR``)."""
    configured = os.environ.get(SCRATCH_ENV)
    if configured:
        base = Path(configured)
        base.mkdir(parents=True, exist_ok=True)
        return base
    return Path(tempfile.gettempdir())


def _pid_alive(pid: int) -> bool:
    """Se ``pid`` é um processo vivo neste host.

    No Windows ``os.kill(pid, 0)`` equivale a Ctrl+C e interromperia o grupo de
    processos do console: lá a consulta é pelo ``OpenProcess``.
    """
    if pid <= 0:
        return False
    if os.name == "nt":
        return _pid_alive_windows(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:  # PermissionError: existe, é de outro usuário
        return True
    return True


def _pid_alive_windows(pid: int) -> bool:  # pragma: no cover - só no Windows
    import ctypes
    from ctypes import wintypes

    query_limited_information = 0x1000
    still_active = 259
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)  # pyright: ignore[reportAttributeAccessIssue]
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = k32.OpenProcess(query_limited_information, False, pid)
    if not handle:
        return ctypes.get_last_error() == 5  # pyright: ignore[reportAttributeAccessIssue]
    try:
        code = wintypes.DWORD()
        if not k32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return True
        return code.value == still_active
    finally:
        k32.CloseHandle(handle)


def _is_orphan(entry: Path, now: float) -> bool:
    """Visão deixada por um processo que não existe mais neste host.

    Com ``owner.json``: o pid dono (do mesmo host) está morto. Sem ele (visão de
    uma versão anterior, ou criada neste instante e ainda sem o arquivo): só
    depois de 7 dias. Visão de outro host ou de processo vivo fica.
    """
    try:
        try:
            owner = json.loads((entry / _OWNER_FILE).read_text())
            pid, host = int(owner["pid"]), str(owner["hostname"])
        except (OSError, ValueError, KeyError, TypeError):
            return now - entry.stat().st_mtime >= _ORPHAN_SECONDS
        return host == socket.gethostname() and not _pid_alive(pid)
    except OSError:
        return False


def remove_orphan_views(base: Path) -> int:
    """Remove de ``base`` as visões ``.quantilica-vigente-*`` de processos mortos.

    Reconhece também o prefixo antigo, sem ponto (``quantilica-vigente-*``).
    Devolve quantas removeu. Só mexe nas pastas desses prefixos, e ``rmtree`` não
    segue links: os arquivos do bruto nunca são tocados.
    """
    if not base.is_dir():
        return 0
    now = time.time()
    removed = 0
    entries = [
        *base.glob(f"{_VIEW_PREFIX}*"),
        *base.glob(f"{_LEGACY_VIEW_PREFIX}*"),
    ]
    for entry in entries:
        if entry.is_dir() and not entry.is_symlink() and _is_orphan(entry, now):
            shutil.rmtree(entry, ignore_errors=True)
            removed += 1
    return removed


def _first_current_file(root: Path) -> Path | None:
    current = select_current(_relative_files(root))
    return root / current[0] if current else None


def _can_link(source: Path | None, folder: Path) -> bool:
    """Se dá para ligar (hardlink ou symlink) arquivos de ``source`` em ``folder``."""
    if source is None:
        return True
    probe = folder / ".probe"
    try:
        for make in (os.link, os.symlink):
            try:
                make(source, probe)
            except OSError:
                continue
            return True
        return False
    finally:
        probe.unlink(missing_ok=True)


def _overlaps(raw: Path, view: Path) -> bool:
    """``raw`` e ``view`` são a mesma pasta ou uma contém a outra (já resolvidas)."""
    raw_real, view_real = raw.resolve(), view.resolve()
    return (
        raw_real == view_real
        or raw_real in view_real.parents
        or view_real in raw_real.parents
    )


@contextlib.contextmanager
def vigente_view(root: Path, *, skip: bool = False) -> Iterator[tuple[Path, int]]:
    """Entrega ``(pasta, n)``: a pasta que o conversor deve ler e quantos
    arquivos de versões antigas ela deixa de fora.

    ``skip=True`` (conversor que declara ``selects_versions``) devolve ``root`` e
    ``0`` sem olhar a pasta. Sem nenhum arquivo com mais de uma versão devolve
    ``root`` e ``0``, sem montar nada. Caso contrário monta a visão com
    hardlinks ou symlinks e a remove ao sair, também quando o bloco levanta.
    Nunca copia dados: se o scratch não aceita link (volume diferente e sem
    privilégio de symlink) ou se o scratch está dentro da pasta do bruto
    levanta ``ViewUnavailableError``; aponte ``QUANTILICA_LAKE_SCRATCH`` para uma
    pasta do mesmo volume do bruto e fora dela, ou passe a pasta da visão
    vigente pronta (``quantilica lake select``).
    """
    if skip or not root.is_dir():
        yield root, 0
        return
    old = superseded_versions(root)
    if not old:
        yield root, 0
        return
    base = scratch_base()
    remove_orphan_views(base)
    tmp = Path(tempfile.mkdtemp(prefix=_VIEW_PREFIX, dir=base))
    try:
        (tmp / _OWNER_FILE).write_text(
            json.dumps({"pid": os.getpid(), "hostname": socket.gethostname()})
        )
        if not _can_link(_first_current_file(root), tmp):
            raise ViewUnavailableError(
                f"não foi possível ligar (hardlink ou symlink) os arquivos de {root} "
                f"em {base}, e a visão vigente não copia dados. Aponte "
                f"{SCRATCH_ENV} para uma pasta do mesmo volume do bruto (e fora da "
                "pasta do bruto), ou passe a pasta da visão vigente pronta "
                "('quantilica lake select')."
            )
        view = tmp / "visao" / (root.absolute().name or "bruto")
        # Sobreposição conferida aqui, às claras: um ValueError qualquer do
        # materialize_view não pode ser mostrado como "scratch dentro do bruto".
        if _overlaps(root, view):
            raise ViewUnavailableError(
                f"a pasta de trabalho da visão vigente ({base}) se sobrepõe ao "
                f"bruto ({root}). Aponte {SCRATCH_ENV} para uma pasta do mesmo "
                "volume do bruto, mas fora da pasta do bruto."
            )
        summary = materialize_view(root, view)
        if summary.copy:  # pragma: no cover - a sonda acima já falharia antes
            raise ViewUnavailableError(
                f"a visão vigente de {root} precisaria copiar {summary.copy} "
                "arquivo(s); passe a pasta da visão vigente pronta "
                "('quantilica lake select')."
            )
        yield view, len(old)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
