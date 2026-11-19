"""Resumo estruturado de uma coleta (``--summary-json``).

O ``sync`` imprime ao final uma linha de texto (``Concluído: X/Y arquivo(s)
baixado(s)``) pensada para quem lê o terminal. Quem agenda a coleta e precisa
dos números (um agendador, um painel, um teste) não deveria extraí-los dessa
linha: com ``--summary-json ARQUIVO`` o comando grava, ao terminar, um objeto
JSON com os mesmos contadores, o código de saída e a versão do esquema.

Contrato (``schema_version`` 1; campos novos podem ser acrescentados sem mudar
a versão, a remoção ou a mudança de significado de um campo muda a versão):

``schema_version``
    Inteiro. ``1``.
``tool`` / ``tool_version``
    ``"quantilica-cli"`` e a versão instalada (``null`` se indeterminada).
``fetcher``
    Nome do fetcher que executou o comando.
``command``
    Nome do comando (``"sync"``).
``status``
    ``"success"`` (exit 0), ``"partial"`` (exit ``EXIT_SYNC_PARTIAL``, parte
    baixou e parte falhou), ``"interrupted"`` (130 ou 143) ou ``"failed"``.
``exit_code``
    Código de saída do comando.
``dry_run``
    ``true`` quando o comando só listou as entradas (``--dry-run``).
``started_at`` / ``finished_at``
    Instantes ISO 8601 em UTC (``...Z``) e ``duration_seconds``.
``total``
    Entradas tentadas (o denominador de ``Concluído: X/Y``).
``succeeded``
    Entradas concluídas sem erro (o numerador de ``Concluído: X/Y``); inclui
    as que já estavam em dia.
``failed``
    Entradas com erro.
``downloaded``, ``skipped``, ``bytes_downloaded``
    Do que ``succeeded`` foi transferência de fato (``downloaded``), o que
    estava em dia (``skipped``) e os bytes transferidos. Vêm do callback de
    progresso do cliente de download; ``null`` quando o fetcher substitui o
    download por código próprio e a medição não existe.
``errors``
    Lista de ``{"id", "message"}`` (até 100; ``errors_truncated`` indica corte).
``error``
    Motivo de uma falha que não é de arquivo (plano inválido, grupo
    desconhecido, exceção inesperada); ``null`` nos demais casos.

A gravação é atômica (temporário + renomeação) e **nunca muda o código de
saída do comando**: se o arquivo não puder ser gravado, o comando avisa em
``stderr`` e segue com o código que teria.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import os
import sys
import threading
from collections.abc import Callable
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from types import TracebackType
from typing import Any

import typer

__all__ = [
    "SUMMARY_SCHEMA_VERSION",
    "DownloadStats",
    "SummaryRecorder",
    "write_summary_json",
]

SUMMARY_SCHEMA_VERSION = 1

# Teto de erros no JSON e de caracteres por mensagem: um sync com milhares de
# falhas não pode gerar um arquivo gigante.
MAX_ERRORS = 100
MAX_MESSAGE_CHARS = 500

# Sinais que viram "interrupted" (130 = SIGINT, 143 = SIGTERM).
_INTERRUPTED_CODES = frozenset({130, 143})


class DownloadStats:
    """Contadores acumulados durante ``download_datasets`` (thread-safe).

    Cada entrada termina em :meth:`succeeded` ou :meth:`failed`. ``transferred``
    conta as que de fato abriram uma transferência (o cliente chamou o callback
    de progresso) e ``bytes`` soma o maior valor reportado por entrada.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.succeeded_count = 0
        self.failed_count = 0
        self.transferred = 0
        self.bytes = 0

    def track(self) -> _EntryProgress:
        """Callback de progresso de UMA entrada; encaminhe-o ao download."""
        return _EntryProgress()

    def succeeded(self, progress: _EntryProgress) -> None:
        with self._lock:
            self.succeeded_count += 1
            if progress.touched:
                self.transferred += 1
                self.bytes += progress.bytes

    def failed(self) -> None:
        with self._lock:
            self.failed_count += 1


class _EntryProgress:
    """Mede a transferência de uma entrada, repassando o progresso adiante."""

    def __init__(self) -> None:
        self.touched = False
        self.bytes = 0
        self._forward: Callable[[int, int], None] | None = None

    def forwarding(self, callback: Callable[[int, int], None] | None) -> _EntryProgress:
        self._forward = callback
        return self

    def __call__(self, downloaded: int, total: int) -> None:
        # O cliente chama (0, 0) ao abrir a transferência: já conta como tal.
        self.touched = True
        if downloaded > self.bytes:
            self.bytes = downloaded
        if self._forward is not None:
            self._forward(downloaded, total)


def _utc_iso(moment: dt.datetime) -> str:
    return moment.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _tool_version() -> str | None:
    try:
        return version("quantilica-cli")
    except PackageNotFoundError:
        return None


def write_summary_json(path: Path, payload: dict[str, Any]) -> bool:
    """Grava ``payload`` em ``path`` de forma atômica; ``False`` se falhar.

    Cria a pasta pai se preciso. Nunca levanta: a falha vira aviso em
    ``stderr``, porque o resumo é acessório e não pode derrubar a coleta.
    """
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)
    except (OSError, TypeError, ValueError) as exc:
        print(
            f"[Aviso] não foi possível gravar o resumo JSON em {path}: {exc}",
            file=sys.stderr,
        )
        return False
    return True


class SummaryRecorder:
    """Contexto que grava o ``--summary-json`` ao sair do comando.

    O corpo do comando preenche ``total``, ``succeeded``, ``errors`` etc. (ou o
    :attr:`stats` do download) e levanta ``typer.Exit`` como sempre; ao sair, o
    contexto grava o arquivo com o código que está saindo e **não suprime**
    nada. Com ``path`` ``None`` não faz nada.

    Args:
        path: Arquivo de saída (``None`` desliga).
        fetcher: Nome do fetcher.
        command: Nome do comando.
        stats_source: Devolve o :class:`DownloadStats` do download em curso
            (lido ao sair, para que uma interrupção ainda traga os contadores).
    """

    def __init__(
        self,
        path: Path | None,
        *,
        fetcher: str,
        command: str,
        stats_source: Callable[[], DownloadStats | None] | None = None,
    ) -> None:
        self.path = path
        self.fetcher = fetcher
        self.command = command
        self.stats_source = stats_source
        self.dry_run = False
        self.total: int | None = None
        self.succeeded: int | None = None
        self.errors: list[tuple[str, str]] | None = None
        self.error: str | None = None
        self._started = dt.datetime.now(dt.UTC)

    def __enter__(self) -> SummaryRecorder:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self.path is None:
            return None
        exit_code = 0
        if exc is not None:
            if isinstance(exc, typer.Exit):
                exit_code = exc.exit_code or 0
            elif isinstance(exc, KeyboardInterrupt):
                exit_code = 130
            elif isinstance(exc, SystemExit):
                code = exc.code
                exit_code = code if isinstance(code, int) else 1
            else:
                exit_code = 1
                if self.error is None:
                    self.error = f"{type(exc).__name__}: {exc}"[:MAX_MESSAGE_CHARS]
        with contextlib.suppress(Exception):  # o resumo nunca derruba o comando
            write_summary_json(self.path, self.build(exit_code))
        return None

    def build(self, exit_code: int) -> dict[str, Any]:
        """Monta o objeto do contrato para o ``exit_code`` dado."""
        finished = dt.datetime.now(dt.UTC)
        stats = self.stats_source() if self.stats_source is not None else None
        errors = list(self.errors or [])

        succeeded = self.succeeded
        failed: int | None = len(errors) if self.errors is not None else None
        downloaded = skipped = bytes_downloaded = None
        if stats is not None:
            # Contadores do download: valem também numa interrupção, em que o
            # retorno de download_datasets nunca chegou ao comando.
            if succeeded is None:
                succeeded = stats.succeeded_count
            if failed is None:
                failed = stats.failed_count
            downloaded = stats.transferred
            bytes_downloaded = stats.bytes
            skipped = max(succeeded - downloaded, 0)
        total = self.total
        if total is None and succeeded is not None and failed is not None:
            total = succeeded + failed

        if exit_code == 0:
            status = "success"
        elif exit_code == _partial_code():
            status = "partial"
        elif exit_code in _INTERRUPTED_CODES:
            status = "interrupted"
        else:
            status = "failed"

        return {
            "schema_version": SUMMARY_SCHEMA_VERSION,
            "tool": "quantilica-cli",
            "tool_version": _tool_version(),
            "fetcher": self.fetcher,
            "command": self.command,
            "status": status,
            "exit_code": exit_code,
            "dry_run": self.dry_run,
            "started_at": _utc_iso(self._started),
            "finished_at": _utc_iso(finished),
            "duration_seconds": round((finished - self._started).total_seconds(), 3),
            "total": total,
            "succeeded": succeeded,
            "failed": failed,
            "downloaded": downloaded,
            "skipped": skipped,
            "bytes_downloaded": bytes_downloaded,
            "errors": [
                {"id": str(eid), "message": str(msg)[:MAX_MESSAGE_CHARS]}
                for eid, msg in errors[:MAX_ERRORS]
            ],
            "errors_truncated": len(errors) > MAX_ERRORS,
            "error": self.error,
        }


def _partial_code() -> int:
    # Import tardio: sdk importa este módulo.
    from quantilica.cli.sdk import EXIT_SYNC_PARTIAL

    return EXIT_SYNC_PARTIAL
