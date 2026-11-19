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
GEN = "2026-11-19T12:00:00Z"  # 3 h antes de NOW


def _write(destination, exceptions, **kw):
    return write_ledger_snapshot(destination, exceptions, generated_at=GEN, **kw)


def test_reads_the_exceptions_of_a_file(tmp_path: Path) -> None:
    snapshot = _write(
        tmp_path / "ledger-vigente-20261119T120000Z.jsonl.gz",
        [exception_record("a/1.csv"), exception_record("b/*.zip")],
    )
    result = read_ledger_exceptions(snapshot, now=NOW)
    assert result.declared is True
    assert result.paths == ("a/1.csv", "b/*.zip")
    assert result.expired == 0
    assert result.generated_at == "2026-11-19T12:00:00Z"


def test_a_folder_means_its_latest_snapshot(tmp_path: Path) -> None:
    _write(tmp_path / "ledger-vigente-latest.jsonl.gz", [exception_record("a/1.csv")])
    assert read_ledger_exceptions(tmp_path, now=NOW).paths == ("a/1.csv",)


def test_folder_without_latest_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(LedgerExceptionsError, match="ausente"):
        read_ledger_exceptions(tmp_path)


def test_expired_exceptions_are_dropped_at_read_time(tmp_path: Path) -> None:
    snapshot = _write(
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
    snapshot = _write(tmp_path / "antigo.jsonl.gz", None)
    result = read_ledger_exceptions(snapshot, now=NOW)
    assert result.declared is False and result.paths == ()


def test_empty_list_is_declared_and_empty(tmp_path: Path) -> None:
    snapshot = _write(tmp_path / "vazio.jsonl.gz", [])
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
    snapshot = _write(tmp_path / "r.jsonl.gz", [exception_record("a/1.csv")])

    def change_path(header: dict) -> None:
        header["lake_exceptions"][0]["path"] = "*"

    def change_count(header: dict) -> None:
        header["lake_exceptions_count"] = 2

    for change in (change_path, change_count):
        with pytest.raises(LedgerExceptionsError, match="lake_exceptions"):
            read_ledger_exceptions(_rewrite(snapshot, change), now=NOW)


def test_wrong_format_or_version_is_refused(tmp_path: Path) -> None:
    good = _write(tmp_path / "r.jsonl.gz", [])
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


def test_old_snapshot_is_refused_with_a_clear_message(tmp_path: Path) -> None:
    snapshot = write_ledger_snapshot(
        tmp_path / "velho.jsonl.gz",
        [exception_record("a/1.csv")],
        generated_at="2026-11-17T12:00:00Z",  # 51 h antes de NOW
    )
    with pytest.raises(LedgerExceptionsError, match="velho demais: 51.0 h"):
        read_ledger_exceptions(snapshot, now=NOW)
    # limite maior ou desligado aceita
    assert read_ledger_exceptions(snapshot, now=NOW, max_age_hours=100).paths
    assert read_ledger_exceptions(snapshot, now=NOW, max_age_hours=None).paths


def test_fresh_snapshot_passes_the_default_limit(tmp_path: Path) -> None:
    snapshot = _write(tmp_path / "novo.jsonl.gz", [exception_record("a/1.csv")])
    assert read_ledger_exceptions(snapshot, now=NOW).paths == ("a/1.csv",)


def test_snapshot_from_the_future_is_refused(tmp_path: Path) -> None:
    snapshot = write_ledger_snapshot(
        tmp_path / "futuro.jsonl.gz", [], generated_at="2026-11-20T12:00:00Z"
    )
    with pytest.raises(LedgerExceptionsError, match="no futuro"):
        read_ledger_exceptions(snapshot, now=NOW)


def test_missing_generated_at_is_refused(tmp_path: Path) -> None:
    good = _write(tmp_path / "r.jsonl.gz", [])
    with pytest.raises(LedgerExceptionsError, match="generated_at"):
        read_ledger_exceptions(_rewrite(good, lambda h: h.pop("generated_at")), now=NOW)


def test_non_positive_max_age_is_a_usage_error(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        read_ledger_exceptions(tmp_path, max_age_hours=0)


@pytest.mark.parametrize(
    "bad", ["/abs/x.csv", "../fora.csv", "a/../../x.csv", "C:/x.csv", "", "  "]
)
def test_unsafe_paths_are_refused(tmp_path: Path, bad: str) -> None:
    snapshot = _write(tmp_path / "r.jsonl.gz", [exception_record(bad)])
    with pytest.raises(LedgerExceptionsError, match="caminho inválido"):
        read_ledger_exceptions(snapshot, now=NOW)


def test_duplicate_paths_are_refused(tmp_path: Path) -> None:
    snapshot = _write(
        tmp_path / "r.jsonl.gz",
        [exception_record("a/1.csv"), exception_record("a\\1.csv")],
    )
    with pytest.raises(LedgerExceptionsError, match="repetido"):
        read_ledger_exceptions(snapshot, now=NOW)


def test_patterns_and_backslashes_are_kept_safe(tmp_path: Path) -> None:
    snapshot = _write(tmp_path / "r.jsonl.gz", [exception_record("a\\b/*.zip")])
    assert read_ledger_exceptions(snapshot, now=NOW).paths == ("a/b/*.zip",)


def test_summary_describes_the_origin(tmp_path: Path) -> None:
    snapshot = _write(
        tmp_path / "r.jsonl.gz",
        [
            exception_record("a/1.csv"),
            exception_record("a/2.csv", expira_em="2020-01-01T00:00:00Z"),
        ],
    )
    assert read_ledger_exceptions(snapshot, now=NOW).summary() == {
        "retrato": str(snapshot),
        "generated_at": GEN,
        "declaradas": True,
        "vigentes": 1,
        "expiradas": 1,
    }
