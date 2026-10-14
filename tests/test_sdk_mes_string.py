"""Regressão: catálogos cujo ``month`` é string (grupo de regex) não quebram a CLI."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import typer
from typer.testing import CliRunner

from quantilica.cli.sdk import FetcherApp, _partition_label, default_path_builder

runner = CliRunner()


def _entry(month: int | str) -> dict:
    return {
        "group": "caged",
        "id": "caged-mov",
        "ext": "7z",
        "year": 2023,
        "month": month,
        "url": "https://example.test/caged-mov.7z",
    }


def test_partition_label_aceita_mes_string_e_int() -> None:
    assert _partition_label(_entry("03")) == "2023-03"
    assert _partition_label(_entry(3)) == "2023-03"
    assert _partition_label(_entry("12")) == "2023-12"


def test_default_path_builder_aceita_mes_string(tmp_path: Path) -> None:
    path = default_path_builder(tmp_path, _entry("03"), dt.date(2024, 3, 15))
    assert path == tmp_path / "caged" / "caged-mov_2023-03@20240315.7z"


def test_list_aceita_mes_string() -> None:
    """`list` terminava com ValueError ao formatar ``"03"`` com ``:02d``."""
    fetcher = FetcherApp(
        name="pdet-fetcher",
        help="teste",
        groups_dict={"caged": {"name": "CAGED"}},
        aliases_dict={},
        list_datasets=lambda group: [_entry("03")],
    )
    root = typer.Typer()
    root.add_typer(fetcher.app, name="fd")
    result = runner.invoke(root, ["fd", "list"])
    assert result.exit_code == 0, result.output
    assert "2023-03" in result.output
