"""Unit tests for FetcherApp.command_backfill."""

import json
from pathlib import Path

from typer.testing import CliRunner

from quantilica.cli.sdk import FetcherApp


def _make_app() -> FetcherApp:
    return FetcherApp(
        "demo",
        groups_dict={"g": "Group G"},
        aliases_dict={},
        list_datasets=lambda g: [],
    )


def test_command_backfill_success_and_resume(tmp_path: Path):
    app = _make_app()
    executed = []

    def get_units():
        return ["u1", "u2", "u3"]

    def run_unit(unit: str):
        executed.append(unit)

    app.command_backfill(get_units, run_unit, name="backfill")

    runner = CliRunner()
    result = runner.invoke(app.app, ["backfill", "-o", str(tmp_path)])
    assert result.exit_code == 0
    assert executed == ["u1", "u2", "u3"]

    # Checkpoint exists
    cp = tmp_path / ".backfill" / "backfill.json"
    assert cp.is_file()
    data = json.loads(cp.read_text(encoding="utf-8"))
    assert data["completed"] == ["u1", "u2", "u3"]
    assert not data["failed"]

    # Second run with --resume should execute 0 remaining units
    executed.clear()
    result2 = runner.invoke(app.app, ["backfill", "-o", str(tmp_path)])
    assert result2.exit_code == 0
    assert executed == []

    # Third run with --restart should execute all 3 units again
    executed.clear()
    result3 = runner.invoke(app.app, ["backfill", "-o", str(tmp_path), "--restart"])
    assert result3.exit_code == 0
    assert executed == ["u1", "u2", "u3"]


def test_command_backfill_failure(tmp_path: Path):
    app = _make_app()

    def get_units():
        return ["good", "bad"]

    def run_unit(unit: str):
        if unit == "bad":
            raise ValueError("simulated network error")

    app.command_backfill(get_units, run_unit, name="backfill")

    runner = CliRunner()
    result = runner.invoke(app.app, ["backfill", "-o", str(tmp_path)])
    assert result.exit_code == 1

    cp = tmp_path / ".backfill" / "backfill.json"
    assert cp.is_file()
    data = json.loads(cp.read_text(encoding="utf-8"))
    assert data["completed"] == ["good"]
    assert "bad" in data["failed"]
