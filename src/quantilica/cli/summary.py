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
    baixou e parte falhou), ``"interrupted"`` (130, 143 ou Ctrl-C fora do
    download, sempre com exit 130) ou ``"failed"``. Com o resumo
    pedido, SIGTERM vira ``SystemExit(143)`` durante o comando, para o arquivo
    ser gravado também no timeout.
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
``skipped_items``
    Lista de ``{"id", "reason"}`` (até 100) das entradas que o host deixou de
    baixar de propósito e que contam em ``succeeded``/``skipped``, não em
    ``failed``. Hoje o único motivo é ``"pending-publication"``: a entrada
    marcada com ``pending_publication`` respondeu 404 (a fonte ainda não
    publicou o período). Lista vazia quando não houve nenhuma.
``empty_groups``
    Grupos pedidos que devolveram zero entradas enquanto outros tinham
    entradas (aviso em WARNING, não erro), exceto os que o plugin declarou
    vazios por desenho (``expected_empty_groups``). Lista vazia no caso comum.
    Se o catálogo inteiro vier vazio o comando sai com 1, ``error`` diz
    ``catálogo vazio para <grupos>`` e este campo traz os mesmos grupos.
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
import signal
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
        self.skipped_items: list[tuple[str, str]] = []

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

    def skipped(self, entry_id: str, reason: str) -> None:
        """Entrada que não foi baixada de propósito (conta como sucesso).

        Entra em ``succeeded`` sem ter aberto transferência, portanto também em
        ``skipped``; ``reason`` fica em ``skipped_items``.
        """
        with self._lock:
            self.succeeded_count += 1
            self.skipped_items.append((entry_id, reason))


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
        self.empty_groups: list[str] = []
        self.interrupted = False
        self._started = dt.datetime.now(dt.UTC)
        self._previous_sigterm: Any = None
        self._sigterm_installed = False

    def __enter__(self) -> SummaryRecorder:
        # Com o resumo pedido, SIGTERM (o 'timeout' do agendador, um 'kill')
        # vira SystemExit(143): sem isso o processo morre sem passar pelo
        # __exit__ e o arquivo não é gravado, justamente no timeout. Só na
        # thread principal; o handler anterior volta ao sair.
        main = threading.current_thread() is threading.main_thread()
        if self.path is not None and main:
            with contextlib.suppress(ValueError, OSError):
                self._previous_sigterm = signal.signal(
                    signal.SIGTERM, _raise_terminated
                )
                self._sigterm_installed = True
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._sigterm_installed:
            with contextlib.suppress(ValueError, OSError, TypeError):
                signal.signal(signal.SIGTERM, self._previous_sigterm or signal.SIG_DFL)
            self._sigterm_installed = False
        if self.path is None:
            return None
        exit_code = 0
        if exc is not None:
            if isinstance(exc, typer.Exit):
                exit_code = exc.exit_code or 0
            elif isinstance(exc, KeyboardInterrupt):
                # Fora do download (que já vira Exit(130)): o código que o
                # typer devolve ao Ctrl-C varia com a versão (130 ou 1 do
                # Abort do click), então fixo 130 aqui e o converto ao sair.
                exit_code = 130
                self.interrupted = True
            elif isinstance(exc, SystemExit):
                code = exc.code
                exit_code = code if isinstance(code, int) else 1
            else:
                exit_code = 1
                if self.error is None:
                    self.error = f"{type(exc).__name__}: {exc}"[:MAX_MESSAGE_CHARS]
        with contextlib.suppress(Exception):  # o resumo nunca derruba o comando
            write_summary_json(self.path, self.build(exit_code))
        if isinstance(exc, KeyboardInterrupt):
            raise typer.Exit(130) from None  # o JSON e o processo dizem o mesmo
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
        skipped_items = list(stats.skipped_items) if stats is not None else []
        total = self.total
        if total is None and succeeded is not None and failed is not None:
            total = succeeded + failed

        if exit_code == 0:
            status = "success"
        elif exit_code == _partial_code():
            status = "partial"
        elif self.interrupted or exit_code in _INTERRUPTED_CODES:
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
            "skipped_items": [
                {"id": str(eid), "reason": str(reason)}
                for eid, reason in skipped_items[:MAX_ERRORS]
            ],
            "empty_groups": list(self.empty_groups),
            "errors": [
                {"id": str(eid), "message": str(msg)[:MAX_MESSAGE_CHARS]}
                for eid, msg in errors[:MAX_ERRORS]
            ],
            "errors_truncated": len(errors) > MAX_ERRORS,
            "error": self.error,
        }


def _raise_terminated(signum: int, frame: Any) -> None:
    raise SystemExit(143)


def _partial_code() -> int:
    # Import tardio: sdk importa este módulo.
    from quantilica.cli.sdk import EXIT_SYNC_PARTIAL

    return EXIT_SYNC_PARTIAL
