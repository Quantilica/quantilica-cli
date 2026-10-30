"""Testes dos comandos `quantilica lake` (select, build, audit)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from quantilica.cli.cli import app

runner = CliRunner()


def _need(module: str) -> None:
    pytest.importorskip(module)


# --- ajuda: funciona sem o analytics -------------------------------------------


def test_lake_help_lists_the_three_commands() -> None:
    result = runner.invoke(app, ["lake", "--help"])
    assert result.exit_code == 0
    for name in ("select", "build", "audit"):
        assert name in result.output


@pytest.mark.parametrize(
    ("command", "options"),
    [
        ("select", ["--raw", "--out", "--mode", "--exclude"]),
        (
            "build",
            [
                "--input",
                "--lake",
                "--sources",
                "--workers",
                "--memory",
                "--threads",
                "--scratch",
                "--force",
                "--retry-errors",
                "--no-disk-check",
                "--allow-sleep",
            ],
        ),
        (
            "audit",
            [
                "--lake",
                "--input",
                "--ledger",
                "--sources",
                "--no-readers",
                "--no-integrity",
                "--exceptions",
                "--rehash",
                "--workers",
                "--json",
                "--json-file",
            ],
        ),
    ],
)
def test_command_help_has_every_option(command: str, options: list[str]) -> None:
    result = runner.invoke(app, ["lake", command, "--help"], terminal_width=200)
    assert result.exit_code == 0
    for option in options:
        assert option in result.output


# --- sem o analytics: mensagem clara e saída != 0 --------------------------------


@pytest.fixture
def no_analytics(monkeypatch: pytest.MonkeyPatch) -> None:
    """Faz `import quantilica.analytics...` falhar, como num ambiente sem ele."""
    for name in list(sys.modules):
        if name.startswith("quantilica.analytics"):
            monkeypatch.delitem(sys.modules, name)
    monkeypatch.setitem(sys.modules, "quantilica.analytics", None)
    monkeypatch.setitem(sys.modules, "quantilica.analytics.lake_build", None)
    monkeypatch.setitem(sys.modules, "quantilica.analytics.lake_audit", None)


@pytest.mark.parametrize("command", ["build", "audit"])
def test_without_analytics_says_what_to_install(
    command: str, no_analytics: None, tmp_path: Path
) -> None:
    args = ["lake", command]
    if command == "build":
        args += ["--input", str(tmp_path), "--lake", str(tmp_path / "lake")]
    else:
        args += ["--lake", str(tmp_path)]
    result = runner.invoke(app, args)
    assert result.exit_code != 0
    assert "quantilica-analytics" in result.output
    assert "quantilica-cli[lake]" in result.output


# --- select ------------------------------------------------------------------------


@pytest.fixture
def corpus(tmp_path: Path):
    _need("quantilica.analytics.lake")
    core_lake = pytest.importorskip("quantilica.core.lake")
    if not hasattr(core_lake, "materialize_view"):
        pytest.skip("quantilica-core sem materialize_view")
    from tests.lake_fixtures import build_corpus

    return build_corpus(tmp_path)


def test_select_creates_the_view(tmp_path: Path, corpus) -> None:
    out = tmp_path / "out"
    result = runner.invoke(
        app, ["lake", "select", "--raw", str(corpus.raw), "--out", str(out)]
    )
    assert result.exit_code == 0, result.output
    assert (out / "alfa" / "vendas" / "vendas_2025@20261021.csv").exists()
    assert not (out / "alfa" / "vendas" / "vendas_2025@20260101.csv").exists()
    assert "arquivos:" in result.output


def test_select_exclude_file(tmp_path: Path, corpus) -> None:
    exclude = tmp_path / "exclude.txt"
    exclude.write_text(
        "# corrompido na fonte\n\nbeta/precos/precos_202501@20261021.csv\n",
        encoding="utf-8",
    )
    out = tmp_path / "out"
    result = runner.invoke(
        app,
        [
            "lake",
            "select",
            "--raw",
            str(corpus.raw),
            "--out",
            str(out),
            "--exclude",
            str(exclude),
            "--mode",
            "copy",
        ],
    )
    assert result.exit_code == 0, result.output
    assert not (out / "beta" / "precos" / "precos_202501@20261021.csv").exists()
    assert (out / "alfa" / "vendas" / "vendas_2026@20261021.csv").exists()


def test_select_rejects_out_inside_raw(tmp_path: Path, corpus) -> None:
    result = runner.invoke(
        app,
        ["lake", "select", "--raw", str(corpus.raw), "--out", str(corpus.raw / "x")],
    )
    assert result.exit_code == 1
    assert "Erro" in result.output


def test_select_rejects_unknown_mode(tmp_path: Path, corpus) -> None:
    result = runner.invoke(
        app,
        [
            "lake",
            "select",
            "--raw",
            str(corpus.raw),
            "--out",
            str(tmp_path / "o"),
            "--mode",
            "teleport",
        ],
    )
    assert result.exit_code == 2


# --- audit -------------------------------------------------------------------------


def test_audit_clean_lake_text(corpus) -> None:
    result = runner.invoke(
        app, ["lake", "audit", "--lake", str(corpus.lake), "--input", str(corpus.view)]
    )
    assert result.exit_code == 0, result.output
    assert "APROVADO" in result.output


def test_audit_clean_lake_json_stdout_is_pure_json(corpus) -> None:
    result = runner.invoke(
        app,
        [
            "lake",
            "audit",
            "--lake",
            str(corpus.lake),
            "--input",
            str(corpus.view),
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["ok"] is True


def test_audit_defect_fails_in_text_mode(corpus) -> None:
    corpus.lake_parquets()[0].unlink()
    result = runner.invoke(
        app, ["lake", "audit", "--lake", str(corpus.lake), "--input", str(corpus.view)]
    )
    assert result.exit_code == 1
    assert "REPROVADO" in result.output


def test_audit_defect_fails_in_json_modes(corpus, tmp_path: Path) -> None:
    path = corpus.lake_parquets()[0]
    path.write_bytes(path.read_bytes()[:40])  # rodapé perdido
    args = ["lake", "audit", "--lake", str(corpus.lake), "--input", str(corpus.view)]

    result = runner.invoke(app, [*args, "--json"])
    assert result.exit_code == 1
    assert json.loads(result.stdout)["ok"] is False

    target = tmp_path / "relatorio.json"
    result = runner.invoke(app, [*args, "--json-file", str(target)])
    assert result.exit_code == 1
    assert "REPROVADO" in result.stdout
    assert json.loads(target.read_text(encoding="utf-8"))["ok"] is False


def test_audit_exceptions_file_silences_known_missing_parquet(corpus, tmp_path) -> None:
    corpus.lake_parquets()[-1].unlink()
    exceptions = tmp_path / "excecoes.txt"
    exceptions.write_text("# sem Parquet\n*\n", encoding="utf-8")
    result = runner.invoke(
        app,
        [
            "lake",
            "audit",
            "--lake",
            str(corpus.lake),
            "--input",
            str(corpus.view),
            "--exceptions",
            str(exceptions),
        ],
    )
    assert result.exit_code == 0, result.output


def test_audit_missing_lake_is_a_usage_error(tmp_path: Path) -> None:
    result = runner.invoke(app, ["lake", "audit", "--lake", str(tmp_path / "nada")])
    assert result.exit_code == 2


def test_audit_options_reach_the_engine(
    corpus, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = sys.modules["quantilica.analytics.lake_audit"]

    seen: dict = {}
    real = engine.lake_audit

    def spy(lake_dir, **kwargs):
        seen.update(kwargs)
        return real(lake_dir, **kwargs)

    monkeypatch.setattr(engine, "lake_audit", spy)
    ledger = tmp_path / "ledger.csv"
    ledger.write_text("path,sha256\n", encoding="utf-8")
    runner.invoke(
        app,
        [
            "lake",
            "audit",
            "--lake",
            str(corpus.lake),
            "--sources",
            "alfa, beta",
            "--no-readers",
            "--no-integrity",
            "--rehash",
            "--workers",
            "3",
            "--ledger",
            str(ledger),
        ],
    )
    assert seen["sources"] == ["alfa", "beta"]
    assert seen["readers"] is False
    assert seen["integrity"] is False
    assert seen["rehash"] is True
    assert seen["workers"] == 3
    assert seen["ledger"] == ledger


# --- build -------------------------------------------------------------------------


@pytest.fixture
def fake_engine(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Faz o `lake_build` real usar conversores falsos (o CLI não os injeta)."""
    _need("quantilica.analytics.lake_build")
    import quantilica.analytics.lake_build as engine

    from tests.lake_build_fakes import FakeConverter

    real = engine.lake_build
    state: dict = {"behaviors": {}}

    def with_fakes(input_dir, lake_dir, **kwargs):
        control = tmp_path / "control"
        converters = {
            "alfa": FakeConverter("alfa", ["vendas"], control, state["behaviors"]),
            "beta": FakeConverter("beta", ["precos"], control),
        }
        return real(input_dir, lake_dir, converters=converters, **kwargs)

    monkeypatch.setattr(engine, "lake_build", with_fakes)
    return state


def _build_args(corpus, tmp_path: Path, *extra: str) -> list[str]:
    return [
        "lake",
        "build",
        "--input",
        str(corpus.view),
        "--lake",
        str(tmp_path / "lake-new"),
        "--workers",
        "2",
        "--no-disk-check",
        "--allow-sleep",
        *extra,
    ]


def test_build_ok(corpus, tmp_path: Path, fake_engine) -> None:
    result = runner.invoke(app, _build_args(corpus, tmp_path))
    assert result.exit_code == 0, result.output
    assert "alfa: converted=1" in result.stdout
    assert "result: ok" in result.stdout
    assert (tmp_path / "lake-new" / "alfa" / "vendas" / "part.txt").exists()


def test_build_resumes_without_redoing(corpus, tmp_path: Path, fake_engine) -> None:
    assert runner.invoke(app, _build_args(corpus, tmp_path)).exit_code == 0
    result = runner.invoke(app, _build_args(corpus, tmp_path))
    assert result.exit_code == 0, result.output
    assert "already_done=1" in result.stdout


def test_build_failing_task_exits_nonzero(corpus, tmp_path: Path, fake_engine) -> None:
    fake_engine["behaviors"]["vendas"] = "raise"
    result = runner.invoke(app, _build_args(corpus, tmp_path))
    assert result.exit_code == 1
    assert "failed: alfa/vendas" in result.stdout
    assert "result: ERRORS" in result.stdout


def test_build_unknown_source_is_a_clear_error(
    corpus, tmp_path: Path, fake_engine
) -> None:
    result = runner.invoke(app, _build_args(corpus, tmp_path, "--sources", "zeta"))
    assert result.exit_code == 1
    assert "Erro" in result.output


def test_build_empty_sources_is_a_usage_error(corpus, tmp_path: Path) -> None:
    _need("quantilica.analytics.lake_build")
    result = runner.invoke(app, _build_args(corpus, tmp_path, "--sources", " , "))
    assert result.exit_code == 2


def test_build_options_reach_the_engine(
    corpus, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _need("quantilica.analytics.lake_build")
    import quantilica.analytics.lake_build as engine

    seen: dict = {}

    def spy(input_dir, lake_dir, **kwargs):
        seen.update(kwargs, input_dir=input_dir, lake_dir=lake_dir)
        return engine.BuildSummary(run_id="r", state_file=tmp_path / "r.jsonl")

    monkeypatch.setattr(engine, "lake_build", spy)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    result = runner.invoke(
        app,
        [
            "lake",
            "build",
            "-i",
            str(corpus.view),
            "--lake",
            str(tmp_path / "l"),
            "--sources",
            "alfa,beta",
            "--workers",
            "3",
            "--memory",
            "12GB",
            "--threads",
            "2",
            "--scratch",
            str(scratch),
            "--force",
            "--retry-errors",
            "--no-disk-check",
            "--allow-sleep",
        ],
    )
    assert result.exit_code == 0, result.output
    assert seen["sources"] == ["alfa", "beta"]
    assert (seen["workers"], seen["memory"], seen["threads"]) == (3, "12GB", 2)
    assert seen["scratch"] == scratch
    assert seen["force"] is True
    assert seen["retry_errors"] is True
    assert seen["check_disk"] is False
    assert seen["prevent_sleep"] is False


@pytest.mark.parametrize(
    ("error", "needle"),
    [("disk", "espaço em disco"), ("build", "boom"), ("interrupt", "retomar")],
)
def test_build_engine_errors_become_messages(
    corpus, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: str, needle: str
) -> None:
    _need("quantilica.analytics.lake_build")
    import quantilica.analytics.lake_build as engine

    def boom(input_dir, lake_dir, **kwargs):
        if error == "disk":
            raise engine.InsufficientDiskError("faltam 10 GB")
        if error == "build":
            raise engine.LakeBuildError("boom")
        raise KeyboardInterrupt

    monkeypatch.setattr(engine, "lake_build", boom)
    result = runner.invoke(app, _build_args(corpus, tmp_path))
    assert result.exit_code == (130 if error == "interrupt" else 1)
    assert needle in result.output


def test_build_progress_goes_to_stderr_not_stdout(
    corpus, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _need("quantilica.analytics.lake_build")
    import logging

    import quantilica.analytics.lake_build as engine

    def chatty(input_dir, lake_dir, **kwargs):
        logging.getLogger("quantilica.analytics.lake_build").info("andamento-xyz")
        return engine.BuildSummary(run_id="r", state_file=tmp_path / "r.jsonl")

    monkeypatch.setattr(engine, "lake_build", chatty)
    result = runner.invoke(app, _build_args(corpus, tmp_path))
    assert result.exit_code == 0, result.output
    assert "andamento-xyz" in result.stderr
    assert "andamento-xyz" not in result.stdout
    assert logging.getLogger("quantilica").propagate is True
