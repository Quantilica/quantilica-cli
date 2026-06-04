"""Local inspection of download manifests written by Quantilica fetchers.

Fetchers write a ``<arquivo>.manifest.json`` next to every downloaded file.
These commands read those files from the filesystem — no network, no database.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import Annotated, Any

import typer
from quantilica.core.dates import parse_iso_datetime, utc_now
from rich.console import Console
from rich.table import Table

app = typer.Typer(
    help="Inspecionar manifestos de download (*.manifest.json) localmente.",
    no_args_is_help=True,
)
console = Console()

MANIFEST_SUFFIX = ".manifest.json"
MANIFEST_GLOB = f"**/*{MANIFEST_SUFFIX}"

_DURATION_RE = re.compile(r"^(\d+)\s*([smhdw])$")
_DURATION_UNITS = {
    "s": "seconds",
    "m": "minutes",
    "h": "hours",
    "d": "days",
    "w": "weeks",
}
_CADENCE = {
    "daily": timedelta(days=1),
    "weekly": timedelta(weeks=1),
    "monthly": timedelta(days=31),
    "quarterly": timedelta(days=92),
    "yearly": timedelta(days=366),
}
# Quanto a coleta pode passar da cadência antes de ser considerada atrasada.
_STALE_GRACE = 1.5


def parse_since(value: str) -> datetime:
    """Parse a relative duration ('7d', '24h') or ISO date into a UTC cutoff."""
    match = _DURATION_RE.match(value.strip().lower())
    if match:
        amount, unit = int(match.group(1)), match.group(2)
        return utc_now() - timedelta(**{_DURATION_UNITS[unit]: amount})
    try:
        return parse_iso_datetime(value)
    except ValueError as exc:
        raise typer.BadParameter(
            f"'{value}' não é uma duração (ex.: 7d, 24h) nem uma data ISO."
        ) from exc


def _parse_dt(value: Any) -> datetime | None:
    """Parse an ISO timestamp, returning None when absent or malformed."""
    if not isinstance(value, str):
        return None
    try:
        return parse_iso_datetime(value)
    except ValueError:
        return None


def _format_size(num: Any) -> str:
    """Render a byte count in human-readable units."""
    if not isinstance(num, int | float):
        return "—"
    size = float(num)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def _format_age(delta: timedelta) -> str:
    """Render a timedelta as a compact age string."""
    total = int(delta.total_seconds())
    if total < 0:
        return "—"
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def iter_manifests(root: Path) -> Iterator[tuple[Path, dict[str, Any]]]:
    """Yield ``(manifest_path, data)`` for every manifest found under ``root``.

    Malformed JSON files are skipped with a warning rather than aborting.
    """
    for path in sorted(root.glob(MANIFEST_GLOB)):
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            console.print(
                f"[yellow]Aviso:[/yellow] manifesto ilegível ignorado: {path} ({exc})"
            )
            continue
        if isinstance(data, dict):
            yield path, data


def _data_filename(manifest_path: Path) -> str:
    """Return the data file name a manifest describes."""
    name = manifest_path.name
    return name[: -len(MANIFEST_SUFFIX)] if name.endswith(MANIFEST_SUFFIX) else name


def _resolve_manifest_path(target: Path) -> Path | None:
    """Resolve ``target`` to a manifest file, given a manifest or a data file."""
    if target.name.endswith(MANIFEST_SUFFIX):
        return target if target.is_file() else None
    candidate = target.with_name(target.name + MANIFEST_SUFFIX)
    return candidate if candidate.is_file() else None


@app.command("list")
def list_manifests(
    root: Annotated[
        Path,
        typer.Option("-r", "--root", help="Diretório raiz a varrer."),
    ] = Path(),
    source: Annotated[
        str | None,
        typer.Option("--source", help="Filtra por source_id."),
    ] = None,
    dataset: Annotated[
        str | None,
        typer.Option("--dataset", help="Filtra por dataset_id."),
    ] = None,
    since: Annotated[
        str | None,
        typer.Option(
            "--since",
            help="Só manifestos a partir de (ex.: 7d, 24h, 2026-05-01).",
        ),
    ] = None,
) -> None:
    """Lista manifestos de download encontrados sob o diretório raiz."""
    cutoff = parse_since(since) if since else None

    rows: list[tuple[Path, dict[str, Any], datetime | None]] = []
    for path, data in iter_manifests(root):
        if source and data.get("source_id") != source:
            continue
        if dataset and data.get("dataset_id") != dataset:
            continue
        fetched = _parse_dt(data.get("fetched_at"))
        if cutoff and (fetched is None or fetched < cutoff):
            continue
        rows.append((path, data, fetched))

    if not rows:
        console.print("[yellow]Nenhum manifesto encontrado.[/yellow]")
        return

    # Mais recentes primeiro; datas ausentes vão para o fim.
    rows.sort(key=lambda r: (r[2] is None, r[2] or datetime.min), reverse=True)

    table = Table(title="Manifestos de download", show_header=True)
    table.add_column("Fonte", style="cyan")
    table.add_column("Dataset", style="green")
    table.add_column("Arquivo", style="white")
    table.add_column("Coletado em", style="magenta")
    table.add_column("Tamanho", style="blue", justify="right")

    for path, data, fetched in rows:
        coletado = (
            fetched.strftime("%Y-%m-%d %H:%M")
            if fetched
            else str(data.get("fetched_at", "—"))
        )
        table.add_row(
            str(data.get("source_id", "—")),
            str(data.get("dataset_id", "—")),
            _data_filename(path),
            coletado,
            _format_size(data.get("size_bytes")),
        )

    console.print(table)
    console.print(f"\n{len(rows)} manifesto(s).")


@app.command("show")
def show_manifest(
    target: Annotated[
        Path,
        typer.Argument(
            help="Arquivo *.manifest.json ou o arquivo de dados correspondente.",
        ),
    ],
) -> None:
    """Exibe todos os campos de um manifesto."""
    path = _resolve_manifest_path(target)
    if path is None:
        console.print(f"[red]Manifesto não encontrado para:[/red] {target}")
        raise typer.Exit(2)

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        console.print(f"[red]Falha ao ler o manifesto:[/red] {path} ({exc})")
        raise typer.Exit(1) from exc

    table = Table(title=str(path), show_header=True)
    table.add_column("Campo", style="cyan")
    table.add_column("Valor", style="white")

    for key in sorted(data):
        value = data[key]
        if isinstance(value, dict | list):
            rendered = json.dumps(value, ensure_ascii=False, indent=2)
        else:
            rendered = str(value)
        table.add_row(key, rendered)

    console.print(table)


@app.command("status")
def status(
    root: Annotated[
        Path,
        typer.Option("-r", "--root", help="Diretório raiz a varrer."),
    ] = Path(),
    source: Annotated[
        str | None,
        typer.Option("--source", help="Filtra por source_id."),
    ] = None,
) -> None:
    """Resume cada dataset: nº de coletas, última coleta e idade."""
    groups: dict[tuple[str, str], list[tuple[datetime | None, dict[str, Any]]]]
    groups = {}
    for _path, data in iter_manifests(root):
        if source and data.get("source_id") != source:
            continue
        key = (
            str(data.get("source_id", "—")),
            str(data.get("dataset_id", "—")),
        )
        groups.setdefault(key, []).append((_parse_dt(data.get("fetched_at")), data))

    if not groups:
        console.print("[yellow]Nenhum manifesto encontrado.[/yellow]")
        return

    now = utc_now()
    table = Table(title="Estado dos datasets", show_header=True)
    table.add_column("Fonte", style="cyan")
    table.add_column("Dataset", style="green")
    table.add_column("Coletas", style="blue", justify="right")
    table.add_column("Última coleta", style="magenta")
    table.add_column("Idade", style="white", justify="right")
    table.add_column("Situação", style="white")

    for (src, ds), entries in sorted(groups.items()):
        dated = [dt for dt, _ in entries if dt is not None]
        latest = max(dated) if dated else None

        if latest is None:
            ultima, idade, situacao = "—", "—", "[dim]sem data[/dim]"
        else:
            age = now - latest
            ultima = latest.strftime("%Y-%m-%d %H:%M")
            idade = _format_age(age)
            situacao = _staleness(entries, latest, age)

        table.add_row(src, ds, str(len(entries)), ultima, idade, situacao)

    console.print(table)


def _expected_cadence(data: dict[str, Any]) -> str | None:
    """Read the expected cadence from a manifest.

    Prefers the v2 ``source_meta`` group, falling back to the free-form
    ``metadata`` dict used by older manifests.
    """
    source_meta = data.get("source_meta")
    if isinstance(source_meta, dict) and source_meta.get("expected_cadence"):
        return source_meta["expected_cadence"]
    metadata = data.get("metadata")
    if isinstance(metadata, dict):
        return metadata.get("expected_cadence")
    return None


def _staleness(
    entries: list[tuple[datetime | None, dict[str, Any]]],
    latest: datetime,
    age: timedelta,
) -> str:
    """Classify a dataset as fresh/stale when an expected cadence is known."""
    cadence: timedelta | None = None
    for dt, data in entries:
        if dt == latest:
            name = _expected_cadence(data)
            cadence = _CADENCE.get(str(name)) if name else None
            break

    if cadence is None:
        return "[dim]—[/dim]"
    if age > cadence * _STALE_GRACE:
        return "[red]atrasado[/red]"
    return "[green]em dia[/green]"
