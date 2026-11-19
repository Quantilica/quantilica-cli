"""Leitor mínimo das exceções da auditoria que viajam no retrato do ledger.

O retrato (``ledger-vigente-<UTC>.jsonl.gz``) é um JSON Lines com gzip cuja primeira
linha é o cabeçalho. As exceções vigentes da auditoria (caminhos brutos que
``quantilica lake audit`` ignora) vão num campo do cabeçalho, ``lake_exceptions``,
com ``lake_exceptions_count`` e ``lake_exceptions_sha256``; o corpo (versões e
exclusões) não é lido aqui.

Por que o leitor vive no cli: o formato é escrito por outro pacote, que o cli não
pode importar, e a leitura das exceções só precisa da biblioteca padrão e da
primeira linha do arquivo. O campo fica no cabeçalho, e não no corpo, para que os
leitores do formato (versão 2) anteriores a ele continuem funcionando: eles ignoram
campo desconhecido do cabeçalho, mas recusam tipo de registro novo no corpo.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import zlib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

FORMAT_NAME = "quantilica-ingest-ledger"
SUPPORTED_FORMAT_VERSIONS = (2,)
LATEST_NAME = "ledger-vigente-latest.jsonl.gz"
_HEADER_MAX_BYTES = 1 << 20


class LedgerExceptionsError(ValueError):
    """Retrato ausente, ilegível, de outro formato ou com a lista adulterada."""


@dataclass(frozen=True)
class LedgerExceptions:
    """Exceções vigentes lidas de um retrato."""

    path: Path
    generated_at: str | None
    #: o cabeçalho trazia o campo (retratos antigos não trazem)
    declared: bool
    paths: tuple[str, ...]
    #: expiradas no momento da leitura (já fora de ``paths``)
    expired: int = 0


def resolve_snapshot(path: Path) -> Path:
    """Arquivo do retrato; uma pasta vale pelo ``ledger-vigente-latest.jsonl.gz``."""
    target = Path(path).expanduser()
    if target.is_dir():
        target = target / LATEST_NAME
    if not target.is_file():
        raise LedgerExceptionsError(f"retrato do ledger ausente: {target}")
    return target


def _canonical(record: dict[str, Any]) -> bytes:
    text = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return text.encode("utf-8") + b"\n"


def _read_header(target: Path) -> dict[str, Any]:
    try:
        with gzip.open(target, "rb") as stream:
            line = stream.readline(_HEADER_MAX_BYTES)
    except (OSError, EOFError, zlib.error) as exc:
        raise LedgerExceptionsError(
            f"{target}: gzip ilegível ou corrompido ({exc})"
        ) from exc
    if not line.endswith(b"\n"):
        raise LedgerExceptionsError(f"{target}: cabeçalho ausente ou truncado")
    try:
        header = json.loads(line)
    except ValueError as exc:
        raise LedgerExceptionsError(f"{target}: cabeçalho ilegível ({exc})") from exc
    if (
        not isinstance(header, dict)
        or header.get("kind") != "header"
        or header.get("format") != FORMAT_NAME
    ):
        raise LedgerExceptionsError(
            f"{target}: não é um retrato do ledger ({FORMAT_NAME})"
        )
    if header.get("format_version") not in SUPPORTED_FORMAT_VERSIONS:
        raise LedgerExceptionsError(
            f"{target}: versão de formato {header.get('format_version')!r} não "
            f"suportada (esta versão lê {list(SUPPORTED_FORMAT_VERSIONS)}); "
            "atualize o quantilica-cli"
        )
    return header


def _parse_expiry(value: Any, where: str) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise LedgerExceptionsError(f"{where}: expira_em inválido")
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise LedgerExceptionsError(f"{where}: expira_em inválido: {value!r}") from exc
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


def read_ledger_exceptions(
    path: Path, *, now: datetime | None = None
) -> LedgerExceptions:
    """Caminhos de exceção vigentes do retrato (arquivo, ou pasta com o ``latest``).

    Confere contagem e sha256 da lista (o ``body_sha256`` do retrato não cobre o
    cabeçalho) e descarta as que já expiraram em ``now``. Retrato sem o campo devolve
    ``declared=False`` e nenhuma exceção.
    """
    target = resolve_snapshot(path)
    header = _read_header(target)
    generated = header.get("generated_at")
    generated_at = generated if isinstance(generated, str) else None
    if "lake_exceptions" not in header:
        return LedgerExceptions(target, generated_at, declared=False, paths=())

    records = header["lake_exceptions"]
    label = f"{target}: lake_exceptions"
    if not isinstance(records, list) or not all(isinstance(r, dict) for r in records):
        raise LedgerExceptionsError(f"{label} inválido")
    if header.get("lake_exceptions_count") != len(records):
        raise LedgerExceptionsError(
            f"{label}: a contagem do cabeçalho não confere com a lista"
        )
    digest = hashlib.sha256()
    for record in records:
        digest.update(_canonical(record))
    if digest.hexdigest() != str(header.get("lake_exceptions_sha256") or "").lower():
        raise LedgerExceptionsError(f"{label}: sha256 não confere; arquivo corrompido")

    current = (now or datetime.now(UTC)).astimezone(UTC)
    paths: list[str] = []
    expired = 0
    for number, record in enumerate(records, start=1):
        value = record.get("path")
        if not isinstance(value, str) or not value.strip():
            raise LedgerExceptionsError(f"{label}[{number}]: path ausente")
        expiry = _parse_expiry(record.get("expira_em"), f"{label}[{number}]")
        if expiry is not None and expiry <= current:
            expired += 1
            continue
        paths.append(value.strip())
    return LedgerExceptions(
        target, generated_at, declared=True, paths=tuple(paths), expired=expired
    )
