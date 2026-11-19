"""Leitura das exceções da auditoria no retrato do ledger."""

from __future__ import annotations

import gzip
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from quantilica.cli.ledger_exceptions import (
    LedgerExceptionsError,
    read_ledger_exceptions,
)
from tests.ledger_snapshots import exception_record, write_ledger_snapshot

NOW = datetime(2026, 11, 19, 15, 0, tzinfo=UTC)


def test_reads_the_exceptions_of_a_file(tmp_path: Path) -> None:
    snapshot = write_ledger_snapshot(
        tmp_path / "ledger-vigente-20261119T120000Z.jsonl.gz",
        [exception_record("a/1.csv"), exception_record("b/*.zip")],
    )
    result = read_ledger_exceptions(snapshot, now=NOW)
    assert result.declared is True
    assert result.paths == ("a/1.csv", "b/*.zip")
    assert result.expired == 0
    assert result.generated_at == "2026-11-19T12:00:00Z"


def test_a_folder_means_its_latest_snapshot(tmp_path: Path) -> None:
    write_ledger_snapshot(
        tmp_path / "ledger-vigente-latest.jsonl.gz", [exception_record("a/1.csv")]
    )
    assert read_ledger_exceptions(tmp_path, now=NOW).paths == ("a/1.csv",)


def test_folder_without_latest_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(LedgerExceptionsError, match="ausente"):
        read_ledger_exceptions(tmp_path)


def test_expired_exceptions_are_dropped_at_read_time(tmp_path: Path) -> None:
    snapshot = write_ledger_snapshot(
        tmp_path / "r.jsonl.gz",
        [
            exception_record("a/vence.csv", expira_em="2026-11-19T14:00:00Z"),
            exception_record("a/vigente.csv", expira_em="2026-12-01T00:00:00Z"),
            exception_record("a/sem-prazo.csv"),
        ],
    )
    result = read_ledger_exceptions(snapshot, now=NOW)
    assert result.paths == ("a/vigente.csv", "a/sem-prazo.csv")
    assert result.expired == 1


def test_snapshot_without_the_field_is_declared_false(tmp_path: Path) -> None:
    snapshot = write_ledger_snapshot(tmp_path / "antigo.jsonl.gz", None)
    result = read_ledger_exceptions(snapshot, now=NOW)
    assert result.declared is False and result.paths == ()


def test_empty_list_is_declared_and_empty(tmp_path: Path) -> None:
    snapshot = write_ledger_snapshot(tmp_path / "vazio.jsonl.gz", [])
    result = read_ledger_exceptions(snapshot, now=NOW)
    assert result.declared is True and result.paths == ()


def _rewrite(snapshot: Path, change) -> Path:
    with gzip.open(snapshot, "rb") as stream:
        lines = stream.read().splitlines(keepends=True)
    header = json.loads(lines[0])
    change(header)
    out = snapshot.with_name("mexido.jsonl.gz")
    with gzip.open(out, "wb") as stream:
        stream.write(json.dumps(header).encode() + b"\n" + b"".join(lines[1:]))
    return out


def test_tampered_list_or_count_is_refused(tmp_path: Path) -> None:
    snapshot = write_ledger_snapshot(
        tmp_path / "r.jsonl.gz", [exception_record("a/1.csv")]
    )

    def change_path(header: dict) -> None:
        header["lake_exceptions"][0]["path"] = "*"

    def change_count(header: dict) -> None:
        header["lake_exceptions_count"] = 2

    for change in (change_path, change_count):
        with pytest.raises(LedgerExceptionsError, match="lake_exceptions"):
            read_ledger_exceptions(_rewrite(snapshot, change), now=NOW)


def test_wrong_format_or_version_is_refused(tmp_path: Path) -> None:
    good = write_ledger_snapshot(tmp_path / "r.jsonl.gz", [])
    with pytest.raises(LedgerExceptionsError, match="não é um retrato"):
        read_ledger_exceptions(
            _rewrite(good, lambda h: h.update(format="outro")), now=NOW
        )
    with pytest.raises(LedgerExceptionsError, match="versão de formato"):
        read_ledger_exceptions(
            _rewrite(good, lambda h: h.update(format_version=3)), now=NOW
        )


def test_corrupted_gzip_is_refused(tmp_path: Path) -> None:
    broken = tmp_path / "quebrado.jsonl.gz"
    broken.write_bytes(b"isto nao e gzip")
    with pytest.raises(LedgerExceptionsError, match="gzip"):
        read_ledger_exceptions(broken)
