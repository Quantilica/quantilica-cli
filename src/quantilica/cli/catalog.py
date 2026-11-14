"""Comandos `quantilica catalog` — sincronização com o Portal (Subplano 3).

Varre o Universal Hive Parquet Lake local (QUANTILICA_LAKE_DIR ou lake_root),
lê descritores `_dataset.json`, inspeciona schemas Arrow e amostras (até 20 linhas),
monta o payload consolidado e dispara para a API do Portal via:
POST /api/v2/admin/catalog/sync (com autenticação via Bearer token).
"""

from __future__ import annotations

import datetime as dt
import decimal
import json
import logging
import os
import re
import urllib.error
import urllib.request
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any

import typer
from quantilica.core.registry import FetcherInfo, RegistryError
from rich.console import Console
from rich.table import Table

from quantilica.cli import __version__

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


#: Registro: id canônico, apelido (e id de lake) -> ``FetcherInfo``.
Registry = Mapping[str, FetcherInfo]


def _registry_from_infos(
    infos: list[tuple[FetcherInfo, tuple[str, ...]]],
) -> dict[str, FetcherInfo]:
    """Indexa os fetchers por id canônico, apelidos e ids extras (lake).

    Id canônico e apelidos têm prioridade sobre os ids extras; entre dois
    fetchers que disputam o mesmo nome, vale o primeiro.
    """
    index: dict[str, FetcherInfo] = {}
    for info, _extra in infos:
        for name in info.names:
            index.setdefault(name, info)
    for info, extra in infos:
        for name in extra:
            index.setdefault(name, info)
    return index


def load_registry_file(path: Path) -> dict[str, FetcherInfo]:
    """Lê um ``registry.json`` (de ``registry export --json`` ou do índice)."""
    data = json.loads(path.read_text(encoding="utf-8"))
    entries = data.get("fetchers") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        raise RegistryError(f"{path}: registro sem a lista 'fetchers'")
    known = set(FetcherInfo.__dataclass_fields__)
    infos: list[tuple[FetcherInfo, tuple[str, ...]]] = []
    for entry in entries:
        if entry.get("info_declared") is False:
            continue
        info = FetcherInfo.from_dict({k: v for k, v in entry.items() if k in known})
        extra = tuple(
            n for n in (entry.get("lake_source_id"), entry.get("source_id")) if n
        )
        infos.append((info, extra))
    return _registry_from_infos(infos)


def load_registry(registry_file: Path | None = None) -> dict[str, FetcherInfo]:
    """Registro de fetchers: do arquivo dado ou dos entry points instalados.

    Só entram fetchers que declaram ``info`` (o resto é desconhecido). Falha ao
    carregar vira registro vazio com aviso: todas as fontes ficam sem metadados.
    """
    try:
        if registry_file is not None:
            return load_registry_file(registry_file)
        from quantilica.cli.registry import discover_fetchers

        return _registry_from_infos(
            [
                (
                    d.info,
                    tuple(n for n in (d.lake_source_id, d.source_id) if n),
                )
                for d in discover_fetchers()
                if d.info_declared
            ]
        )
    except Exception as exc:
        logger.warning("Não foi possível carregar o registro de fetchers: %s", exc)
        return {}


def _count_rows(parquet_files: list[Path]) -> int:
    """Soma ``num_rows`` do rodapé de cada Parquet, sem ler os dados."""
    if not parquet_files:
        return 0
    try:
        import pyarrow.parquet as pq
    except ImportError:
        return 0
    total = 0
    for path in parquet_files:
        try:
            total += pq.ParquetFile(path).metadata.num_rows
        except Exception as exc:
            logger.warning("Não foi possível contar linhas de %s: %s", path, exc)
    return total


@dataclass
class CatalogBuild:
    """Resultado da varredura: o payload e o que ficou de fora ou sem registro."""

    payload: dict[str, Any]
    #: Datasets omitidos por redistribuição restrita, por id canônico da fonte.
    restricted: Counter[str] = field(default_factory=Counter)
    #: ``source_id`` do lake que o registro não conhece (ficam fora do payload).
    unknown: set[str] = field(default_factory=set)


def _sigla(orgao: str | None) -> str | None:
    """Sigla do primeiro trecho entre parênteses do órgão (``... (ANAC)``)."""
    m = re.search(r"\(([^()]+)\)", orgao or "")
    return m.group(1).strip() or None if m else None


def _source_object(
    source_id: str, info: FetcherInfo | None, total_bytes: int
) -> dict[str, Any]:
    """Metadados da fonte: só o que o registro declara; o resto vai ``None``."""
    return {
        "id": source_id,
        "nome": info.nome if info else None,
        "sigla": _sigla(info.orgao) if info else None,
        "orgao_emissor": info.orgao if info else None,
        "dominio_tematico": info.dominio if info else None,
        "licenca": info.licenca if info else None,
        "homepage_url": info.homepage if info else None,
        "cadencia": info.cadencia if info else None,
        "redistribuicao": info.redistribuicao if info else None,
        "tier_default": 2 if total_bytes > 0 else 1,
        "ativo": True,
    }


def _declared_text(desc_data: Mapping[str, Any], key: str) -> str | None:
    value = desc_data.get(key)
    return value.strip() if isinstance(value, str) and value.strip() else None


def _extract_dataset_metadata(
    descriptor_path: Path,
    sample_size: int = 20,
    lake_root: Path | None = None,
    registry: Registry | None = None,
) -> dict[str, Any] | None:
    """Extrai metadados, colunas e amostras de um _dataset.json e seus Parquets.

    O ``source_id`` do descritor é resolvido contra ``registry`` (apelido vira
    id canônico). O resultado traz ``restrita`` (fonte de redistribuição
    restrita) e ``conhecida`` (``source_id`` presente no registro).
    """
    try:
        desc_text = descriptor_path.read_text(encoding="utf-8")
        desc_data = json.loads(desc_text)
    except Exception as exc:
        logger.warning("Falha ao ler descritor %s: %s", descriptor_path, exc)
        return None

    raw_source_id = desc_data.get("source_id")
    dataset_id = desc_data.get("dataset_id")
    if not raw_source_id or not dataset_id:
        return None

    info = (registry or {}).get(raw_source_id)
    source_id = info.id if info else raw_source_id
    restricted = info is not None and info.redistribuicao == "restrita"

    dataset_dir = descriptor_path.parent
    parquet_files = list(dataset_dir.rglob("*.parquet"))
    total_arquivos = len(parquet_files)
    total_bytes = sum(f.stat().st_size for f in parquet_files)
    total_rows = _count_rows(parquet_files)

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

    source_obj = _source_object(source_id, info, total_bytes)

    if lake_root:
        try:
            lake_path = dataset_dir.relative_to(lake_root).as_posix()
        except ValueError:
            lake_path = f"{raw_source_id}/{dataset_id}"
    else:
        try:
            lake_path = dataset_dir.relative_to(dataset_dir.parent.parent).as_posix()
        except ValueError:
            lake_path = f"{raw_source_id}/{dataset_id}"

    dataset_obj = {
        "id": full_id,
        "source_id": source_id,
        "fetcher_id": info.id if info else None,
        "nome": alias.replace("-", " ").replace("_", " ").title(),
        "descricao": _declared_text(desc_data, "descricao"),
        "frequencia_temporal": _declared_text(desc_data, "frequencia_temporal"),
        "granularidade_espacial": _declared_text(desc_data, "granularidade_espacial"),
        "lake_path": lake_path,
        "manifest_sha256": None,
        "ultima_materializacao_em": ultima_materializacao,
        "total_linhas": total_rows,
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
        "restrita": restricted,
        "conhecida": info is not None,
    }


def build_catalog(
    lake_dir: Path,
    source_filter: str | None = None,
    sample_size: int = 20,
    registry: Registry | None = None,
) -> CatalogBuild:
    """Varre o Lake e monta o payload, o que ficou de fora e o que é desconhecido.

    ``registry`` ausente carrega os fetchers instalados. Fontes de
    redistribuição restrita não entram no payload.
    """
    if registry is None:
        registry = load_registry()
    sources_dict: dict[str, dict[str, Any]] = {}
    datasets_list: list[dict[str, Any]] = []
    columns_list: list[dict[str, Any]] = []
    samples_list: list[dict[str, Any]] = []
    build = CatalogBuild(payload={})

    if not lake_dir.is_dir():
        logger.warning("Diretório do Lake %s não existe.", lake_dir)
        build.payload = {"sources": [], "datasets": [], "columns": [], "samples": []}
        return build

    wanted = None
    if source_filter:
        found = registry.get(source_filter)
        wanted = found.id if found else source_filter

    seen_ids: set[str] = set()
    # Busca todos os descritores _dataset.json
    for desc_path in sorted(lake_dir.rglob("_dataset.json")):
        meta = _extract_dataset_metadata(
            desc_path, sample_size=sample_size, lake_root=lake_dir, registry=registry
        )
        if not meta:
            continue

        src = meta["source"]
        if wanted and src["id"] != wanted:
            continue
        if meta["restrita"]:
            build.restricted[src["id"]] += 1
            continue
        if not meta["conhecida"]:
            # Sem registro não há como saber se a fonte pode ser redistribuída
            # (um fetcher restrito não instalado aqui cairia neste caso).
            build.unknown.add(src["id"])
            continue
        if meta["dataset"]["id"] in seen_ids:
            logger.warning(
                "Dataset %s aparece mais de uma vez no Lake (%s); mantido o primeiro.",
                meta["dataset"]["id"],
                desc_path,
            )
            continue
        seen_ids.add(meta["dataset"]["id"])

        sources_dict.setdefault(src["id"], src)
        datasets_list.append(meta["dataset"])
        columns_list.extend(meta["columns"])
        # Com --sample-size 0 a amostra não vai: enviar linhas vazias apagaria
        # no portal a amostra de um envio anterior.
        if sample_size > 0 and (
            meta["sample"]["linhas_amostra"] or meta["sample"]["schema_arrow_json"]
        ):
            samples_list.append(meta["sample"])

    for source_id in sorted(build.unknown):
        logger.warning(
            "Fonte %r desconhecida do registro de fetchers: ficou de fora do envio.",
            source_id,
        )
    if build.restricted:
        logger.info(
            "%d dataset(s) de fonte(s) de redistribuição restrita ficaram de fora: %s",
            sum(build.restricted.values()),
            ", ".join(f"{k} ({v})" for k, v in sorted(build.restricted.items())),
        )

    build.payload = {
        "sources": list(sources_dict.values()),
        "datasets": datasets_list,
        "columns": columns_list,
        "samples": samples_list,
    }
    return build


def extract_catalog_payload(
    lake_dir: Path,
    source_filter: str | None = None,
    sample_size: int = 20,
    registry: Registry | None = None,
) -> dict[str, Any]:
    """Varre o diretório do Lake e extrai o payload consolidado de metadados."""
    return build_catalog(lake_dir, source_filter, sample_size, registry).payload


def send_catalog_payload(
    portal_url: str,
    payload: dict[str, Any],
    api_key: str | None = None,
) -> dict[str, Any]:
    """Envia o payload via HTTP POST para o endpoint de sincronização do Portal."""
    url = f"{portal_url.rstrip('/')}/api/v2/admin/catalog/sync"
    body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
    # User-Agent próprio: o Cloudflare do portal barra o padrão "Python-urllib/x"
    # (erro 1010, assinatura do navegador).
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": f"quantilica-cli/{__version__}",
    }
    if api_key:
        headers["Authorization"] = f"Bearer {api_key.strip()}"

    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        err_body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"Falha na sincronização HTTP {exc.code} em {url}: {err_body}"
        ) from exc
    except Exception as exc:
        raise RuntimeError(f"Erro de conexão com o portal em {url}: {exc}") from exc


# O portal recusa corpos acima de 5 MiB em /admin/catalog/sync (413). O lake
# inteiro passa disso (8,5 MB em 13/11/2026), então o envio vai em lotes, cada
# fonte inteira num lote só. O sync do portal é upsert: lotes não apagam nada
# do que veio nos anteriores.
LOTE_MAX_BYTES = 4_000_000


def _tamanho_json(obj: Any) -> int:
    return len(json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8"))


def split_catalog_payload(
    payload: dict[str, Any], max_bytes: int = LOTE_MAX_BYTES
) -> list[dict[str, Any]]:
    """Divide o payload em lotes por fonte, cada um com até ``max_bytes``.

    Uma fonte nunca é partida (a fonte, os datasets dela e as colunas e amostras
    desses datasets vão juntos); uma fonte sozinha acima do limite vira um lote
    próprio, e o portal decide. Itens sem ``dataset_id`` conhecido vão no primeiro
    lote. Payload que já cabe volta como está, num lote só.
    """
    if _tamanho_json(payload) <= max_bytes:
        return [payload]

    listas = [k for k, v in payload.items() if isinstance(v, list)]
    filhos = [k for k in listas if k not in ("sources", "datasets")]
    fonte_do_dataset = {
        d.get("id"): d.get("source_id") for d in payload.get("datasets", [])
    }

    grupos: dict[Any, dict[str, list]] = {}
    ordem: list[Any] = []

    def grupo(fonte: Any) -> dict[str, list]:
        if fonte not in grupos:
            grupos[fonte] = {k: [] for k in listas}
            ordem.append(fonte)
        return grupos[fonte]

    for src in payload.get("sources", []):
        grupo(src.get("id"))["sources"].append(src)
    for ds in payload.get("datasets", []):
        grupo(ds.get("source_id"))["datasets"].append(ds)
    soltos: dict[str, list] = {k: [] for k in listas}
    for k in filhos:
        for item in payload[k]:
            fonte = fonte_do_dataset.get(item.get("dataset_id"), _SEM_FONTE)
            if fonte is _SEM_FONTE:
                soltos[k].append(item)
            else:
                grupo(fonte)[k].append(item)

    extras = {k: v for k, v in payload.items() if k not in listas}
    lotes: list[dict[str, Any]] = []
    atual: dict[str, Any] | None = None
    for fonte in ordem:
        parte = grupos[fonte]
        if atual is not None:
            candidato = {k: atual[k] + parte[k] for k in listas}
            if _tamanho_json({**extras, **candidato}) <= max_bytes:
                atual.update(candidato)
                continue
            lotes.append(atual)
        atual = {**extras, **{k: list(parte[k]) for k in listas}}
    if atual is not None:
        lotes.append(atual)
    if not lotes:
        lotes.append({**extras, **{k: [] for k in listas}})
    for k in listas:
        lotes[0][k] = lotes[0][k] + soltos[k]
    return lotes


_SEM_FONTE = object()


def _somar_respostas(respostas: list[dict[str, Any]]) -> dict[str, Any]:
    """Soma os contadores numéricos das respostas dos lotes."""
    if len(respostas) == 1:
        return respostas[0]
    total: dict[str, Any] = {"lotes": len(respostas)}
    for res in respostas:
        for chave, valor in res.items():
            if isinstance(valor, bool):
                total[chave] = valor
            elif isinstance(valor, (int, float)):
                total[chave] = total.get(chave, 0) + valor
            elif isinstance(valor, dict):
                sub = total.setdefault(chave, {})
                for k, v in valor.items():
                    if isinstance(v, (int, float)) and not isinstance(v, bool):
                        sub[k] = sub.get(k, 0) + v
                    else:
                        sub[k] = v
            else:
                total[chave] = valor
    return total


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
    registry_file: Annotated[
        Path | None,
        typer.Option(
            "--registry",
            help="registry.json a usar no lugar dos fetchers instalados "
            "(saída de 'quantilica registry export --json').",
            envvar="QUANTILICA_REGISTRY_FILE",
        ),
    ] = None,
    sample_size: Annotated[
        int,
        typer.Option(
            "--sample-size",
            help="Número de linhas de amostra a extrair (máx 20). Com 0 a amostra "
            "não é enviada e a do portal fica como está.",
        ),
    ] = 20,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Exibe o payload JSON extraído sem enviar HTTP para o Portal.",
        ),
    ] = False,
) -> None:
    """Sincroniza os metadados do Universal Hive Parquet Lake com o Portal."""
    target_lake = (lake_dir or _default_lake_dir()).expanduser().resolve()
    console.print(
        f"[cyan]Varrendo Universal Hive Parquet Lake em:[/cyan] {target_lake}"
    )

    try:
        import pyarrow.parquet  # noqa: F401
    except ImportError:
        # Sem pyarrow o payload sai sem colunas, sem amostras e com zero
        # linhas, e o envio apagaria esses dados no portal.
        console.print(
            "[red]pyarrow não está instalado: nada foi lido.[/red] "
            "Instale o extra 'lake' (quantilica-cli[lake])."
        )
        raise typer.Exit(1) from None

    safe_sample_size = max(0, min(sample_size, 20))
    build = build_catalog(
        target_lake,
        source_filter=source,
        sample_size=safe_sample_size,
        registry=(registry := load_registry(registry_file)),
    )
    payload = build.payload
    if not registry and not dry_run:
        # Sem registro não dá para saber quais fontes são de redistribuição
        # restrita: enviar assim poderia publicar o que não pode ser publicado.
        console.print(
            "[red]Registro de fetchers vazio: nada foi enviado.[/red] Instale os "
            "fetchers nesta máquina ou informe --registry com um registry.json."
        )
        raise typer.Exit(1)

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
    table.add_row(
        "Deixados de fora (redistribuição restrita)",
        str(sum(build.restricted.values())),
    )
    table.add_row("Fontes desconhecidas do registro", str(len(build.unknown)))
    console.print(table)
    if build.restricted:
        console.print(
            "[yellow]Fora do payload (restrita):[/yellow] "
            + ", ".join(f"{k} ({v})" for k, v in sorted(build.restricted.items()))
        )
    if build.unknown:
        console.print(
            "[yellow]Fora do payload (sem registro):[/yellow] "
            + ", ".join(sorted(build.unknown))
        )

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
    lotes = split_catalog_payload(payload)
    respostas: list[dict[str, Any]] = []
    try:
        for i, lote in enumerate(lotes, 1):
            if len(lotes) > 1:
                console.print(
                    f"[cyan]Lote {i}/{len(lotes)}:[/cyan] "
                    + ", ".join(sorted(str(x.get("id")) for x in lote["sources"]))
                )
            respostas.append(send_catalog_payload(portal_url, lote, api_key=api_key))
        res = _somar_respostas(respostas)
        console.print(
            f"[green]✓ Catálogo sincronizado com sucesso![/green] Resposta: {res}"
        )
    except Exception as exc:
        err_console.print(f"[red]Erro ao sincronizar catálogo:[/red] {exc}")
        raise typer.Exit(code=1) from exc
