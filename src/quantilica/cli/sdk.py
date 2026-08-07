"""SDK for building Quantilica data fetchers.

Provides the FetcherApp class which eliminates boilerplate across fetchers.
"""

from __future__ import annotations

import concurrent.futures
import contextlib
import datetime as dt
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any

import typer
from quantilica.core.exceptions import FetchError
from quantilica.core.http import HttpClient, HttpStatusError, ProgressCallback
from quantilica.core.logging import get_logger
from rich.console import Group
from rich.live import Live
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


def default_client() -> HttpClient:
    return HttpClient(
        timeout=180.0,
        verify=True,
        attempts=5,
        retry_base_delay=2.0,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/142.0.0.0 Safari/537.36"
            ),
        },
    )


class FetcherApp:
    """Standard orchestrator for Quantilica fetchers."""

    def __init__(
        self,
        name: str,
        *,
        help: str = "",
        groups_dict: dict[str, dict[str, Any]],
        aliases_dict: dict[str, list[str]],
        list_datasets: Callable[[str], list[dict[str, Any]]],
        path_builder: Callable[[Path, dict[str, Any], dt.date | None], Path],
        default_output: Path | None = None,
        client: HttpClient | None = None,
    ):
        self.name = name
        self.help = help
        self.groups = groups_dict
        self.aliases = aliases_dict
        self.list_datasets = list_datasets
        self.path_builder = path_builder
        self.default_output = default_output or Path(
            f"/data/{name.replace('-fetcher', '')}"
        )
        self.client = client or default_client()

        self.all_group_keys = list(self.groups.keys())
        self.all_keys = self.all_group_keys + list(self.aliases.keys())

        # O objeto typer principal
        self.app = typer.Typer(help=help)
        self._build_commands()

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
        """Download one dataset entry and return the destination path."""
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

            target_groups: list[str] = []
            for g in groups or self.all_group_keys:
                expanded = self._expand_group(g)
                if not expanded:
                    console.print(f"[red]Grupo desconhecido: {g!r}[/red]")
                    console.print(f"Grupos válidos: {', '.join(self.all_keys)}")
                    raise typer.Exit(1)
                for canon in expanded:
                    if canon not in target_groups:
                        target_groups.append(canon)

            entries = [e for g in target_groups for e in self.list_datasets(g)]
            total = len(entries)

            if dry_run:
                table = Table("Grupo", "ID", "URL", title="Arquivos a baixar (dry-run)")
                for e in entries:
                    table.add_row(e.get("group", ""), e.get("id", ""), e.get("url", ""))
                console.print(table)
                console.print(f"\n[bold]{total}[/bold] arquivo(s) listado(s).")
                return

            overall = make_batch_progress(console)
            file_prog = make_download_progress(console)
            overall_task = overall.add_task("[cyan]Baixando...[/cyan]", total=total)

            downloaded = 0
            errors: list[tuple[str, str]] = []
            pool = ProgressPool(workers=workers, file_prog=file_prog)

            def _worker(entry: dict[str, Any]) -> bool:
                try:
                    eid = entry.get("id", "unknown")
                    with pool.acquire(description=f"[cyan]{eid}[/cyan]") as cb:
                        self.download_entry(entry, actual_output, progress=cb)
                        return True
                except Exception as exc:
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
