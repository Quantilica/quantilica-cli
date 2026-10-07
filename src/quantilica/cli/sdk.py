"""SDK for building Quantilica data fetchers.

Provides the FetcherApp class which eliminates boilerplate across fetchers.
"""

from __future__ import annotations

import concurrent.futures
import contextlib
import datetime as dt
import os
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any

import typer
from quantilica.core.exceptions import FetchError
from quantilica.core.ftp import FtpClient
from quantilica.core.http import (
    HttpClient,
    HttpStatusError,
    ProgressCallback,
    resolve_verify_from_env,
)
from quantilica.core.logging import get_logger
from quantilica.core.storage import (
    StampedDataRepository,
    build_stamped_filename,
    stamp_filename,
)
from rich.console import Console, Group
from rich.live import Live
from rich.rule import Rule
from rich.table import Table

from quantilica.cli.ui import (
    ProgressPool,
    get_console,
    graceful_executor,
    make_batch_progress,
    make_download_progress,
    setup_rich_logging,
)

logger = get_logger(__name__)

__all__ = [
    "DataRepository",
    "FetcherApp",
    "SyncPlan",
    "SyncPlanItem",
    "default_client",
    "default_path_builder",
    "make_resolve_groups",
]


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


def default_client() -> HttpClient:
    """Create a default HttpClient with standard configuration.

    TLS verification and retry budget can be overridden via environment:

    - ``QUANTILICA_CA_BUNDLE``: path to a custom CA bundle.
    - ``QUANTILICA_SSL_VERIFY``: ``0``/``false`` disables verification
      (ops escape hatch for hosts with broken chains — never default);
      a path uses it as CA bundle.
    - ``QUANTILICA_HTTP_ATTEMPTS`` / ``QUANTILICA_HTTP_TIMEOUT`` /
      ``QUANTILICA_HTTP_RETRY_DELAY``: retry budget overrides.

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
    return HttpClient(
        timeout=timeout,
        verify=verify,
        attempts=attempts,
        retry_base_delay=retry_base_delay,
        emulate_browser=True,
    )


class DataRepository(StampedDataRepository):
    """Canonical data repository layout shared by Quantilica fetchers.

    Files are stored under ``{dataset_id}/`` directly, with stamped filenames
    following the ecosystem convention ``{slug}[@{partition}]@{YYYYMMDD}.{ext}``
    (see :func:`stamp_filename`). Entries follow the canonical SDK schema:
    ``group``, ``id``, ``ext``, ``url``, and optional ``year``/``month``/
    ``semester`` partition fields.
    """

    def path_for_entry(
        self,
        entry: dict[str, Any],
        *,
        last_modified: dt.date | None = None,
    ) -> Path:
        """Compute the local path for a dataset entry.

        Args:
            entry: Dataset entry dictionary (canonical SDK schema).
            last_modified: The last modified date to stamp, or None.

        Returns:
            Path: The destination path for the entry.

        Raises:
            StorageError: If a required bucket key is empty or invalid.
        """
        dataset_id = str(entry.get("group") or entry.get("id") or "datasets")
        ext = entry.get("ext")
        if not ext:
            tail = str(entry.get("url", "")).split("?")[0].rsplit("/", 1)[-1]
            ext = tail.rsplit(".", 1)[-1] if "." in tail else "bin"

        year = entry.get("year")
        month = entry.get("month")
        semester = entry.get("semester")
        slug = str(entry.get("id") or dataset_id)

        if year is not None and semester is not None:
            filename = build_stamped_filename(
                slug,
                f"{year}-{semester:02d}",
                ext=ext,
                timestamp=last_modified,
            )
        elif year is not None and month is not None:
            filename = build_stamped_filename(
                slug,
                f"{year}-{month:02d}",
                ext=ext,
                timestamp=last_modified,
            )
        elif year is not None:
            filename = build_stamped_filename(
                slug, year, ext=ext, timestamp=last_modified
            )
        else:
            filename = stamp_filename(slug, ext, last_modified)

        return self.dataset_path(dataset_id, filename)


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
    aliases_dict: dict[str, list[str]],
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
            expanded = (
                list(aliases[key])
                if key in aliases
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
        build_default_commands: If True (default), registers the built-in
            ``sync`` and ``list`` commands. Set to False to register only
            custom commands via :meth:`attach_command`.
        client: HTTP or FTP client instance. Defaults to default_client().
    """

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
        client: HttpClient | FtpClient | None = None,
        build_default_commands: bool = True,
    ):
        self.name = name
        self.help = help
        self.groups = groups_dict
        self.aliases = aliases_dict
        self.list_datasets = list_datasets
        self.path_builder = path_builder or default_path_builder
        self.default_output = default_output or Path(
            f"/data/{name.replace('-fetcher', '')}"
        )
        self.client = client or default_client()

        self.all_group_keys = list(self.groups.keys())
        self.all_keys = self.all_group_keys + list(self.aliases.keys())
        self.resolve_groups = make_resolve_groups(groups_dict, aliases_dict)

        # O objeto typer principal
        self.app = typer.Typer(help=help)
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

    def command_convert(
        self, func: Callable[[Path, Path], Any]
    ) -> Callable[[Path, Path], Any]:
        """Register the standard ``convert`` subcommand.

        The generated command exposes the canonical flags (``-i/--input``,
        ``-o/--output``, ``--verbose``) and delegates to ``func(input, output)``.
        An ``ImportError`` propagating from ``func`` is treated as missing
        analytical extras and exits gracefully with code 1.

        Args:
            func: Callable receiving ``(input, output)`` paths and performing
                the conversion.

        Returns:
            The original ``func`` (the method can be used as a decorator).
        """
        console = get_console()

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
            output: Annotated[
                Path,
                typer.Option("-o", "--output", help="Diretório de destino"),
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
                    f"[red]Erro:[/red] convert requer extras de análise: "
                    rf"pip install {self.name}\[analysis]"
                )
                raise typer.Exit(1) from None
            console.print(
                f"[green]✓[/green] Conversão concluída em [dim]{output}[/dim]."
            )

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
                    rf"pip install {self.name}\[analysis]"
                )
                raise typer.Exit(1) from None
            console.print(f"[green]✓[/green] Arquivo criado em [dim]{output}[/dim].")

        return func

    def command_pipeline(
        self, convert_func: Callable[[Path, Path], Any]
    ) -> Callable[[Path, Path], Any]:
        """Register the standard ``pipeline`` subcommand (sync → convert).

        Step 1/2 invokes the built-in ``sync`` command (same flags: groups,
        ``-o/--output``, ``--parquet-dir``, ``--workers``, ``--dry-run``,
        ``--verbose``). Step 2/2 invokes ``convert_func(output, parquet_dir)``.
        With ``--dry-run`` the plan is only listed and the conversion step is
        skipped. An ``ImportError`` from ``convert_func`` is treated as
        missing analytical extras.

        Args:
            convert_func: Callable receiving ``(output, parquet_dir)`` paths
                and performing the conversion.

        Returns:
            The original ``convert_func`` (usable as a decorator).
        """
        console = get_console()

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
            parquet_dir: Annotated[
                Path | None,
                typer.Option(
                    "--parquet-dir",
                    help="Diretório para os Parquet (padrão: igual a --output)",
                ),
            ] = None,
            workers: Annotated[
                int, typer.Option("--workers", help="Downloads paralelos")
            ] = 4,
            dry_run: Annotated[
                bool, typer.Option("--dry-run", help="Listar arquivos sem baixar")
            ] = False,
            verbose: Annotated[
                bool, typer.Option("--verbose", help="Logs detalhados")
            ] = False,
        ) -> None:
            setup_rich_logging(verbose, console=console)
            actual_output = output or self.default_output
            parquet_out = parquet_dir or actual_output

            console.print(Rule("[bold]Passo 1/2: Download[/bold]"))
            sync_cmd = next(
                (
                    cmd.callback
                    for cmd in self.app.registered_commands
                    if cmd.name == "sync"
                ),
                None,
            )
            if sync_cmd is None:
                console.print(
                    "[red]Erro:[/red] pipeline requer o comando 'sync' "
                    "(build_default_commands=True)."
                )
                raise typer.Exit(1) from None
            ctx.invoke(
                sync_cmd,
                groups=groups,
                output=actual_output,
                dry_run=dry_run,
                workers=workers,
                verbose=verbose,
            )

            if dry_run:
                console.print(
                    "\n[yellow]Dry-run:[/yellow] conversão (passo 2/2) não executada."
                )
                return

            console.print(Rule("[bold]Passo 2/2: Conversão[/bold]"))
            try:
                convert_func(actual_output, parquet_out)
            except ImportError:
                console.print(
                    f"[red]Erro:[/red] pipeline (conversão) requer extras de "
                    rf"análise: pip install {self.name}\[analysis]"
                )
                raise typer.Exit(1) from None
            console.print(
                f"[green]✓[/green] Pipeline concluído: Parquet em "
                f"[dim]{parquet_out}[/dim]."
            )

        return convert_func

    def _safe_head_date(self, url: str) -> dt.date | None:
        with contextlib.suppress(Exception):
            return self.client.head_last_modified_date(url)
        return None

    def _download_file(
        self, url: str, output: Path, progress: ProgressCallback | None = None
    ) -> Path:
        dataset_id = output.parent.name
        return self.client.download_with_manifest(
            url,
            output,
            source_id=self.name.replace("-fetcher", ""),
            dataset_id=dataset_id,
            producer=self.name,
            progress=progress,
        )

    def download_entry(
        self,
        entry: dict[str, Any],
        output_dir: Path,
        *,
        dry_run: bool = False,
        progress: ProgressCallback | None = None,
    ) -> Path:
        """Download one dataset entry and return the destination path.

        Args:
            entry: Dictionary containing dataset metadata (url, id, etc).
            output_dir: Destination directory.
            dry_run: If True, computes the destination path without downloading.
            progress: Optional callback to track download progress.

        Returns:
            The local path where the file was (or would be) saved.

        Raises:
            FetchError: If no valid URLs could be downloaded.
            HttpStatusError: On non-404 HTTP errors.
        """
        urls_to_try = [entry["url"]]
        if "fallback_urls" in entry and entry["fallback_urls"]:
            urls_to_try.extend(entry["fallback_urls"])

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
                return self._download_file(url, output, progress=progress)
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
    ) -> tuple[int, int, list[tuple[str, str]]]:
        """Executes parallel download for a list of dataset entries.

        Args:
            entries: List of dataset entries to download.
            output_dir: The target base directory for downloads.
            workers: Maximum number of parallel downloads.

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

        # Pooling por worker: cada thread mantém seu HttpClient com keep-alive.
        thread_local = threading.local()
        _worker_clients: list[HttpClient] = []

        def _get_worker_client() -> HttpClient | FtpClient:
            if isinstance(self.client, FtpClient):
                return self.client
            if not hasattr(thread_local, "client"):
                # Reusa config do client canônico, mas com sessão persistente.
                c = HttpClient(
                    timeout=self.client.timeout,
                    headers=dict(self.client.headers),
                    follow_redirects=self.client.follow_redirects,
                    attempts=self.client.attempts,
                    retry_base_delay=self.client.retry_base_delay,
                    verify=self.client.verify,
                    limits=self.client.limits,
                    emulate_browser=True,
                )
                c.__enter__()
                thread_local.client = c
                _worker_clients.append(c)
            return thread_local.client  # type: ignore[return-value]

        def _worker(entry: dict[str, Any]) -> bool:
            # Troca temporária do client para o da thread (com pooling).
            worker_client = _get_worker_client()
            prev = self.client
            self.client = worker_client  # type: ignore[assignment]
            try:
                eid = entry.get("id", "unknown")
                with pool.acquire(description=f"[cyan]{eid}[/cyan]") as cb:
                    self.download_entry(entry, output_dir, progress=cb)
                    return True
            except Exception as exc:
                errors.append((entry.get("id", "unknown"), str(exc)))
                return False
            finally:
                self.client = prev

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
                for c in _worker_clients:
                    with contextlib.suppress(Exception):
                        c.close()

        return downloaded, total, errors

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
                int, typer.Option("--workers", help="Downloads paralelos")
            ] = 4,
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

            entries = [e for g in target_groups for e in self.list_datasets(g)]

            if dry_run:
                table = Table("Grupo", "ID", "URL", title="Arquivos a baixar (dry-run)")
                for e in entries:
                    table.add_row(e.get("group", ""), e.get("id", ""), e.get("url", ""))
                console.print(table)
                console.print(f"\n[bold]{len(entries)}[/bold] arquivo(s) listado(s).")
                return

            downloaded, total, errors = self.download_datasets(
                entries, actual_output, workers=workers
            )

            console.print(
                f"\n[green]Concluído:[/green] {downloaded}/{total} arquivo(s) baixado(s)."
            )
            if errors:
                console.print(f"[red]{len(errors)} erro(s):[/red]")
                for eid, emsg in errors:
                    console.print(f"  {eid}: {emsg}")

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
                    if entry.get("semester") is not None:
                        partition = f"{entry['year']}-S{entry['semester']}"
                    elif entry.get("month") is not None:
                        partition = f"{entry['year']}-{entry['month']:02d}"
                    elif entry.get("year") is not None:
                        partition = str(entry["year"])
                    else:
                        partition = "—"
                    table.add_row(
                        entry.get("id", ""),
                        partition,
                        entry.get("ext", ""),
                        entry.get("url", ""),
                    )
                console.print(table)

            total = sum(len(self.list_datasets(g)) for g in self.all_group_keys)
            console.print(f"\n[bold]{total}[/bold] dataset(s) no catálogo.")
