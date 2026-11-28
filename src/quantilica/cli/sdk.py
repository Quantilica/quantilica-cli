"""SDK for building Quantilica data fetchers.

Provides the FetcherApp class which eliminates boilerplate across fetchers.
"""

from __future__ import annotations

import concurrent.futures
import contextlib
import dataclasses
import datetime as dt
import email.utils
import inspect
import json
import os
import re
import sys
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from importlib.metadata import entry_points
from pathlib import Path
from typing import Annotated, Any, cast

import typer
from quantilica.core.exceptions import FetchError
from quantilica.core.ftp import FtpClient
from quantilica.core.http import (
    DEFAULT_RETRY_EXCEPTIONS,
    HttpClient,
    HttpStatusError,
    ProgressCallback,
    is_remote_more_recent,
    resolve_verify_from_env,
)
from quantilica.core.logging import get_logger
from quantilica.core.manifests import manifest_sidecar_path
from quantilica.core.registry import FetcherInfo
from quantilica.core.storage import (
    StampedDataRepository,
    expected_format_for_path,
    format_partition_token,
)
from rich.console import Console, Group
from rich.live import Live
from rich.markup import escape
from rich.rule import Rule
from rich.table import Table

from quantilica.cli.summary import (
    SUMMARY_SCHEMA_VERSION,
    DownloadStats,
    SummaryRecorder,
)
from quantilica.cli.ui import (
    ProgressPool,
    get_console,
    graceful_executor,
    make_batch_progress,
    make_download_progress,
    setup_rich_logging,
)
from quantilica.cli.vigente import (
    ViewUnavailableError,
    converter_selects_versions,
    vigente_view,
)

logger = get_logger(__name__)


def _sync_accepts_summary_json(callback: Callable[..., Any] | None) -> bool:
    """``True`` se o callback do ``sync`` tem o parâmetro ``summary_json``.

    ``inspect.signature`` segue ``__wrapped__``, então um sync embrulhado com
    ``functools.wraps`` vale pela assinatura do original.
    """
    if callback is None:
        return False
    try:
        return "summary_json" in inspect.signature(callback).parameters
    except (TypeError, ValueError):
        return False


def derive_fetcher_info(name: str) -> FetcherInfo:
    """Deriva um :class:`FetcherInfo` mínimo do nome do fetcher.

    Usado quando o fetcher ainda não declara ``info``: o id é o ``name`` sem o
    sufixo ``-fetcher``, normalizado (``bcb_sgs-fetcher`` -> ``bcb-sgs``).
    """
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower().removesuffix("-fetcher"))
    slug = slug.strip("-") or "fetcher"
    return FetcherInfo(id=slug, nome=slug)


__all__ = [
    "EXIT_SYNC_PARTIAL",
    "LISTING_ERROR_REASON",
    "PENDING_PUBLICATION_KEY",
    "PENDING_PUBLICATION_REASON",
    "SYNC_WORKERS_ENVVAR",
    "derive_fetcher_info",
    "CheckPlan",
    "CheckPlanItem",
    "DataRepository",
    "FetcherApp",
    "SyncPlan",
    "SyncPlanItem",
    "default_client",
    "default_path_builder",
    "plan_listing_errors",
    "make_resolve_groups",
]


# Código de saída do ``sync`` quando parte dos arquivos baixou e parte falhou.
# O que baixou está íntegro em disco (o agendador pode seguir com a etapa
# seguinte); o código distinto de 0 faz a falha parcial aparecer como alerta. Se
# nada baixou, o código é 1 (falha total). 124/130/143 já são de timeout/sinal, e
# 75/78 (EX_TEMPFAIL/EX_CONFIG, do sysexits) ficam para quem agenda a execução.
EXIT_SYNC_PARTIAL = 3

# Marca na entrada do catálogo (``entry["pending_publication"] = True``): o
# período corrente é gerado antes de a fonte publicá-lo (virada de ano/mês). Um
# 404 nessa entrada (em todas as URLs dela) não é erro: o ``sync`` a conta como
# pulada, com este motivo, e o ``check`` a devolve como ``skip-unavailable``.
PENDING_PUBLICATION_KEY = "pending_publication"
PENDING_PUBLICATION_REASON = "pending-publication"

# Falha de descoberta vista pelo ``check``: o fetcher devolve, no lugar das entradas
# que não conseguiu listar, um item ``skip-unavailable`` com este motivo e a causa
# em ``entry["error"]`` (e ``entry["error_class"]``, se houver). O plano dos demais
# itens é gravado e o ``check`` sai 0; o ``sync --from-plan`` conta cada item como
# erro (saída 3 se algo baixou, 1 se nada).
LISTING_ERROR_REASON = "listing-error"

# Variável de ambiente que define ``--workers`` do ``sync``/``pipeline`` sem
# argumento extra na linha de comando (um agendador nem sempre consegue passar flags). A flag
# explícita vence a variável, que vence o padrão do comando.
SYNC_WORKERS_ENVVAR = "QUANTILICA_SYNC_WORKERS"


@dataclass(frozen=True)
class SyncPlanItem:
    """A single dataset file planned for synchronization.

    Args:
        dataset: Canonical dataset (group) identifier.
        partition: Human-readable partition label (e.g. ``2023-05``), or None.
        filename: The stamped file name for the entry.
        url: Remote URL for the resource.
        target: Local destination path.
        dataset_name: Optional human-readable dataset name.
    """

    dataset: str
    partition: str | None
    filename: str
    url: str
    target: Path
    dataset_name: str = ""


@dataclass(frozen=True)
class SyncPlan:
    """Preview plan for a synchronization run (``--dry-run``).

    Args:
        items: Planned :class:`SyncPlanItem` entries.
        skipped: Number of entries ignored (outside of coverage).
    """

    items: list[SyncPlanItem] = field(default_factory=list)
    skipped: int = 0

    def render_table(self, console: Console | None = None) -> None:
        """Render the plan as a Rich table with a summary footer.

        Args:
            console: Optional Rich Console to print to (defaults to the shared
                console from :mod:`quantilica.cli.ui`).
        """
        con = console or get_console()
        table = Table(
            "Dataset",
            "Partição",
            "Arquivo",
            "URL",
            title="Arquivos a baixar (dry-run)",
        )
        for item in self.items:
            table.add_row(
                item.dataset,
                item.partition or "—",
                item.filename,
                item.url,
            )
        con.print(table)
        con.print(
            f"Total: {len(self.items)} arquivos planejados. "
            f"{self.skipped} ignorados fora de cobertura."
        )


def _two_digits(value: Any) -> str:
    """Format a month/semester as two digits, accepting ``int`` or numeric ``str``.

    Catalogs built from filename regexes (e.g. PDET) carry the month as the
    captured string (``"03"``), while others use ``int``; ``f"{x:02d}"`` only
    works for the latter.
    """
    return f"{int(value):02d}"


def _partition_label(entry: dict[str, Any]) -> str | None:
    """Human-readable partition label for a dataset entry."""
    return format_partition_token(entry)


def _parse_head_date(value: str | None) -> dt.date | None:
    """Parse an HTTP Last-Modified header value into a date, or None."""
    if not value:
        return None
    with contextlib.suppress(Exception):
        return email.utils.parsedate_to_datetime(value).date()
    return None


def _short_remote(item: CheckPlanItem) -> str:
    """Compact remote description for table display (narrow terminals)."""
    date = _parse_head_date(item.remote_last_modified)
    remote = date.isoformat() if date else (item.remote_last_modified or "—")
    if item.remote_size:
        remote += f" ({item.remote_size} B)"
    return remote


# Probe Range: novas tentativas em status ou erro de transporte transitório
# (``DEFAULT_RETRY_EXCEPTIONS``) antes de desistir.
_PROBE_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
_PROBE_ATTEMPTS = 3
_PROBE_BACKOFF_S = 1.0


def _is_pending_publication(entry: Mapping[str, Any]) -> bool:
    """``True`` se a entrada traz a marca ``pending_publication``."""
    return bool(entry.get(PENDING_PUBLICATION_KEY))


def _is_not_found(exc: BaseException | None) -> bool:
    """``True`` se ``exc`` é um ``HttpStatusError`` 404 (ou foi causado por um).

    Segue só ``__cause__`` (``raise X from 404``), nunca ``__context__``: uma
    falha (tempo esgotado, 5xx) levantada enquanto um 404 era tratado não é um
    404 e não pode ser perdoada.
    """
    seen: set[int] = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        if isinstance(exc, HttpStatusError) and exc.status_code == 404:
            return True
        exc = exc.__cause__
    return False


class _ProbeNotFoundError(Exception):
    """O GET de probe Range respondeu 404 (URL removida no servidor)."""


@dataclass(frozen=True)
class CheckPlanItem:
    """Freshness verdict for a single dataset entry (no download performed).

    Args:
        dataset: Canonical dataset (group) identifier.
        id: Dataset entry identifier.
        url: Remote URL that was checked.
        partition: Human-readable partition label, or None.
        local_path: Local destination path the entry maps to.
        remote_etag: Remote ETag header, or None.
        remote_last_modified: Remote Last-Modified header, or None.
        remote_size: Remote Content-Length, or None.
        local_exists: Whether the artifact already exists locally — this
            covers either the raw file itself or its ephemeral ``.manifest.json``
            sidecar marker (file deleted for retention, sidecar still fresh).
        action: ``"download"``, ``"skip-up-to-date"`` or ``"skip-unavailable"``
            (todas as URLs da entrada responderam 404 no HEAD).
        reason: Machine-readable reason for the action.
        entry: Original dataset entry (allows ``sync --from-plan`` round-trip).
    """

    dataset: str
    id: str
    url: str
    partition: str | None
    local_path: str
    remote_etag: str | None
    remote_last_modified: str | None
    remote_size: int | None
    local_exists: bool
    action: str
    reason: str
    entry: dict[str, Any]


@dataclass(frozen=True)
class CheckPlan:
    """Freshness plan for a verification run (``check`` command).

    Args:
        fetcher: Name of the fetcher that produced the plan.
        output_dir: Output directory the plan was computed against.
        generated_at: ISO-8601 UTC timestamp of plan generation.
        items: Per-entry verdicts.
        expected_empty: ``True`` quando o catálogo veio vazio e todos os grupos
            pedidos estão em ``expected_empty_groups`` do plugin (vazio por
            desenho): o ``sync --from-plan`` aceita esse plano sem itens.
    """

    fetcher: str
    output_dir: str
    generated_at: str
    items: list[CheckPlanItem] = field(default_factory=list)
    expected_empty: bool = False

    def render_table(self, console: Console | None = None) -> None:
        """Render the plan as a Rich table with a summary footer."""
        con = console or get_console()
        table = Table(
            "Dataset",
            "ID",
            "Ação",
            "Motivo",
            "Remoto",
            title="Verificação remoto × local (check)",
        )
        for item in self.items:
            table.add_row(
                item.dataset,
                item.id,
                item.action,
                item.reason,
                _short_remote(item),
            )
        con.print(table)
        con.print(self.summary_line())

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON-serializable representation of the plan."""
        extra: dict[str, Any] = {"expected_empty": True} if self.expected_empty else {}
        return {
            "fetcher": self.fetcher,
            "output_dir": self.output_dir,
            "generated_at": self.generated_at,
            **extra,
            "items": [
                {
                    "dataset": it.dataset,
                    "id": it.id,
                    "url": it.url,
                    "partition": it.partition,
                    "local_path": it.local_path,
                    "remote_etag": it.remote_etag,
                    "remote_last_modified": it.remote_last_modified,
                    "remote_size": it.remote_size,
                    "local_exists": it.local_exists,
                    "action": it.action,
                    "reason": it.reason,
                    "entry": it.entry,
                }
                for it in self.items
            ],
        }

    def summary_line(self) -> str:
        """Return a one-line summary with the total and per-action counts."""
        counts: dict[str, int] = {}
        for item in self.items:
            counts[item.action] = counts.get(item.action, 0) + 1
        summary = ", ".join(f"{v} {k}" for k, v in sorted(counts.items()))
        return f"[bold]{len(self.items)}[/bold] verificado(s): {summary}."

    def to_json(self) -> str:
        """Serialize the plan to a JSON string (for ``sync --from-plan``)."""
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CheckPlan:
        """Rebuild a plan from :meth:`to_dict` output.

        Raises:
            ValueError: If the payload is not a valid check plan.
        """
        if not isinstance(data, dict) or not isinstance(data.get("items"), list):
            raise ValueError("not a check plan: missing 'items' list")
        items = []
        for raw in data["items"]:
            if not isinstance(raw, dict):
                raise ValueError("not a check plan: invalid item")
            try:
                entry = raw["entry"]
                action = raw["action"]
                eid = raw["id"]
            except KeyError as exc:
                raise ValueError(f"not a check plan: item missing {exc}") from exc
            if not isinstance(entry, dict) or action not in (
                "download",
                "skip-up-to-date",
                "skip-unavailable",
            ):
                raise ValueError("not a check plan: invalid item")
            items.append(
                CheckPlanItem(
                    dataset=str(raw.get("dataset", "")),
                    id=str(eid),
                    url=str(raw.get("url", "")),
                    partition=raw.get("partition"),
                    local_path=str(raw.get("local_path", "")),
                    remote_etag=raw.get("remote_etag"),
                    remote_last_modified=raw.get("remote_last_modified"),
                    remote_size=raw.get("remote_size"),
                    local_exists=bool(raw.get("local_exists", False)),
                    action=action,
                    reason=str(raw.get("reason", "")),
                    entry=entry,
                )
            )
        return cls(
            fetcher=str(data.get("fetcher", "")),
            output_dir=str(data.get("output_dir", "")),
            generated_at=str(data.get("generated_at", "")),
            items=items,
            expected_empty=bool(data.get("expected_empty", False)),
        )

    @classmethod
    def from_json(cls, payload: str) -> CheckPlan:
        """Parse a plan serialized with :meth:`to_json`.

        Raises:
            ValueError: If the payload is not valid JSON or not a plan.
        """
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ValueError(f"not a check plan: invalid JSON ({exc})") from exc
        return cls.from_dict(data)


def plan_listing_errors(plan: CheckPlan) -> list[tuple[str, str]]:
    """Falhas de descoberta do plano, como pares ``(id, mensagem)`` de erro do sync.

    Um item ``skip-unavailable`` com motivo ``listing-error`` é a falha de
    listagem que o ``check`` registrou; a mensagem é ``<error_class>: <error>``
    (ou só o ``error``) da entrada. Sem texto na entrada, cita a URL do item.
    """
    found: list[tuple[str, str]] = []
    for item in plan.items:
        if item.action != "skip-unavailable" or item.reason != LISTING_ERROR_REASON:
            continue
        error_class = item.entry.get("error_class")
        message = str(item.entry.get("error") or f"listagem de {item.url}")
        found.append(
            (
                item.id or item.dataset,
                f"{error_class}: {message}" if error_class else message,
            )
        )
    return found


def default_client() -> HttpClient:
    """Create a default HttpClient with standard configuration.

    TLS verification and retry budget can be overridden via environment:

    - ``QUANTILICA_CA_BUNDLE``: path to a custom CA bundle.
    - ``QUANTILICA_SSL_VERIFY``: ``0``/``false`` disables verification
      (ops escape hatch for hosts with broken chains — never default);
      a path uses it as CA bundle.
    - ``QUANTILICA_HTTP_ATTEMPTS`` / ``QUANTILICA_HTTP_TIMEOUT`` /
      ``QUANTILICA_HTTP_RETRY_DELAY``: retry budget overrides.
    - ``QUANTILICA_HTTP_MAX_RESUMES``: teto de retomadas com progresso de
      downloads interrompidos (default do ``HttpClient`` quando ausente).

    Returns:
        A pre-configured HttpClient instance (browser-like WAF headers + pooling).
    """
    verify = resolve_verify_from_env()
    if verify is False:
        logger.warning(
            "TLS verification disabled via QUANTILICA_SSL_VERIFY — "
            "use only for hosts with broken chains"
        )
    attempts = int(os.environ.get("QUANTILICA_HTTP_ATTEMPTS", "5"))
    timeout = float(os.environ.get("QUANTILICA_HTTP_TIMEOUT", "180.0"))
    retry_base_delay = float(os.environ.get("QUANTILICA_HTTP_RETRY_DELAY", "2.0"))
    # Só repassado quando definido: preserva o default do core.
    extra: dict[str, Any] = {}
    max_resumes = os.environ.get("QUANTILICA_HTTP_MAX_RESUMES", "").strip()
    if max_resumes:
        extra["max_resumes"] = int(max_resumes)
    return HttpClient(
        timeout=timeout,
        verify=verify,
        attempts=attempts,
        retry_base_delay=retry_base_delay,
        emulate_browser=True,
        **extra,
    )


_aviso_core_antigo = False
_aviso_core_lock = threading.Lock()


def _warn_core_sem_estado_compartilhado() -> None:
    """Avisa uma só vez por processo (não por thread) que o core é antigo."""
    global _aviso_core_antigo
    with _aviso_core_lock:
        if _aviso_core_antigo:
            return
        _aviso_core_antigo = True
    logger.warning(
        "quantilica-core sem share_host_state: os downloads paralelos não "
        "compartilham a política por host"
    )


class DataRepository(StampedDataRepository):
    """Canonical data repository layout shared by Quantilica fetchers.

    Inherits standard path resolution from
    :class:`quantilica.core.storage.BaseDataRepository`, storing files under
    ``{dataset_id}/`` directly with stamped filenames following the ecosystem
    convention ``{slug}[_{partition}]@{YYYYMMDD}.{ext}``.
    """


def default_path_builder(
    output_dir: Path,
    entry: dict[str, Any],
    last_modified: dt.date | None = None,
) -> Path:
    """Canonical path builder used when a fetcher does not provide one.

    Args:
        output_dir: The root output directory.
        entry: The dataset entry dictionary.
        last_modified: The last modified date of the dataset.

    Returns:
        Path: The destination path for the entry.
    """
    return DataRepository(output_dir).path_for_entry(entry, last_modified=last_modified)


def make_resolve_groups(
    groups_dict: dict[str, dict[str, Any]],
    aliases_dict: Mapping[str, list[str] | str],
) -> Callable[[Iterable[str] | None], list[str]]:
    """Build a resolver mapping group keys/aliases to canonical group IDs.

    Args:
        groups_dict: Dictionary of dataset groups and their metadata.
        aliases_dict: Dictionary of alias mappings to dataset groups.

    Returns:
        Callable: A function receiving group keys and/or aliases (or None for
        all groups) and returning the deduplicated canonical group IDs in
        declaration order. Raises ValueError on unknown keys.
    """
    groups = list(groups_dict)
    aliases = dict(aliases_dict)

    def resolve(keys: Iterable[str] | None = None) -> list[str]:
        resolved: list[str] = []
        for key in groups if keys is None else keys:
            target = aliases.get(key)
            expanded = (
                ([target] if isinstance(target, str) else list(target))
                if target is not None
                else ([key] if key in groups_dict else [])
            )
            if not expanded:
                raise ValueError(f"Grupo desconhecido: {key!r}")
            for canon in expanded:
                if canon not in resolved:
                    resolved.append(canon)
        return resolved

    return resolve


class FetcherApp:
    """Standard orchestrator for Quantilica fetchers.

    Args:
        name: Name of the fetcher (e.g., 'comex-fetcher').
        help: Help text for the CLI.
        groups_dict: Dictionary of dataset groups and their metadata.
        aliases_dict: Dictionary of alias mappings to dataset groups.
        list_datasets: Callback to list datasets given a group ID.
        path_builder: Callback to build the destination path. If None, uses
            :func:`default_path_builder` (canonical ``DataRepository`` layout).
        default_output: Default output directory path.
        client: HTTP or FTP client instance. Defaults to default_client().
        source_id: Source ID recorded in download manifests. Defaults to
            ``name`` without the ``-fetcher`` suffix. When ``default_output``
            is omitted it is derived as ``/data/{source_id}``.
        info: Fatos da fonte (:class:`~quantilica.core.registry.FetcherInfo`).
            Sem ele, um ``FetcherInfo`` mínimo é derivado de ``name`` e
            ``info_declared`` fica ``False`` (compatibilidade com fetchers antigos).
        build_default_commands: If True (default), registers the built-in
            ``sync`` and ``list`` commands. Set to False to register only
            custom commands via :meth:`attach_command`.
        expected_empty_groups: Grupos que, por desenho, podem devolver zero
            entradas de ``list_datasets`` (ex.: um grupo só de catálogo, sem
            arquivo para baixar). O ``sync``/``check`` sem essa declaração
            trata catálogo inteiro vazio como erro (saída 1, "catálogo vazio
            para <grupos>") e grupo vazio isolado como aviso (``empty_groups``
            no ``--summary-json``); os grupos declarados aqui ficam fora das
            duas regras. Só declare o que é vazio de verdade por desenho: um
            grupo que esvazia porque a fonte mudou precisa continuar falhando.

    Contrato de entrada do catálogo (cada ``dict`` devolvido por
    ``list_datasets``), além de ``url``/``id``/``group``:

    * ``pending_publication``: ``True`` marca a entrada do período corrente
      que o catálogo gera antes de a fonte publicá-lo (virada de ano/mês).
      Um 404 em todas as URLs dessa entrada deixa de ser erro: o ``sync`` a
      conta como pulada (motivo ``pending-publication``, log em INFO, sem
      efeito no código de saída) e o ``check`` a devolve como
      ``skip-unavailable``/``pending-publication``. Sem a marca, 404 segue
      sendo erro do arquivo. Outros erros (403, 5xx, rede) continuam erros
      mesmo com a marca.
    """

    # Contadores do último ``download_datasets`` (para o ``--summary-json``);
    # ``None`` se ainda não rodou ou se o fetcher substituiu o método.
    last_download_stats: DownloadStats | None = None

    def __init__(
        self,
        name: str,
        *,
        help: str = "",
        groups_dict: dict[str, dict[str, Any]],
        aliases_dict: dict[str, list[str]],
        list_datasets: Callable[[str], list[dict[str, Any]]],
        path_builder: Callable[[Path, dict[str, Any], dt.date | None], Path]
        | None = None,
        default_output: Path | None = None,
        source_id: str | None = None,
        client: HttpClient | FtpClient | None = None,
        freshness_policy: Any = None,
        info: FetcherInfo | None = None,
        build_default_commands: bool = True,
        expected_empty_groups: Iterable[str] = (),
    ):
        self.name = name
        self.help = help
        self.groups = groups_dict
        self.aliases = aliases_dict
        self.list_datasets = list_datasets
        self.path_builder = path_builder or default_path_builder
        # source_id explícito vence; senão deriva de `name` sem o sufixo
        # "-fetcher" (comportamento histórico).
        self.source_id = source_id or name.replace("-fetcher", "")
        self.default_output = default_output or Path(f"/data/{self.source_id}")
        self.client = client or default_client()
        self.freshness_policy = freshness_policy
        self.info_declared = info is not None
        self.info: FetcherInfo = info or derive_fetcher_info(name)

        self.expected_empty_groups: frozenset[str] = frozenset(expected_empty_groups)
        self.all_group_keys = list(self.groups.keys())
        self.all_keys = self.all_group_keys + list(self.aliases.keys())
        self.resolve_groups = make_resolve_groups(groups_dict, aliases_dict)

        # O objeto typer principal
        self.app = typer.Typer(help=help)
        # O entry point `quantilica.fetchers` aponta para o Typer; este atributo
        # deixa `quantilica registry export` chegar ao FetcherApp.
        self.app.fetcher_app = self  # type: ignore[attr-defined]
        if build_default_commands:
            self._build_commands()

    def attach_command(
        self,
        cmd_func: Callable[..., Any],
        name: str | None = None,
        **kwargs: Any,
    ) -> None:
        """Register a custom subcommand on the app.

        Lets fetchers add subcommands (e.g. ``convert``, ``pipeline``,
        ``archive``) cleanly, without subclassing and overriding
        ``_build_commands``.

        Args:
            cmd_func: The Typer command function to register.
            name: The command name. Defaults to the function name.
            **kwargs: Extra options forwarded to ``typer.Typer.command()``.
        """
        self.app.command(name=name, **kwargs)(cmd_func)

    def _analysis_hint(self) -> str:
        """Comando que instala o extra ``analysis`` no ambiente desta CLI.

        Usa a chave de registro do fetcher (a que ``quantilica install`` e o
        entry point conhecem, ex.: ``td``) e, se não a encontrar, o nome da
        distribuição, que ``quantilica install`` também aceita.

        Returns:
            Texto sem colchetes, seguro para ``console.print``.
        """
        key = self.name
        try:
            for ep in entry_points(group="quantilica.fetchers"):
                if ep.dist is not None and ep.dist.name == self.name:
                    key = ep.name
                    break
        except (
            Exception
        ):  # pragma: no cover - metadados quebrados não podem mascarar o erro
            pass
        return f"quantilica install {key} --extra analysis"

    def _default_lake(self) -> Path:
        """Raiz padrão do lake: ``lake_root()`` do core, se disponível.

        ``quantilica.core.lake`` só existe a partir de versões recentes do core;
        sem ele, cai no ``default_output`` (comportamento anterior).
        """
        try:
            from quantilica.core.lake import lake_root
        except ImportError:  # pragma: no cover - core sem o módulo lake
            return self.default_output
        return lake_root()

    def _run_convert(
        self,
        func: Callable[..., Any],
        input: Path,
        lake: Path,
        *,
        dataset: list[str] | None = None,
        force: bool = False,
    ) -> Any:
        """Chama ``func`` com os argumentos que ela aceita.

        Conversores legados têm a forma ``func(input, lake)``. ``dataset`` e
        ``force`` só são repassados (como keyword) quando a assinatura os aceita
        ou tem ``**kwargs``. Se o usuário pediu um deles e ``func`` não o
        aceita, sai com código 2 em vez de ignorar a opção em silêncio.
        """
        try:
            params = inspect.signature(func).parameters
        except (TypeError, ValueError):  # pragma: no cover - builtins/C callables
            params = None
        kwargs: dict[str, Any] = {}
        if params is not None:
            var_kw = any(
                p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()
            )
            wanted = {"dataset": dataset or None, "force": force or None}
            for name, value in wanted.items():
                if name in params or var_kw:
                    kwargs[name] = dataset if name == "dataset" else force
                elif value is not None:
                    get_console().print(
                        f"[red]Erro:[/red] este conversor não suporta --{name}."
                    )
                    raise typer.Exit(2)
        # Entrada ausente não é "nada a converter": alguns conversores devolvem
        # resultado vazio sem erro nesse caso, e o lake ficaria sem aviso.
        if not input.exists():
            message = f"diretório de entrada do conversor não existe: {input}"
            get_console().print(f"[red]Erro:[/red] {escape(message)}")
            logger.error("[convert] %s", message)
            raise typer.Exit(1)
        # O conversor lê só a versão vigente de cada arquivo bruto, nunca todas
        # as versões carimbadas da pasta. Quem escolhe a vigente por conta própria
        # (``selects_versions = True``) recebe a pasta original.
        try:
            with vigente_view(input, skip=converter_selects_versions(func)) as (
                source,
                ignored,
            ):
                if ignored:
                    get_console().print(
                        f"[dim]Bruto com versões antigas: {ignored} arquivo(s) fora "
                        "da conversão (só a versão vigente é lida).[/dim]"
                    )
                return func(source, lake, **kwargs)
        except ViewUnavailableError as exc:
            get_console().print(f"[red]Erro:[/red] {escape(str(exc))}")
            raise typer.Exit(1) from None

    @staticmethod
    def _convert_counts(result: Any) -> dict[str, Any] | None:
        """Extrai ``converted``/``skipped``/``errors`` do retorno de um conversor.

        Aceita um objeto com esses atributos ou um dicionário de contagens.
        Valores podem ser inteiros ou coleções (usa-se o tamanho nas contagens).

        Returns:
            ``{"converted": int, "skipped": int, "errors": int,
            "error_items": list}`` ou ``None`` se o retorno não tiver o formato.
        """
        keys = ("converted", "skipped", "errors")
        if isinstance(result, dict):
            raw = {k: result.get(k) for k in keys}
            present = any(k in result for k in keys)
        else:
            raw = {k: getattr(result, k, None) for k in keys}
            present = any(hasattr(result, k) for k in keys)
        if not present:
            return None

        def count(value: Any) -> int:
            if value is None:
                return 0
            if isinstance(value, int | float):
                return int(value)
            return len(value)

        errors = raw["errors"]
        items = (
            list(errors)
            if errors is not None and not isinstance(errors, int | float)
            else []
        )
        return {
            "converted": count(raw["converted"]),
            "skipped": count(raw["skipped"]),
            "errors": count(errors),
            "error_items": items,
        }

    def _report_convert(self, result: Any, console: Console) -> bool:
        """Imprime o sumário da conversão. Retorna ``True`` se houve erros."""
        counts = self._convert_counts(result)
        if counts is None:
            return False
        table = Table(show_header=False, box=None, pad_edge=False)
        table.add_row("[green]convertidos[/green]", str(counts["converted"]))
        table.add_row("[dim]pulados[/dim]", str(counts["skipped"]))
        table.add_row(
            "[red]erros[/red]" if counts["errors"] else "[dim]erros[/dim]",
            str(counts["errors"]),
        )
        console.print(table)
        for item in counts["error_items"]:
            console.print(f"  [red]✗[/red] {escape(str(item))}")
        return counts["errors"] > 0

    def command_convert(self, func: Callable[..., Any]) -> Callable[..., Any]:
        """Register the standard ``convert`` subcommand (raw → lake).

        The generated command exposes ``-i/--input``, ``--lake`` (aliases
        ``-o``/``--output``; default ``lake_root()``), ``--dataset`` (repeatable),
        ``--force`` and ``--verbose``. ``func`` is called as
        ``func(input, lake, dataset=..., force=...)``; the keywords are only
        passed when its signature accepts them, so legacy ``func(input, output)``
        converters keep working.

        If ``func`` returns an object (or dict) with ``converted``, ``skipped``
        and ``errors``, a summary is rendered and the command exits with code 1
        when there are errors. An ``ImportError`` propagating from ``func`` is
        treated as missing analytical extras and exits with code 1.

        Args:
            func: Callable performing the conversion.

        Returns:
            The original ``func`` (the method can be used as a decorator).
        """
        console = get_console()
        default_lake = self._default_lake()

        @self.app.command("convert")
        def convert(
            input: Annotated[
                Path,
                typer.Option(
                    "-i",
                    "--input",
                    help="Diretório de origem com arquivos brutos",
                ),
            ] = self.default_output,
            lake: Annotated[
                Path,
                typer.Option(
                    "--lake",
                    "-o",
                    "--output",
                    help="Raiz do lake de destino (padrão: QUANTILICA_LAKE_DIR)",
                ),
            ] = default_lake,
            dataset: Annotated[
                list[str] | None,
                typer.Option(
                    "--dataset", help="Filtrar dataset(s) específico(s) a converter"
                ),
            ] = None,
            force: Annotated[
                bool,
                typer.Option(
                    "--force", help="Forçar reconversão mesmo se sha256 coincidir"
                ),
            ] = False,
            verbose: Annotated[
                bool, typer.Option("--verbose", help="Logs detalhados")
            ] = False,
        ) -> None:
            setup_rich_logging(verbose, console=console)
            try:
                result = self._run_convert(
                    func, input, lake, dataset=dataset, force=force
                )
            except ImportError:
                console.print(
                    f"[red]Erro:[/red] convert requer extras de análise: "
                    f"{self._analysis_hint()}"
                )
                raise typer.Exit(1) from None
            failed = self._report_convert(result, console)
            if failed:
                console.print("[red]✗[/red] Conversão concluída com erros.")
                raise typer.Exit(1)
            console.print(f"[green]✓[/green] Conversão concluída em [dim]{lake}[/dim].")

        return func

    def command_archive(
        self, func: Callable[[Path, Path], Any]
    ) -> Callable[[Path, Path], Any]:
        """Register the standard ``archive`` subcommand.

        Mirrors :meth:`command_convert` with the canonical flags
        (``-i/--input``, ``-o/--output``, ``--verbose``) and delegates to
        ``func(input, output)``. An ``ImportError`` propagating from ``func``
        is treated as missing analytical extras and exits gracefully with
        code 1.

        Args:
            func: Callable receiving ``(input, output)`` paths and creating
                the historical archive.

        Returns:
            The original ``func`` (the method can be used as a decorator).
        """
        console = get_console()

        @self.app.command("archive")
        def archive(
            input: Annotated[
                Path,
                typer.Option(
                    "-i",
                    "--input",
                    help="Diretório de dados a arquivar",
                ),
            ] = self.default_output,
            output: Annotated[
                Path,
                typer.Option("-o", "--output", help="Diretório do arquivo histórico"),
            ] = self.default_output,
            verbose: Annotated[
                bool, typer.Option("--verbose", help="Logs detalhados")
            ] = False,
        ) -> None:
            setup_rich_logging(verbose, console=console)
            try:
                func(input, output)
            except ImportError:
                console.print(
                    f"[red]Erro:[/red] archive requer extras de análise: "
                    f"{self._analysis_hint()}"
                )
                raise typer.Exit(1) from None
            console.print(f"[green]✓[/green] Arquivo criado em [dim]{output}[/dim].")

        return func

    def command_pipeline(self, convert_func: Callable[..., Any]) -> Callable[..., Any]:
        """Register the standard ``pipeline`` subcommand (sync → convert).

        Step 1/2 invokes the built-in ``sync`` command (same flags: groups,
        ``-o/--output``, ``--workers``, ``--dry-run``, ``--verbose``) and step
        2/2 invokes ``convert_func(output, lake)`` as in :meth:`command_convert`
        (``--lake`` defaults to ``lake_root()``; ``--parquet-dir`` is kept as an
        alias for backwards compatibility).
        With ``--dry-run`` the plan is only listed and the conversion step is
        skipped. An ``ImportError`` from ``convert_func`` is treated as
        missing analytical extras.

        Args:
            convert_func: Callable receiving ``(output, lake)`` paths and
                performing the conversion.

        Returns:
            The original ``convert_func`` (usable as a decorator).
        """
        console = get_console()
        default_lake = self._default_lake()
        # A opção só aparece na ajuda do pipeline se o 'sync' registrado a aceita
        # (um sync próprio, p. ex. com build_default_commands=False, pode não ter):
        # quem sonda a ajuda (um agendador) não pode ter falso positivo.
        summary_visible = _sync_accepts_summary_json(self._registered_sync())

        @self.app.command("pipeline")
        def pipeline(
            ctx: typer.Context,
            groups: Annotated[
                list[str] | None,
                typer.Argument(
                    help="Grupos a baixar. Use 'list' para ver grupos "
                    "disponíveis. Padrão: todos."
                ),
            ] = None,
            output: Annotated[
                Path | None,
                typer.Option("-o", "--output", help="Diretório de saída"),
            ] = None,
            lake: Annotated[
                Path | None,
                typer.Option(
                    "--lake",
                    "--parquet-dir",
                    help="Raiz do lake de destino (padrão: QUANTILICA_LAKE_DIR)",
                ),
            ] = None,
            workers: Annotated[
                int,
                typer.Option(
                    "--workers",
                    envvar=SYNC_WORKERS_ENVVAR,
                    min=1,
                    help="Downloads paralelos (o padrão e a variável de ambiente "
                    "aparecem ao lado).",
                ),
            ] = 4,
            dry_run: Annotated[
                bool, typer.Option("--dry-run", help="Listar arquivos sem baixar")
            ] = False,
            verbose: Annotated[
                bool, typer.Option("--verbose", help="Logs detalhados")
            ] = False,
            # Valor-padrão (não Annotated): 'hidden' depende de uma variável do
            # fechamento, que as anotações adiadas (__future__) não enxergam.
            summary_json: Path | None = typer.Option(  # noqa: B008
                None,
                "--summary-json",
                help="Resumo JSON do passo de download (o mesmo do 'sync'; "
                "o exit_code do arquivo é o do download, não o do pipeline).",
                hidden=not summary_visible,
            ),
        ) -> None:
            setup_rich_logging(verbose, console=console)
            actual_output = output or self.default_output
            lake_out = lake or default_lake
            # Só repassa quando pedido: um 'sync' sobrescrito por um fetcher
            # pode não conhecer a opção.
            sync_extra: dict[str, Any] = {}

            console.print(Rule("[bold]Passo 1/2: Download[/bold]"))
            # O 'sync' efetivo: o que o typer resolve pelo nome (o último
            # registrado), padrão ou do plugin, chamado só com o que ele aceita.
            sync_callback = self._registered_sync()
            sync_cmd = self._sync_command() if sync_callback is not None else None
            if sync_cmd is None:
                console.print(
                    "[red]Erro:[/red] pipeline requer o comando 'sync' "
                    "(build_default_commands=True)."
                )
                raise typer.Exit(1) from None
            sync_kwargs = self._pipeline_sync_kwargs(
                sync_cmd,
                ctx,
                groups=groups,
                output=actual_output,
                dry_run=dry_run,
                workers=workers,
                verbose=verbose,
            )
            if sync_kwargs is None:
                raise typer.Exit(2)
            if summary_json is not None:
                if _sync_accepts_summary_json(sync_callback):
                    sync_extra["summary_json"] = summary_json
                else:
                    print(
                        "[Aviso] o 'sync' deste fetcher não aceita --summary-json; "
                        "o pipeline segue sem o resumo.",
                        file=sys.stderr,
                    )
            sync_partial = False
            try:
                # O callback do typer preenche com o padrão do click o que não vai
                # nos argumentos (sem os OptionInfo dos padrões em estilo
                # 'x: T = typer.Option(...)').
                ctx.invoke(sync_cmd.callback, **sync_kwargs, **sync_extra)
            except typer.Exit as exc:
                # Falha parcial não impede converter o que já baixou; o código
                # parcial é devolvido ao final. Falha total ou outros códigos
                # interrompem o pipeline como antes.
                if exc.exit_code != EXIT_SYNC_PARTIAL:
                    raise
                sync_partial = True

            if dry_run:
                console.print(
                    "\n[yellow]Dry-run:[/yellow] conversão (passo 2/2) não executada."
                )
                return

            console.print(Rule("[bold]Passo 2/2: Conversão[/bold]"))
            try:
                result = self._run_convert(convert_func, actual_output, lake_out)
            except ImportError:
                console.print(
                    f"[red]Erro:[/red] pipeline (conversão) requer extras de "
                    f"análise: {self._analysis_hint()}"
                )
                raise typer.Exit(1) from None
            if self._report_convert(result, console):
                console.print("[red]✗[/red] Pipeline concluído com erros na conversão.")
                raise typer.Exit(1)
            if sync_partial:
                console.print(
                    "[yellow]![/yellow] Pipeline concluído com download parcial: "
                    f"lake em [dim]{lake_out}[/dim]."
                )
                raise typer.Exit(EXIT_SYNC_PARTIAL)
            console.print(
                f"[green]✓[/green] Pipeline concluído: lake em [dim]{lake_out}[/dim]."
            )

        return convert_func

    def command_backfill(
        self,
        units: Callable[[], Iterable[str]],
        run_unit: Callable[..., None],
        *,
        name: str = "backfill",
        help: str = "Executar carga histórica (backfill) com checkpoint",
    ) -> None:
        """Register a backfill subcommand with persistent checkpointing (P5).

        Units completed are tracked in ``<output>/.backfill/<name>.json``.
        By default execution resumes from previous checkpoint (``--resume``);
        use ``--restart`` to ignore existing checkpoints.
        Handles SIGINT gracefully by persisting state before exit.
        """
        console = get_console()

        @self.app.command(name, help=help)
        def backfill(
            output: Annotated[
                Path | None,
                typer.Option("-o", "--output", help="Diretório de saída"),
            ] = None,
            resume: Annotated[
                bool,
                typer.Option(
                    "--resume/--restart",
                    help="Retomar do checkpoint ou reiniciar",
                ),
            ] = True,
            verbose: Annotated[
                bool, typer.Option("--verbose", help="Logs detalhados")
            ] = False,
            rateio: Annotated[
                bool,
                typer.Option(
                    "--rateio",
                    help="Carga histórica de rateio e ganhadores por município",
                ),
            ] = False,
            regional: Annotated[
                bool,
                typer.Option(
                    "--regional",
                    help="Carga de séries regionais e de municípios",
                ),
            ] = False,
        ) -> None:
            setup_rich_logging(verbose, console=console)
            actual_output = output or self.default_output
            backfill_dir = actual_output / ".backfill"
            backfill_dir.mkdir(parents=True, exist_ok=True)
            checkpoint_path = backfill_dir / f"{name}.json"

            completed: set[str] = set()
            failed: dict[str, str] = {}

            if resume and checkpoint_path.is_file():
                try:
                    with open(checkpoint_path, encoding="utf-8") as f:
                        data = json.load(f)
                    if isinstance(data, dict):
                        completed = set(data.get("completed", []))
                        failed = dict(data.get("failed", {}))
                        console.print(
                            f"[dim]Checkpoint carregado: {len(completed)} "
                            f"concluídos, {len(failed)} com erro.[/dim]"
                        )
                except Exception as exc:
                    console.print(
                        f"[yellow]Aviso:[/yellow] Falha ao ler checkpoint: {exc}"
                    )

            def save_checkpoint() -> None:
                tmp_checkpoint = checkpoint_path.with_suffix(".tmp")
                payload = {
                    "name": name,
                    "updated_at": dt.datetime.now(dt.UTC).isoformat(),
                    "completed": sorted(completed),
                    "failed": failed,
                }
                with open(tmp_checkpoint, "w", encoding="utf-8") as f:
                    json.dump(payload, f, ensure_ascii=False, indent=2)
                tmp_checkpoint.replace(checkpoint_path)

            import inspect

            kwargs: dict[str, Any] = {}
            if rateio:
                kwargs["rateio"] = True
            if regional:
                kwargs["regional"] = True

            units_sig = inspect.signature(units)
            params = units_sig.parameters
            unit_call_args: list[Any] = []
            if len(params) >= 1 and list(params.keys())[0] not in (
                "rateio",
                "regional",
            ):
                unit_call_args.append(actual_output)
            unit_call_kwargs = {k: v for k, v in kwargs.items() if k in params}
            unit_list = list(units(*unit_call_args, **unit_call_kwargs))
            total = len(unit_list)
            # Falha de unidade que a lista atual não gera mais (ex.: uma unidade
            # de erro de uma listagem que agora funciona) nunca vai rodar de
            # novo: não pode prender o backfill em saída 1 para sempre.
            current_units = set(unit_list)
            vanished = [u for u in failed if u not in current_units]
            for unit in vanished:
                del failed[unit]
            if vanished:
                logger.info(
                    "[backfill] %d falha(s) do checkpoint de unidade(s) fora da "
                    "lista atual, descartada(s): %s",
                    len(vanished),
                    ", ".join(vanished[:10]) + (" ..." if len(vanished) > 10 else ""),
                )
            remaining = [u for u in unit_list if u not in completed]
            console.print(
                f"Iniciando {name}: {len(remaining)}/{total} unidades pendentes."
            )

            run_sig = inspect.signature(run_unit)
            takes_output = len(run_sig.parameters) >= 2

            try:
                for unit in remaining:
                    try:
                        if takes_output:
                            run_unit(unit, actual_output)
                        else:
                            run_unit(unit)
                        completed.add(unit)
                        failed.pop(unit, None)
                    except Exception as exc:
                        failed[unit] = str(exc)
                        console.print(f"  [red]✗[/red] {unit}: {exc}")
                    save_checkpoint()
            except KeyboardInterrupt:
                save_checkpoint()
                console.print(
                    "\n[yellow]Interrompido por SIGINT. Checkpoint salvo.[/yellow]"
                )
                raise typer.Exit(130) from None

            save_checkpoint()
            if failed:
                console.print(
                    f"[red]Backfill concluído com {len(failed)} falhas em "
                    f"{total} unidades.[/red]"
                )
                raise typer.Exit(1)
            console.print(
                f"[green]✓[/green] Backfill {name} concluído com sucesso "
                f"({total} unidades)."
            )

    def _safe_head_date(self, url: str) -> dt.date | None:
        with contextlib.suppress(Exception):
            if hasattr(self, "freshness_policy") and self.freshness_policy:
                from quantilica.core.sync import FreshnessPolicy, HttpFreshnessProbe

                policy = FreshnessPolicy(self.freshness_policy)
                if policy == FreshnessPolicy.REMOTE_ONLY:
                    # O conteúdo é a única verdade (sha256); não sondar
                    # com HEAD — o timestamp do nome virá dos metadados da
                    # própria entrada, não de uma sondagem remota.
                    return None
                # Só HTTP tem HEAD; com FtpClient o erro cai no suppress abaixo.
                http_client = cast(HttpClient, self.client)
                probe = HttpFreshnessProbe(http_client, policy=policy)
                stat = probe.probe(url)
                if stat and stat.last_modified:
                    return stat.last_modified.date()
            return cast(HttpClient, self.client).head_last_modified_date(url)
        return None

    def _download_file(
        self,
        url: str,
        output: Path,
        progress: ProgressCallback | None = None,
        *,
        force: bool = False,
        dataset_id: str | None = None,
        expected_format: str | None = None,
    ) -> Path:
        if dataset_id:
            resolved_dataset_id = dataset_id
        elif (
            output.parent.name.isdigit()
            and len(output.parent.name) == 4
            and output.parent.parent.name not in ("", ".")
        ):
            resolved_dataset_id = output.parent.parent.name
        else:
            resolved_dataset_id = output.parent.name or self.source_id
        # `force` só é repassado quando True: clients/subclasses antigos não
        # precisam aceitar o kwarg.
        extra: dict[str, Any] = {"force": True} if force else {}
        # Deduplication by SHA-256 for REMOTE_ONLY sources.
        # When freshness_policy is REMOTE_ONLY, we skip the HEAD probe and rely on
        # content hash comparison to avoid creating new @YYYYMMDD versions for
        # unchanged content.
        try:
            from quantilica.core.sync import FreshnessPolicy

            if getattr(
                self, "freshness_policy", None
            ) == FreshnessPolicy.REMOTE_ONLY and self._client_accepts(
                "deduplicate_by_sha256"
            ):
                extra["deduplicate_by_sha256"] = True
        except Exception:
            pass
        # F4: valida a assinatura do payload automaticamente pela extensão do
        # destino (Soft-200 HTML, WAF e arquivos truncados). Passe
        # ``expected_format=""`` para desativar em casos especiais. Clientes
        # que não aceitam o kwarg (ex.: FtpClient) são ignorados.
        fmt = (
            expected_format
            if expected_format is not None
            else expected_format_for_path(output.name)
        )
        if fmt and self._client_accepts("expected_format"):
            extra["expected_format"] = fmt
        return self.client.download_with_manifest(
            url,
            output,
            source_id=self.source_id,
            dataset_id=resolved_dataset_id,
            producer=self.name,
            progress=progress,
            **extra,
        )

    def _client_accepts(self, kwarg: str) -> bool:
        """Tell whether ``client.download_with_manifest`` takes ``kwarg``.

        Clients such as ``FtpClient`` do not accept every HTTP-only option, so
        optional kwargs are only forwarded when the signature allows them (or
        when it cannot be inspected, to keep the previous behaviour).
        """
        try:
            sig = inspect.signature(self.client.download_with_manifest)
        except (ValueError, TypeError):
            return True
        return any(
            p.kind == inspect.Parameter.VAR_KEYWORD or p.name == kwarg
            for p in sig.parameters.values()
        )

    def download_entry(
        self,
        entry: dict[str, Any],
        output_dir: Path,
        *,
        dry_run: bool = False,
        progress: ProgressCallback | None = None,
        force: bool = False,
    ) -> Path:
        """Download one dataset entry and return the destination path.

        Args:
            entry: Dictionary containing dataset metadata (url, id, etc).
            output_dir: Destination directory.
            dry_run: If True, computes the destination path without downloading.
            progress: Optional callback to track download progress.
            force: Se True, ignora a checagem de frescor local e baixa sempre.

        Returns:
            The local path where the file was (or would be) saved.

        Raises:
            FetchError: If no valid URLs could be downloaded.
            HttpStatusError: On non-404 HTTP errors.
        """
        urls_to_try = [entry["url"]]
        if "fallback_urls" in entry and entry["fallback_urls"]:
            urls_to_try.extend(entry["fallback_urls"])

        entry_dataset_id = (
            str(entry.get("group") or entry.get("dataset") or entry.get("id") or "")
            or None
        )

        last_err = None

        for url in urls_to_try:
            try:
                last_modified = self._safe_head_date(url)
                output = self.path_builder(output_dir, entry, last_modified)

                # Use original ext for output filename but override if url has different one
                if url != entry["url"]:
                    actual_ext = url.split(".")[-1]
                    if "ext" in entry and output.name.endswith(f".{entry['ext']}"):
                        new_name = (
                            output.name[: -(len(entry["ext"]) + 1)] + f".{actual_ext}"
                        )
                        output = output.with_name(new_name)

                if dry_run:
                    return output
                dl_kwargs: dict[str, Any] = {"progress": progress}
                if force:
                    dl_kwargs["force"] = True
                if entry_dataset_id is not None:
                    try:
                        sig = inspect.signature(self._download_file)
                        if any(
                            p.kind == inspect.Parameter.VAR_KEYWORD
                            or p.name == "dataset_id"
                            for p in sig.parameters.values()
                        ):
                            dl_kwargs["dataset_id"] = entry_dataset_id
                    except (ValueError, TypeError):
                        dl_kwargs["dataset_id"] = entry_dataset_id
                entry_format = entry.get("expected_format")
                if entry_format is not None:
                    try:
                        sig = inspect.signature(self._download_file)
                        if any(
                            p.kind == inspect.Parameter.VAR_KEYWORD
                            or p.name == "expected_format"
                            for p in sig.parameters.values()
                        ):
                            dl_kwargs["expected_format"] = str(entry_format)
                    except (ValueError, TypeError):
                        dl_kwargs["expected_format"] = str(entry_format)
                return self._download_file(url, output, **dl_kwargs)
            except HttpStatusError as exc:
                if exc.status_code == 404:
                    last_err = exc
                    continue
                raise

        if last_err:
            raise last_err
        raise FetchError(f"No valid URLs for {entry.get('id', 'unknown')}")

    def _expand_group(self, key: str) -> list[str]:
        if key in self.aliases:
            return self.aliases[key]
        if key in self.groups:
            return [key]
        return []

    def download_datasets(
        self,
        entries: list[dict[str, Any]],
        output_dir: Path,
        workers: int = 4,
        *,
        force: bool = False,
    ) -> tuple[int, int, list[tuple[str, str]]]:
        """Executes parallel download for a list of dataset entries.

        Args:
            entries: List of dataset entries to download.
            output_dir: The target base directory for downloads.
            workers: Maximum number of parallel downloads.
            force: Se True, ignora a checagem de frescor local e baixa tudo.

        Returns:
            A tuple containing:
            - downloaded_count: Number of successfully downloaded files.
            - total_count: Total number of files attempted.
            - errors_list: A list of tuples containing the dataset ID and the error message.
        """
        console = get_console()
        total = len(entries)
        overall = make_batch_progress(console)
        file_prog = make_download_progress(console)
        overall_task = overall.add_task("[cyan]Baixando...[/cyan]", total=total)

        downloaded = 0
        errors: list[tuple[str, str]] = []
        pool = ProgressPool(workers=workers, file_prog=file_prog)
        stats = self.last_download_stats = DownloadStats()

        # Pooling por worker: cada thread mantém seu HttpClient com keep-alive.
        thread_local = threading.local()
        _worker_clients: list[HttpClient] = []
        # O client do app, antes de qualquer troca por um client de thread.
        base_client = self.client

        def _get_worker_client() -> HttpClient | FtpClient:
            if isinstance(base_client, FtpClient):
                return base_client
            if not hasattr(thread_local, "client"):
                # Reusa config do client canônico, mas com sessão persistente.
                c = HttpClient(
                    timeout=base_client.timeout,
                    headers=dict(base_client.headers),
                    follow_redirects=base_client.follow_redirects,
                    attempts=base_client.attempts,
                    retry_base_delay=base_client.retry_base_delay,
                    verify=base_client.verify,
                    transport=base_client.transport,
                    limits=base_client.limits,
                    emulate_browser=True,
                )
                if base_client.host_policies:
                    # Política por host: o ritmo, a pausa do 429 e o teto de
                    # simultâneas são do host, não da thread. Sem política o
                    # worker segue independente, como sempre foi.
                    share = getattr(c, "share_host_state", None)
                    if share is not None:
                        share(base_client)
                    else:  # quantilica-core anterior à política compartilhada
                        _warn_core_sem_estado_compartilhado()
                c.__enter__()
                thread_local.client = c
                _worker_clients.append(c)
            return thread_local.client  # type: ignore[return-value]

        def _worker(entry: dict[str, Any]) -> bool:
            # Troca temporária do client para o da thread (com pooling).
            worker_client = _get_worker_client()
            self.client = worker_client  # type: ignore[assignment]
            try:
                eid = entry.get("id", "unknown")
                with pool.acquire(description=f"[cyan]{eid}[/cyan]") as cb:
                    measured = stats.track().forwarding(cb)
                    if force:
                        self.download_entry(
                            entry, output_dir, progress=measured, force=True
                        )
                    else:
                        self.download_entry(entry, output_dir, progress=measured)
                    stats.succeeded(measured)
                    return True
            except Exception as exc:
                eid = str(entry.get("id", "unknown"))
                if _is_pending_publication(entry) and _is_not_found(exc):
                    # Período corrente que a fonte ainda não publicou: não é
                    # erro do arquivo (não afeta o código de saída).
                    logger.info(
                        "[sync] %s ainda não publicado pela fonte (404); "
                        "entrada pulada (%s).",
                        eid,
                        PENDING_PUBLICATION_REASON,
                    )
                    stats.skipped(eid, PENDING_PUBLICATION_REASON)
                    return True
                stats.failed()
                errors.append((entry.get("id", "unknown"), str(exc)))
                return False

        with graceful_executor(max_workers=workers) as executor:
            try:
                with Live(
                    Group(overall, file_prog),
                    console=console,
                    refresh_per_second=10,
                ):
                    futures = {
                        executor.submit(_worker, entry): entry for entry in entries
                    }
                    for future in concurrent.futures.as_completed(futures):
                        overall.update(overall_task, advance=1)
                        if future.result():
                            downloaded += 1
            except KeyboardInterrupt:
                console.print("\n[yellow]Interrompido.[/yellow]")
                raise typer.Exit(130) from None
            finally:
                # Devolve o client do app: as threads o trocam pelo seu e, com
                # a troca por thread, o "anterior" de uma pode ser o de outra.
                self.client = base_client
                for c in _worker_clients:
                    with contextlib.suppress(Exception):
                        c.close()

        return downloaded, total, errors

    def check_entry(self, entry: dict[str, Any], output_dir: Path) -> CheckPlanItem:
        """Check one dataset entry against the remote without downloading.

        Performs HEAD requests (same URLs ``download_entry`` would try,
        including ``fallback_urls``) and compares against the local stamped
        file using the same freshness predicate as the download path.

        Args:
            entry: Dictionary containing dataset metadata (url, id, etc).
            output_dir: Destination directory used for path computation.

        Returns:
            The freshness verdict for the entry.
        """
        dataset = str(entry.get("group") or entry.get("id") or "datasets")
        eid = str(entry.get("id", "unknown"))
        partition = _partition_label(entry)
        urls_to_try = [entry["url"]]
        if "fallback_urls" in entry and entry["fallback_urls"]:
            urls_to_try.extend(entry["fallback_urls"])

        head = None
        head_error: Exception | None = None
        not_found = 0
        for url in urls_to_try:
            try:
                candidate = cast(HttpClient, self.client).head(url)  # só HTTP
            except HttpStatusError as exc:
                head_error = exc
                if exc.status_code == 404:
                    not_found += 1
                    continue
                if exc.status_code in (403, 405):
                    try:
                        probed = self._range_probe(entry, output_dir, url, partition)
                    except _ProbeNotFoundError:
                        # HEAD recusado, mas o GET de probe prova que sumiu.
                        not_found += 1
                        continue
                    if probed is not None:
                        return probed
                break
            except Exception as exc:  # noqa: BLE001 - metadata best effort
                head_error = exc
                break
            head = candidate
            break

        if head is None:
            local = self.path_builder(output_dir, entry, None)
            if not_found == len(urls_to_try):
                pending = _is_pending_publication(entry)
                if pending:
                    logger.info(
                        "[check] %s ainda não publicado pela fonte (404): %s",
                        eid,
                        PENDING_PUBLICATION_REASON,
                    )
                return CheckPlanItem(
                    dataset=dataset,
                    id=eid,
                    url=str(entry["url"]),
                    partition=partition,
                    local_path=str(local),
                    remote_etag=None,
                    remote_last_modified=None,
                    remote_size=None,
                    local_exists=local.exists(),
                    action="skip-unavailable",
                    reason=PENDING_PUBLICATION_REASON if pending else "remote-404",
                    entry=dict(entry),
                )
            return CheckPlanItem(
                dataset=dataset,
                id=eid,
                url=str(entry["url"]),
                partition=partition,
                local_path=str(local),
                remote_etag=None,
                remote_last_modified=None,
                remote_size=None,
                local_exists=local.exists(),
                action="download",
                reason=f"metadata-unavailable: {head_error}",
                entry=dict(entry),
            )

        etag = head.headers.get("ETag")
        size_raw = head.headers.get("Content-Length")
        try:
            size = int(size_raw) if size_raw else None
        except (TypeError, ValueError):
            size = None
        last_modified = _parse_head_date(head.headers.get("Last-Modified"))
        local = self.path_builder(output_dir, entry, last_modified)
        sidecar = manifest_sidecar_path(local)
        exists = local.exists() or sidecar.exists()
        if exists and not is_remote_more_recent(head, local):
            if local.exists():
                action, reason = "skip-up-to-date", "local-fresh"
            else:
                action, reason = "skip-up-to-date", "manifest-fresh"
        else:
            action = "download"
            reason = "not-present" if not exists else "remote-newer"
        return CheckPlanItem(
            dataset=dataset,
            id=eid,
            url=str(head.url),
            partition=partition,
            local_path=str(local),
            remote_etag=etag,
            remote_last_modified=head.headers.get("Last-Modified"),
            remote_size=size,
            local_exists=exists,
            action=action,
            reason=reason,
            entry=dict(entry),
        )

    def entry_from_plan(self, data: dict[str, Any]) -> dict[str, Any]:
        """Restaura uma entry desserializada de um :class:`CheckPlan`.

        O padrão retorna uma cópia rasa de ``data``, sem nenhuma
        transformação. Sobrescreva (subclasse ou
        atribuição de função na instância, que recebe só ``data``) quando
        ``check_entry`` serializa objetos ricos (ex.: ``datetime``) na entry.

        Args:
            data: Entry como lida do plano JSON.

        Returns:
            A entry pronta para ``download_entry``/``path_builder``.
        """
        return dict(data)

    def _range_probe(
        self,
        entry: dict[str, Any],
        output_dir: Path,
        url: str,
        partition: str | None,
    ) -> CheckPlanItem | None:
        """Probe ``GET`` com ``Range: bytes=0-0`` quando o ``HEAD`` é recusado.

        Nunca lê o corpo: se o servidor ignorar o Range e responder 200, a
        resposta é fechada sem consumir o body.

        Returns:
            O veredito da entrada, ou ``None`` se o client não suporta
            streaming ou o probe falhou (mantém o comportamento padrão).

        Raises:
            _ProbeNotFoundError: Se o GET de probe responder 404.
        """
        maybe_stream = getattr(self.client, "stream", None)
        if not callable(maybe_stream):
            return None
        stream: Callable[..., Any] = maybe_stream  # callable() narrows to object
        for attempt in range(1, _PROBE_ATTEMPTS + 1):
            try:
                with stream(
                    "GET",
                    url,
                    # identity: Content-Length/Content-Range devem refletir o
                    # tamanho real, não o comprimido.
                    headers={"Range": "bytes=0-0", "Accept-Encoding": "identity"},
                ) as response:
                    headers = response.headers
                    status = response.status_code
                    final_url = str(response.url)
                break
            except HttpStatusError as exc:
                if exc.status_code == 404:
                    raise _ProbeNotFoundError(url) from exc
                failure: Exception = exc
                retryable = exc.status_code in _PROBE_RETRY_STATUSES
            except DEFAULT_RETRY_EXCEPTIONS as exc:
                failure, retryable = exc, True
            except Exception as exc:  # noqa: BLE001 - probe é best effort
                failure, retryable = exc, False
            # 429/5xx ou erro de transporte transitório (ex.: 503 intermitente
            # do gov.br): sem nova tentativa, uma URL removida viraria
            # "download" e falharia com 404 no sync.
            if retryable and attempt < _PROBE_ATTEMPTS:
                time.sleep(_PROBE_BACKOFF_S * attempt)
                continue
            # WARNING: uma execução agendada não roda com --verbose e precisa registrar a causa.
            logger.warning(
                "[check] probe Range falhou para %s após %d tentativa(s): %r",
                url,
                attempt,
                failure,
            )
            return None

        total: int | None = None
        content_range = headers.get("Content-Range")
        if status == 206 and content_range and "/" in content_range:
            raw = content_range.rsplit("/", 1)[1].strip()
            total = int(raw) if raw.isdigit() else None
        elif status == 200:
            encoding = (headers.get("Content-Encoding") or "identity").strip()
            if encoding.lower() != "identity":
                return None  # Content-Length seria o tamanho comprimido
            raw = (headers.get("Content-Length") or "").strip()
            total = int(raw) if raw.isdigit() else None
        if total is None:
            return None

        last_modified = _parse_head_date(headers.get("Last-Modified"))
        local = self.path_builder(output_dir, entry, last_modified)
        sidecar = manifest_sidecar_path(local)
        local_exists = local.exists()
        exists = local_exists or sidecar.exists()
        action, reason = "download", "range-probe"
        if local_exists and local.stat().st_size == total:
            action, reason = "skip-up-to-date", "size-match-range-probe"
        elif sidecar.exists():
            with contextlib.suppress(OSError, ValueError, AttributeError):
                data = json.loads(sidecar.read_text(encoding="utf-8"))
                if data.get("size_bytes") == total:
                    action, reason = "skip-up-to-date", "size-match-range-probe"
        return CheckPlanItem(
            dataset=str(entry.get("group") or entry.get("id") or "datasets"),
            id=str(entry.get("id", "unknown")),
            url=final_url,
            partition=partition,
            local_path=str(local),
            remote_etag=headers.get("ETag"),
            remote_last_modified=headers.get("Last-Modified"),
            remote_size=total,
            local_exists=exists,
            action=action,
            reason=reason,
            entry=dict(entry),
        )

    def _check_entry_isolated(
        self, entry: dict[str, Any], output_dir: Path
    ) -> CheckPlanItem:
        """Run :meth:`check_entry` without letting one entry abort the check.

        An unexpected error (e.g. ``OSError`` while building the local path)
        is retried once; if it persists the entry becomes a conservative
        ``download`` verdict, so the plan still covers every other entry
        instead of the whole ``check`` exiting 1.
        """
        for attempt in (1, 2):
            try:
                return self.check_entry(entry, output_dir)
            except Exception as exc:  # noqa: BLE001 - isolate per entry
                failure = exc
                if attempt == 1:
                    time.sleep(1.0)
        eid = str(entry.get("id", "unknown"))
        logger.warning(
            "[check] falha ao verificar %s (%s: %s); marcada para download",
            eid,
            type(failure).__name__,
            failure,
        )
        return CheckPlanItem(
            dataset=str(entry.get("group") or entry.get("id") or "datasets"),
            id=eid,
            url=str(entry.get("url", "")),
            partition=_partition_label(entry),
            local_path="",
            remote_etag=None,
            remote_last_modified=None,
            remote_size=None,
            local_exists=False,
            action="download",
            reason=f"check-error: {type(failure).__name__}: {failure}",
            entry=dict(entry),
        )

    def check_datasets(
        self,
        entries: list[dict[str, Any]],
        output_dir: Path,
        workers: int = 4,
    ) -> CheckPlan:
        """Checks freshness for a list of dataset entries (no downloads).

        Args:
            entries: List of dataset entries to check.
            output_dir: The base directory used for path computation.
            workers: Maximum number of parallel checks.

        Returns:
            A :class:`CheckPlan` with one verdict per entry.
        """
        results: list[CheckPlanItem | None] = [None] * len(entries)

        def _worker(index: int, entry: dict[str, Any]) -> None:
            results[index] = self._check_entry_isolated(entry, output_dir)
            if (index + 1) % 100 == 0 or index + 1 == len(entries):
                logger.info("[check] %d/%d verificados", index + 1, len(entries))

        with graceful_executor(max_workers=workers) as executor:
            try:
                futures = {
                    executor.submit(_worker, index, entry): index
                    for index, entry in enumerate(entries)
                }
                for future in concurrent.futures.as_completed(futures):
                    future.result()
            except KeyboardInterrupt:
                get_console().print("\n[yellow]Interrompido.[/yellow]")
                raise typer.Exit(130) from None

        generated = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
        return CheckPlan(
            fetcher=self.name,
            output_dir=str(output_dir),
            generated_at=generated,
            items=[item for item in results if item is not None],
        )

    def _list_catalog(
        self, target_groups: list[str]
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """Entradas dos grupos pedidos e os grupos que não devolveram nenhuma."""
        entries: list[dict[str, Any]] = []
        empty: list[str] = []
        for group in target_groups:
            found = list(self.list_datasets(group))
            if not found:
                empty.append(group)
            entries.extend(found)
        return entries, empty

    def _guard_catalog(
        self,
        target_groups: list[str],
        entries: list[dict[str, Any]],
        empty: list[str],
        *,
        recorder: SummaryRecorder | None = None,
    ) -> str | None:
        """Catálogo vazio não é sucesso (``sync`` e ``check``).

        * Nenhuma entrada em nenhum grupo pedido: devolve a mensagem de erro
          ``catálogo vazio para <grupos>``; quem chama a imprime e sai com 1
          (depois de dar ao plugin a chance de somar as causas dele, no
          ``sync``). Exceção: todos os grupos pedidos estão em
          ``expected_empty_groups`` (devolve ``None``).
        * Algum grupo vazio com os outros cheios: aviso em WARNING e
          ``empty_groups`` no resumo; não muda o código de saída. Os grupos de
          ``expected_empty_groups`` não entram.
        """
        expected = self.expected_empty_groups
        unexpected = [g for g in empty if g not in expected]
        if not entries:
            if target_groups and all(g in expected for g in target_groups):
                logger.info(
                    "[catálogo] sem entradas em %s (vazio por desenho)",
                    ", ".join(target_groups),
                )
                return None
            culprits = unexpected or target_groups
            label = ", ".join(culprits) if culprits else "(nenhum grupo)"
            message = f"catálogo vazio para {label}"
            logger.error("[catálogo] %s", message)
            if recorder is not None:
                recorder.error = message
                recorder.empty_groups = list(unexpected)
            return message
        if unexpected:
            logger.warning(
                "[catálogo] grupo(s) sem entradas: %s (os demais têm entradas)",
                ", ".join(unexpected),
            )
            if recorder is not None:
                recorder.empty_groups = list(unexpected)
        return None

    def _fail_empty_catalog(
        self,
        message: str,
        recorder: SummaryRecorder,
        *,
        output: Path,
        dry_run: bool,
        workers: int,
        force: bool,
    ) -> None:
        """Termina o ``sync`` de catálogo vazio com saída 1, sem apagar causas.

        Um plugin que sobrescreve ``download_datasets`` pode acrescentar aos
        erros as causas que só ele conhece (ex.: falha de descoberta de um
        grupo). Por isso o ``download_datasets`` roda com a lista vazia (fora do
        ``--dry-run``), os erros dele vão para o resumo e para a tela, e a
        mensagem de catálogo vazio entra junto, em ``error``.
        """
        console = get_console()
        recorder.error = message
        errors: list[tuple[str, str]] = []
        if not dry_run:
            kwargs: dict[str, Any] = {"workers": workers}
            if force:
                kwargs["force"] = True
            downloaded, total, errors = self.download_datasets([], output, **kwargs)
            recorder.total, recorder.succeeded = total, downloaded
            recorder.errors = errors
        console.print(f"[red]Erro:[/red] {escape(message)}")
        if errors:
            console.print(f"[red]{len(errors)} erro(s):[/red]")
            for eid, emsg in errors:
                console.print(f"  {eid}: {emsg}")
        raise typer.Exit(1)

    def _run_sync(
        self,
        recorder: SummaryRecorder,
        *,
        groups: list[str] | None,
        output: Path,
        dry_run: bool,
        workers: int,
        from_plan: Path | None,
        force: bool,
    ) -> None:
        """Corpo do comando ``sync`` (resolve entradas, baixa e reporta).

        Preenche ``recorder`` e levanta ``typer.Exit`` como o comando sempre
        fez; a gravação do ``--summary-json`` fica com o contexto do chamador.
        """
        console = get_console()
        actual_output = output
        # Falhas de descoberta que o ``check`` gravou no plano (itens
        # ``skip-unavailable``/``listing-error``): entram nos erros do sync.
        listing_errors: list[tuple[str, str]] = []

        if from_plan is not None:
            try:
                plan = CheckPlan.from_json(from_plan.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                console.print(f"[red]Plano inválido: {exc}[/red]")
                recorder.error = f"Plano inválido: {exc}"
                raise typer.Exit(1) from None
            if not plan.items and plan.expected_empty:
                console.print(
                    f"[dim]Plano {from_plan}: catálogo vazio por desenho "
                    "(grupos em expected_empty_groups); nada a baixar.[/dim]"
                )
            elif not plan.items:
                # Um plano sem nenhuma entrada verificada só sai de um catálogo
                # vazio: não é "nada a baixar".
                message = f"plano sem nenhuma entrada verificada: {from_plan}"
                console.print(f"[red]Erro:[/red] {escape(message)}")
                recorder.error = message
                raise typer.Exit(1)
            entries = [
                self.entry_from_plan(dict(item.entry))
                for item in plan.items
                if item.action == "download"
            ]
            listing_errors = plan_listing_errors(plan)
            console.print(
                f"[dim]Plano {from_plan}: {len(entries)} entrada(s) "
                f"com ação 'download' de {len(plan.items)} verificada(s).[/dim]"
            )
        else:
            try:
                target_groups = self.resolve_groups(groups)
            except ValueError as exc:
                console.print(f"[red]{exc}[/red]")
                console.print(f"Grupos válidos: {', '.join(self.all_keys)}")
                recorder.error = str(exc)
                raise typer.Exit(1) from None

            entries, empty = self._list_catalog(target_groups)
            empty_message = self._guard_catalog(
                target_groups, entries, empty, recorder=recorder
            )
            if empty_message is not None:
                self._fail_empty_catalog(
                    empty_message,
                    recorder,
                    output=actual_output,
                    dry_run=dry_run,
                    workers=workers,
                    force=force,
                )

        recorder.total = len(entries)

        if dry_run:
            recorder.dry_run = True
            table = Table("Grupo", "ID", "URL", title="Arquivos a baixar (dry-run)")
            for e in entries:
                table.add_row(e.get("group", ""), e.get("id", ""), e.get("url", ""))
            console.print(table)
            console.print(f"\n[bold]{len(entries)}[/bold] arquivo(s) listado(s).")
            recorder.succeeded = 0
            recorder.errors = list(listing_errors)
            if listing_errors:
                console.print(
                    f"[red]{len(listing_errors)} falha(s) de descoberta no plano:[/red]"
                )
                for eid, emsg in listing_errors:
                    console.print(f"  {eid}: {escape(emsg)}")
                raise typer.Exit(1)
            return

        if force:
            downloaded, total, errors = self.download_datasets(
                entries, actual_output, workers=workers, force=True
            )
        else:
            downloaded, total, errors = self.download_datasets(
                entries, actual_output, workers=workers
            )
        if listing_errors:
            total += len(listing_errors)
            errors = [*errors, *listing_errors]
        recorder.total, recorder.succeeded, recorder.errors = total, downloaded, errors

        console.print(
            f"\n[green]Concluído:[/green] {downloaded}/{total} arquivo(s) baixado(s)."
        )
        pending = [
            eid
            for eid, reason in (
                self.last_download_stats.skipped_items
                if self.last_download_stats is not None
                else []
            )
            if reason == PENDING_PUBLICATION_REASON
        ]
        if pending:
            console.print(
                f"[dim]{len(pending)} entrada(s) ainda não publicada(s) pela fonte "
                f"(404 tolerado, contadas como puladas): "
                f"{escape(', '.join(pending))}[/dim]"
            )
        if errors:
            console.print(f"[red]{len(errors)} erro(s):[/red]")
            for eid, emsg in errors:
                console.print(f"  {eid}: {emsg}")
            # Erros nunca saem como sucesso: parcial (3) se algo baixou,
            # falha total (1) se nada baixou.
            raise typer.Exit(EXIT_SYNC_PARTIAL if downloaded > 0 else 1)

    def _registered_sync(self) -> Callable[..., Any] | None:
        """Callback do comando ``sync`` efetivo neste app, se houver.

        Quando mais de um ``sync`` foi registrado (o padrão do ``FetcherApp`` e
        um do plugin, por exemplo), o typer fica com o ÚLTIMO de cada nome; é
        esse que o usuário executa e que o ``pipeline`` precisa chamar.
        """
        found: Callable[..., Any] | None = None
        for cmd in self.app.registered_commands:
            callback = cmd.callback
            name = cmd.name
            if name is None and callback is not None:
                # nome padrão do typer: o da função, com "_" virando "-"
                name = callback.__name__.lower().replace("_", "-")
            if name == "sync" and callback is not None:
                found = callback
        return found

    def _sync_command(self) -> Any | None:
        """O comando ``sync`` como o typer o resolve por nome (objeto do click)."""
        try:
            root = typer.main.get_command(self.app)
        except Exception:  # noqa: BLE001 - app sem comandos/typer incompatível
            return None
        commands = getattr(root, "commands", None)
        return commands.get("sync") if commands else None

    @staticmethod
    def _pipeline_sync_kwargs(
        sync_cmd: Any,
        ctx: typer.Context,
        *,
        groups: list[str] | None,
        output: Path,
        dry_run: bool,
        workers: int,
        verbose: bool,
    ) -> dict[str, Any] | None:
        """Argumentos do ``pipeline`` que o ``sync`` efetivo sabe receber.

        Só repassa o que o comando do click declara. O que o ``pipeline``
        precisa de verdade (``output``, ``--dry-run`` pedido, grupos pedidos)
        e o ``sync`` não aceita é erro claro (mensagem em stdout, retorna
        ``None``); ``workers`` e ``verbose`` são opcionais e ficam de fora sem
        erro (``--workers`` na linha de comando avisa em stderr).

        Os grupos vão para o parâmetro ``groups`` ou, se não houver, para o
        único argumento posicional variádico do ``sync`` (ex.: ``datasets``).
        """
        console = get_console()
        params = {p.name: p for p in sync_cmd.params if p.name}
        kwargs: dict[str, Any] = {}

        def refuse(what: str) -> None:
            console.print(
                f"[red]Erro:[/red] o 'sync' deste fetcher não aceita {what}, "
                "que o pipeline precisa repassar; rode 'sync' e 'convert' em "
                "separado."
            )

        if "output" not in params:
            refuse("-o/--output")
            return None
        kwargs["output"] = output

        if "dry_run" in params:
            kwargs["dry_run"] = dry_run
        elif dry_run:
            refuse("--dry-run")
            return None

        if groups:
            target = "groups" if "groups" in params else None
            if target is None:
                variadic = [
                    n
                    for n, p in params.items()
                    if getattr(p, "param_type_name", "") == "argument"
                    and getattr(p, "nargs", 1) == -1
                ]
                if len(variadic) == 1:
                    target = variadic[0]
            if target is None:
                refuse("grupos como argumento")
                return None
            kwargs[target] = groups

        if "workers" in params:
            kwargs["workers"] = workers
        else:
            source = ctx.get_parameter_source("workers")
            if source is not None and source.name == "COMMANDLINE":
                print(
                    "[Aviso] o 'sync' deste fetcher não aceita --workers; "
                    "o pipeline segue sem a opção.",
                    file=sys.stderr,
                )
        if "verbose" in params:
            kwargs["verbose"] = verbose
        required = [
            f"--{n.replace('_', '-')}"
            if getattr(p, "param_type_name", "") != "argument"
            else n
            for n, p in params.items()
            if getattr(p, "required", False) and n not in kwargs
        ]
        if required:
            refuse(f"rodar sem {', '.join(required)} (obrigatório no 'sync' dele)")
            return None
        return kwargs

    def _build_commands(self) -> None:
        console = get_console()

        @self.app.command("sync")
        def sync(
            groups: Annotated[
                list[str] | None,
                typer.Argument(
                    help="Grupos a baixar. Use 'list' para ver grupos disponíveis. Padrão: todos."
                ),
            ] = None,
            output: Annotated[
                Path | None, typer.Option("-o", "--output", help="Diretório de saída")
            ] = None,
            dry_run: Annotated[
                bool, typer.Option("--dry-run", help="Listar arquivos sem baixar")
            ] = False,
            workers: Annotated[
                int,
                typer.Option(
                    "--workers",
                    envvar=SYNC_WORKERS_ENVVAR,
                    min=1,
                    help="Downloads paralelos (o padrão e a variável de ambiente "
                    "aparecem ao lado).",
                ),
            ] = 4,
            from_plan: Annotated[
                Path | None,
                typer.Option(
                    "--from-plan",
                    help="Baixar somente as entradas com ação 'download' "
                    "de um plano gerado por 'check' (ignora a seleção "
                    "de grupos).",
                ),
            ] = None,
            force: Annotated[
                bool,
                typer.Option(
                    "--force",
                    help="Ignora a checagem de frescor local (arquivo/sidecar) "
                    "e baixa todas as entradas selecionadas (útil com --from-plan)",
                ),
            ] = False,
            verbose: Annotated[
                bool, typer.Option("--verbose", help="Logs detalhados")
            ] = False,
            summary_json: Annotated[
                Path | None,
                typer.Option(
                    "--summary-json",
                    help="Grava neste arquivo (atômico) um resumo JSON da "
                    "execução: contadores, bytes, erros e código de saída "
                    f"(esquema versão {SUMMARY_SCHEMA_VERSION}). Não altera o "
                    "código de saída do comando.",
                ),
            ] = None,
        ) -> None:
            setup_rich_logging(verbose, console=console)
            actual_output = output or self.default_output
            with SummaryRecorder(
                summary_json,
                fetcher=self.name,
                command="sync",
                stats_source=lambda: self.last_download_stats,
            ) as recorder:
                self.last_download_stats = None
                self._run_sync(
                    recorder,
                    groups=groups,
                    output=actual_output,
                    dry_run=dry_run,
                    workers=workers,
                    from_plan=from_plan,
                    force=force,
                )

        @self.app.command("check")
        def check(
            groups: Annotated[
                list[str] | None,
                typer.Argument(
                    help="Grupos a verificar. Use 'list' para ver grupos disponíveis. Padrão: todos."
                ),
            ] = None,
            output: Annotated[
                Path | None, typer.Option("-o", "--output", help="Diretório de saída")
            ] = None,
            workers: Annotated[
                int, typer.Option("--workers", help="Verificações paralelas")
            ] = 4,
            as_json: Annotated[
                bool,
                typer.Option(
                    "--json",
                    help="Imprime o plano em JSON (para 'sync --from-plan')",
                ),
            ] = False,
            out: Annotated[
                Path | None,
                typer.Option(
                    "--out",
                    help="Grava o plano JSON neste arquivo (atômico) e imprime "
                    "só um resumo; prevalece sobre --json",
                ),
            ] = None,
            verbose: Annotated[
                bool, typer.Option("--verbose", help="Logs detalhados")
            ] = False,
        ) -> None:
            setup_rich_logging(verbose, console=console)
            actual_output = output or self.default_output

            try:
                target_groups = self.resolve_groups(groups)
            except ValueError as exc:
                console.print(f"[red]{exc}[/red]")
                console.print(f"Grupos válidos: {', '.join(self.all_keys)}")
                raise typer.Exit(1) from None

            entries, empty = self._list_catalog(target_groups)
            empty_message = self._guard_catalog(target_groups, entries, empty)
            # O plugin pode abortar o check por conta própria (ex.: falha de
            # descoberta): ele roda antes da mensagem de catálogo vazio.
            plan = self.check_datasets(entries, actual_output, workers=workers)
            if empty_message is not None:
                console.print(f"[red]Erro:[/red] {escape(empty_message)}")
                raise typer.Exit(1)
            if not entries:
                plan = dataclasses.replace(plan, expected_empty=True)

            if out is not None:
                tmp = out.with_name(f".{out.name}.{os.getpid()}.tmp")
                try:
                    out.parent.mkdir(parents=True, exist_ok=True)
                    try:
                        tmp.write_text(plan.to_json(), encoding="utf-8")
                        os.replace(tmp, out)
                    finally:
                        tmp.unlink(missing_ok=True)
                except OSError as exc:
                    console.print(f"[red]Falha ao gravar o plano em {out}: {exc}[/red]")
                    raise typer.Exit(1) from None
                console.print(f"{plan.summary_line()} Plano gravado em {out}")
            elif as_json:
                print(plan.to_json())
            else:
                plan.render_table()

        @self.app.command("list")
        def cmd_list(
            verbose: Annotated[
                bool, typer.Option("--verbose", help="Logs detalhados")
            ] = False,
        ) -> None:
            setup_rich_logging(verbose, console=console)

            for group_id, group_info in self.groups.items():
                table = Table(
                    "ID",
                    "Partição",
                    "Extensão",
                    "URL",
                    title=f"[bold]{group_id}[/bold] — {group_info.get('name', '')}",
                )
                for entry in self.list_datasets(group_id):
                    table.add_row(
                        entry.get("id", ""),
                        _partition_label(entry) or "—",
                        entry.get("ext", ""),
                        entry.get("url", ""),
                    )
                console.print(table)

            total = sum(len(self.list_datasets(g)) for g in self.all_group_keys)
            console.print(f"\n[bold]{total}[/bold] dataset(s) no catálogo.")
