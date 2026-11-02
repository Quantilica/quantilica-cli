"""Comandos `quantilica catalog` — sincronização com o Portal (Subplano 3).

Varre o Universal Hive Parquet Lake local (QUANTILICA_LAKE_DIR ou lake_root),
lê descritores `_dataset.json`, inspeciona schemas Arrow e amostras (até 20 linhas),
monta o payload consolidado e dispara para a VPS via:
POST /api/v2/admin/catalog/sync (com autenticação via Bearer token).
"""

from __future__ import annotations

import datetime as dt
import decimal
import json
import logging
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.table import Table

logger = logging.getLogger("quantilica.cli.catalog")
console = Console()
err_console = Console(stderr=True)


def _sanitize_value(val: Any) -> Any:
    """Converte valores Python/Arrow em tipos primitivos compatíveis com JSON."""
    if val is None:
        return None
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float, str)):
        return val
    if isinstance(val, decimal.Decimal):
        return float(val)
    if hasattr(val, "isoformat"):
        return val.isoformat()
    if isinstance(val, bytes):
        try:
            return val.decode("utf-8")
        except UnicodeDecodeError:
            return val.hex()
    if isinstance(val, dict):
        return {str(k): _sanitize_value(v) for k, v in val.items()}
    if isinstance(val, (list, tuple, set)):
        return [_sanitize_value(x) for x in val]
    if hasattr(val, "as_py"):
        return _sanitize_value(val.as_py())
    return str(val)


app = typer.Typer(
    help="Gerenciar e sincronizar o catálogo de dados do ecossistema com o Portal.",
    no_args_is_help=True,
)


def _default_lake_dir() -> Path:
    """Obtém o diretório padrão do Lake a partir do SDK ou ambiente."""
    try:
        from quantilica.core.lake import lake_root

        return lake_root()
    except Exception:
        env = os.environ.get("QUANTILICA_LAKE_DIR") or os.environ.get(
            "QUANTILICA_DATA_DIR"
        )
        if env:
            p = Path(env).expanduser().resolve()
            return p / "lake" if (p / "lake").is_dir() else p
        return Path("~/data/lake").expanduser().resolve()


def _extract_dataset_metadata(
    descriptor_path: Path, sample_size: int = 20, lake_root: Path | None = None
) -> dict[str, Any] | None:
    """Extrai metadados, colunas e amostras de um _dataset.json e seus Parquets."""
    try:
        desc_text = descriptor_path.read_text(encoding="utf-8")
        desc_data = json.loads(desc_text)
    except Exception as exc:
        logger.warning("Falha ao ler descritor %s: %s", descriptor_path, exc)
        return None

    source_id = desc_data.get("source_id")
    dataset_id = desc_data.get("dataset_id")
    if not source_id or not dataset_id:
        return None

    dataset_dir = descriptor_path.parent
    parquet_files = list(dataset_dir.rglob("*.parquet"))
    total_arquivos = len(parquet_files)
    total_bytes = sum(f.stat().st_size for f in parquet_files)

    ultima_materializacao: str | None = None
    if parquet_files:
        mtime = max(f.stat().st_mtime for f in parquet_files)
        ultima_materializacao = dt.datetime.fromtimestamp(mtime, tz=dt.UTC).isoformat()

    full_id = f"{source_id}/{dataset_id}"
    alias = desc_data.get("alias", dataset_id)

    # Colunas
    columns: list[dict[str, Any]] = []
    order = 0

    partition_keys = desc_data.get("partition_keys", []) or []
    partition_types = desc_data.get("partition_types", {}) or {}
    for p_key in partition_keys:
        columns.append(
            {
                "dataset_id": full_id,
                "nome": p_key,
                "dtype_arrow": partition_types.get(p_key, "Utf8"),
                "eh_particao_hive": True,
                "eh_chave_cruzamento": False,
                "percentual_nulos": 0.0,
                "descricao": f"Chave de partição Hive ({p_key})",
                "display_order": order,
            }
        )
        order += 1

    sample_rows: list[dict[str, Any]] = []
    schema_arrow_json: dict[str, Any] = {}

    if parquet_files:
        first_parquet = parquet_files[0]
        try:
            import pyarrow.parquet as pq

            pf = pq.ParquetFile(first_parquet)
            schema = pf.schema_arrow
            schema_arrow_json = {f.name: str(f.type) for f in schema}

            for field in schema:
                if field.name not in partition_keys:
                    columns.append(
                        {
                            "dataset_id": full_id,
                            "nome": field.name,
                            "dtype_arrow": str(field.type),
                            "eh_particao_hive": False,
                            "eh_chave_cruzamento": False,
                            "percentual_nulos": 0.0,
                            "descricao": None,
                            "display_order": order,
                        }
                    )
                    order += 1

            if sample_size > 0 and pf.num_row_groups > 0:
                table = pf.read_row_group(0)
                if table.num_rows > sample_size:
                    table = table.slice(0, sample_size)
                # Serialização segura para JSON
                pydict = table.to_pydict()
                keys = list(pydict.keys())
                for idx in range(table.num_rows):
                    row: dict[str, Any] = {}
                    for k in keys:
                        row[k] = _sanitize_value(pydict[k][idx])
                    sample_rows.append(row)
        except Exception as exc:
            logger.debug(
                "Não foi possível inspecionar Parquet com pyarrow (%s): %s",
                first_parquet,
                exc,
            )

    source_obj = {
        "id": source_id,
        "nome": source_id.upper(),
        "sigla": source_id.upper()[:32],
        "orgao_emissor": source_id.upper(),
        "dominio_tematico": "Geral",
        "licenca": "Open Data",
        "homepage_url": None,
        "tier_default": 2 if total_bytes > 0 else 1,
        "ativo": True,
    }

    if lake_root:
        try:
            lake_path = str(dataset_dir.relative_to(lake_root))
        except ValueError:
            lake_path = f"{source_id}/{dataset_id}"
    else:
        try:
            lake_path = str(dataset_dir.relative_to(dataset_dir.parent.parent))
        except ValueError:
            lake_path = f"{source_id}/{dataset_id}"

    dataset_obj = {
        "id": full_id,
        "source_id": source_id,
        "nome": alias.replace("-", " ").replace("_", " ").title(),
        "descricao": (
            f"Dataset {alias} extraído e materializado no Universal Hive Parquet Lake."
        ),
        "frequencia_temporal": "mensal",
        "granularidade_espacial": "nacional",
        "lake_path": lake_path,
        "manifest_sha256": None,
        "ultima_materializacao_em": ultima_materializacao,
        "total_linhas": 0,
        "total_bytes": total_bytes,
        "total_arquivos": total_arquivos,
        "em_destaque": False,
    }

    sample_obj = {
        "dataset_id": full_id,
        "linhas_amostra": sample_rows,
        "schema_arrow_json": schema_arrow_json,
    }

    return {
        "source": source_obj,
        "dataset": dataset_obj,
        "columns": columns,
        "sample": sample_obj,
    }


def extract_catalog_payload(
    lake_dir: Path,
    source_filter: str | None = None,
    sample_size: int = 20,
) -> dict[str, Any]:
    """Varre o diretório do Lake e extrai o payload consolidado de metadados."""
    sources_dict: dict[str, dict[str, Any]] = {}
    datasets_list: list[dict[str, Any]] = []
    columns_list: list[dict[str, Any]] = []
    samples_list: list[dict[str, Any]] = []

    if not lake_dir.is_dir():
        logger.warning("Diretório do Lake %s não existe.", lake_dir)
        return {
            "sources": [],
            "datasets": [],
            "columns": [],
            "samples": [],
        }

    # Busca todos os descritores _dataset.json
    descriptor_paths = sorted(lake_dir.rglob("_dataset.json"))
    for desc_path in descriptor_paths:
        meta = _extract_dataset_metadata(
            desc_path, sample_size=sample_size, lake_root=lake_dir
        )
        if not meta:
            continue

        src = meta["source"]
        if source_filter and src["id"] != source_filter:
            continue

        sources_dict[src["id"]] = src
        datasets_list.append(meta["dataset"])
        columns_list.extend(meta["columns"])
        if meta["sample"]["linhas_amostra"] or meta["sample"]["schema_arrow_json"]:
            samples_list.append(meta["sample"])

    return {
        "sources": list(sources_dict.values()),
        "datasets": datasets_list,
        "columns": columns_list,
        "samples": samples_list,
    }


def send_catalog_payload(
    portal_url: str,
    payload: dict[str, Any],
    api_key: str | None = None,
) -> dict[str, Any]:
    """Envia o payload via HTTP POST para o endpoint de sincronização do Portal."""
    endpoints = [
        f"{portal_url.rstrip('/')}/api/v2/admin/catalog/sync",
        f"{portal_url.rstrip('/')}/api/v2/catalog/sync",
    ]

    body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if api_key:
        headers["Authorization"] = f"Bearer {api_key.strip()}"

    last_error: Exception | None = None

    for url in endpoints:
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                resp_text = resp.read().decode("utf-8")
                return json.loads(resp_text)
        except urllib.error.HTTPError as exc:
            # Se for 404 tenta o próximo endpoint
            if exc.code == 404 and url != endpoints[-1]:
                continue
            err_body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(
                f"Falha na sincronização HTTP {exc.code} em {url}: {err_body}"
            ) from exc
        except Exception as exc:
            last_error = exc
            if url != endpoints[-1]:
                continue
            raise RuntimeError(f"Erro de conexão com o portal em {url}: {exc}") from exc

    if last_error:
        raise last_error
    return {}


@app.command("sync")
def cmd_catalog_sync(
    lake_dir: Annotated[
        Path | None,
        typer.Option(
            "--lake-dir",
            help="Diretório raiz do Universal Hive Parquet Lake.",
        ),
    ] = None,
    portal_url: Annotated[
        str,
        typer.Option(
            "--portal-url",
            help="URL base do Quantilica Portal.",
            envvar="QUANTILICA_PORTAL_URL",
        ),
    ] = "http://localhost:8000",
    api_key: Annotated[
        str | None,
        typer.Option(
            "--api-key",
            "--token",
            help="Chave de API administrativa para autenticação.",
            envvar="QUANTILICA_ADMIN_API_KEY",
        ),
    ] = None,
    source: Annotated[
        str | None,
        typer.Option(
            "--source",
            "-s",
            help="Filtrar por ID de fonte específica (ex: cvm, anp, b3).",
        ),
    ] = None,
    sample_size: Annotated[
        int,
        typer.Option(
            "--sample-size",
            help="Número de linhas de amostra a extrair (máx 20).",
        ),
    ] = 20,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Exibe o payload JSON extraído sem enviar HTTP para a VPS.",
        ),
    ] = False,
) -> None:
    """Sincroniza os metadados do Universal Hive Parquet Lake com o Portal."""
    target_lake = (lake_dir or _default_lake_dir()).expanduser().resolve()
    console.print(
        f"[cyan]Varrendo Universal Hive Parquet Lake em:[/cyan] {target_lake}"
    )

    safe_sample_size = max(0, min(sample_size, 20))
    payload = extract_catalog_payload(
        target_lake, source_filter=source, sample_size=safe_sample_size
    )

    sources_count = len(payload.get("sources", []))
    datasets_count = len(payload.get("datasets", []))
    columns_count = len(payload.get("columns", []))
    samples_count = len(payload.get("samples", []))

    table = Table(title="Metadados Extraídos para Sincronização", show_header=True)
    table.add_column("Entidade", style="cyan")
    table.add_column("Quantidade", style="green", justify="right")
    table.add_row("Fontes Oficiais", str(sources_count))
    table.add_row("Datasets Parquet", str(datasets_count))
    table.add_row("Colunas e Chaves", str(columns_count))
    table.add_row("Amostras de Dados", str(samples_count))
    console.print(table)

    if dry_run:
        console.print(
            "[yellow]Modo --dry-run ativo:[/yellow] nenhum dado enviado para o portal."
        )
        console.print_json(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str)
        )
        return

    if datasets_count == 0:
        console.print(
            "[yellow]Nenhum descritor _dataset.json encontrado.[/yellow] "
            "Execute 'quantilica lake build' para materializar datasets."
        )
        return

    console.print(
        f"[cyan]Enviando payload ({sources_count} fontes, "
        f"{datasets_count} datasets)...[/cyan]"
    )
    try:
        res = send_catalog_payload(portal_url, payload, api_key=api_key)
        console.print(
            f"[green]✓ Catálogo sincronizado com sucesso![/green] Resposta: {res}"
        )
    except Exception as exc:
        err_console.print(f"[red]Erro ao sincronizar catálogo:[/red] {exc}")
        raise typer.Exit(code=1) from exc
