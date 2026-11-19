"""Retrato do ledger sintético para os testes das exceções da auditoria.

Escreve o formato (versão 2) à mão, sem importar o pacote que o gera: o cabeçalho
com ``body_sha256`` e contagens e, nele, a lista ``lake_exceptions`` com a contagem
e o sha256 próprios.
"""

from __future__ import annotations

import gzip
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


def _dumps(record: dict[str, Any]) -> bytes:
    text = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return text.encode("utf-8") + b"\n"


def exception_record(path: str, **extra: Any) -> dict[str, Any]:
    return {
        "path": path,
        "source_id": path.split("/", 1)[0],
        "motivo": "vazio na fonte",
        "criado_por": "dk",
        "criado_em": "2026-11-19T12:00:00Z",
        "expira_em": None,
    } | extra


def hours_ago(hours: float) -> str:
    moment = datetime.now(UTC) - timedelta(hours=hours)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def write_ledger_snapshot(
    destination: Path,
    exceptions: list[dict[str, Any]] | None,
    *,
    generated_at: str | None = None,
    header_extra: dict[str, Any] | None = None,
) -> Path:
    """Retrato só com o cabeçalho (o leitor das exceções não lê o corpo).

    ``exceptions=None`` escreve um retrato sem o campo (exportador anterior);
    ``generated_at`` padrão é uma hora atrás.
    """
    header: dict[str, Any] = {
        "kind": "header",
        "format": "quantilica-ingest-ledger",
        "format_version": 2,
        "generated_at": generated_at or hours_ago(1),
        "versions": 0,
        "exclusions": 0,
        "source_counts": {},
        "body_sha256": hashlib.sha256(b"").hexdigest(),
    }
    if exceptions is not None:
        digest = hashlib.sha256()
        for record in exceptions:
            digest.update(_dumps(record))
        header |= {
            "lake_exceptions": exceptions,
            "lake_exceptions_count": len(exceptions),
            "lake_exceptions_sha256": digest.hexdigest(),
        }
    header |= header_extra or {}
    destination.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(destination, "wb") as stream:
        stream.write(_dumps(header))
    return destination
