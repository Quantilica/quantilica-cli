"""Testes unitários para quantilica.cli.sdk (Onda 2: FetcherApp melhorias)."""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Annotated

import pytest
import typer
from typer.testing import CliRunner

from quantilica.cli.sdk import (
    DataRepository,
    FetcherApp,
    default_path_builder,
    make_resolve_groups,
)

runner = CliRunner()


def _grouped(app: typer.Typer, name: str = "fd") -> typer.Typer:
    """Emita o sub-app como um grupo nomeado.

    Um `Typer` com apenas um comando é executado de forma plana; agrupar
    garante uma invocação consistente nos testes.
    """
    root = typer.Typer()
    root.add_typer(app, name=name)
    return root


GROUPS = {"exp": {"name": "Exportações"}, "imp": {"name": "Importações"}}
ALIASES = {"trade": ["exp", "imp"], "empty": []}


def sample_list_datasets(group: str) -> list[dict]:
    return [
        {
            "group": group,
            "id": f"{group}-monthly",
            "ext": "csv",
            "year": 2023,
            "month": 5,
            "url": f"https://example.test/{group}-monthly.csv",
        }
    ]


def make_app(**overrides) -> FetcherApp:
    kwargs: dict = {
        "name": "comex-fetcher",
        "groups_dict": GROUPS,
        "aliases_dict": ALIASES,
        "list_datasets": sample_list_datasets,
    }
    kwargs.update(overrides)
    return FetcherApp(**kwargs)  # type: ignore[arg-type]


# ---------------------------------------------------------------
# default_path_builder / DataRepository
# ---------------------------------------------------------------


def test_default_path_builder_monthly_partition(tmp_path: Path):
    entry = {
        "group": "exp",
        "id": "exp-mun",
        "ext": "csv",
        "year": 2023,
        "month": 5,
    }
    path = default_path_builder(tmp_path, entry, dt.date(2024, 3, 15))
    assert path == tmp_path / "exp" / "exp-mun_2023-05@20240315.csv"


def test_default_path_builder_semester_partition(tmp_path: Path):
    entry = {
        "group": "exp",
        "id": "exp-sem",
        "ext": "csv",
        "year": 2023,
        "semester": 2,
    }
    path = default_path_builder(tmp_path, entry, dt.date(2024, 3, 15))
    assert path == tmp_path / "exp" / "exp-sem_2023-02@20240315.csv"


def test_default_path_builder_year_only(tmp_path: Path):
    entry = {"group": "exp", "id": "exp", "ext": "csv", "year": 2023}
    path = default_path_builder(tmp_path, entry, dt.date(2024, 3, 15))
    assert path == tmp_path / "exp" / "exp_2023@20240315.csv"


def test_default_path_builder_no_stamp_without_date(tmp_path: Path):
    entry = {"group": "exp", "id": "exp", "ext": "csv", "year": 2023}
    path = default_path_builder(tmp_path, entry, None)
    assert path == tmp_path / "exp" / "exp_2023.csv"


def test_default_path_builder_derives_ext_from_url(tmp_path: Path):
    entry = {"group": "exp", "id": "exp", "url": "https://x.test/file.xlsx?a=b"}
    path = default_path_builder(tmp_path, entry, None)
    assert path.suffix == ".xlsx"


def test_default_path_builder_ext_fallback(tmp_path: Path):
    entry = {"group": "exp", "id": "exp", "url": "https://x.test/download"}
    path = default_path_builder(tmp_path, entry, None)
    assert path.suffix == ".bin"


def test_default_path_builder_no_group_uses_id(tmp_path: Path):
    entry = {"id": "lonely", "ext": "csv"}
    path = default_path_builder(tmp_path, entry, None)
    assert path == tmp_path / "lonely" / "lonely.csv"


def test_data_repository_is_stamped(tmp_path: Path):
    repo = DataRepository(tmp_path)
    entry = {"group": "imp", "id": "imp", "ext": "csv", "year": 2024}
    path = repo.path_for_entry(entry, last_modified=dt.date(2025, 1, 2))
    assert path == tmp_path / "imp" / "imp_2024@20250102.csv"


# ---------------------------------------------------------------
# FetcherApp: path_builder opcional
# ---------------------------------------------------------------


def test_fetcher_app_defaults_to_default_path_builder():
    app = make_app()
    assert app.path_builder is default_path_builder


def test_fetcher_app_accepts_custom_path_builder(tmp_path: Path):
    def custom(output_dir, entry, last_modified):
        return output_dir / "custom.csv"

    app = make_app(path_builder=custom)
    assert app.path_builder is custom


def test_fetcher_app_default_output():
    app = make_app()
    assert app.default_output == Path("/data/comex")


def test_fetcher_app_default_output_from_name(tmp_path: Path):
    app = make_app(name="foo-fetcher")
    assert app.default_output == Path("/data/foo")


# ---------------------------------------------------------------
# FetcherApp: attach_command e build_default_commands
# ---------------------------------------------------------------


def test_attach_command_inherits_default_commands():
    def ping() -> None:
        return None

    app = make_app()
    app.attach_command(ping)
    result = runner.invoke(app.app, ["ping"])
    assert result.exit_code == 0, result.output
    names = [cmd.name for cmd in app.app.registered_commands]
    assert {"sync", "list"} <= set(names)


def test_attach_command_with_custom_name_and_kwargs(tmp_path: Path):
    def convert(
        input: Annotated[Path, typer.Argument(help="Arquivo de entrada")],
    ) -> None:
        typer.echo(f"converted {input.name}")

    app = make_app(build_default_commands=False)
    app.attach_command(convert, name="conv", no_args_is_help=True)
    names = [cmd.name for cmd in app.app.registered_commands]
    assert names == ["conv"]

    # run the attached command through the CliRunner
    dummy = tmp_path / "in.csv"
    dummy.write_text("x,y\n1,2\n")
    assert runner.invoke(_grouped(app.app), ["fd", "conv", str(dummy)]).exit_code == 0


def test_build_default_commands_false_removes_sync_and_list():
    app = make_app(build_default_commands=False)
    names = [cmd.name for cmd in app.app.registered_commands]
    assert "sync" not in names
    assert "list" not in names


def test_attach_command_invocation_output(tmp_path: Path):
    fetcher = make_app(build_default_commands=False)

    def hello(
        verbose: Annotated[
            bool, typer.Option("--verbose", help="Logs detalhados")
        ] = False,
    ) -> None:
        if verbose:
            typer.echo("verbose")
        typer.echo("hello")

    fetcher.attach_command(hello)
    result = runner.invoke(_grouped(fetcher.app), ["fd", "hello", "--verbose"])
    assert result.exit_code == 0, result.output
    assert "verbose" in result.output


# ---------------------------------------------------------------
# make_resolve_groups
# ---------------------------------------------------------------


def test_make_resolve_groups_expands_aliases():
    resolve = make_resolve_groups(GROUPS, ALIASES)
    assert resolve(["trade"]) == ["exp", "imp"]


def test_make_resolve_groups_dedups_and_preserves_order():
    resolve = make_resolve_groups(GROUPS, ALIASES)
    assert resolve(["imp", "trade", "exp"]) == ["imp", "exp"]


def test_make_resolve_groups_none_returns_all_groups():
    resolve = make_resolve_groups(GROUPS, ALIASES)
    assert resolve(None) == ["exp", "imp"]


def test_make_resolve_groups_unknown_raises():
    resolve = make_resolve_groups(GROUPS, ALIASES)
    with pytest.raises(ValueError, match="Grupo desconhecido: 'zzz'"):
        resolve(["zzz"])


def test_make_resolve_groups_alias_to_empty_raises():
    resolve = make_resolve_groups(GROUPS, ALIASES)
    with pytest.raises(ValueError, match="Grupo desconhecido: 'empty'"):
        resolve(["empty"])


def test_resolver_shared_between_instances():
    app_a = make_app()
    app_b = make_app()
    assert app_a.resolve_groups(["trade"]) == ["exp", "imp"]
    assert app_b.resolve_groups(["exp"]) == ["exp"]


# ---------------------------------------------------------------
# Integração: sync command com resolver (dry-run)
# ---------------------------------------------------------------


def test_builtin_sync_dry_run_with_alias():
    app = make_app()
    result = runner.invoke(app.app, ["sync", "trade", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "2 arquivo(s) listado(s)" in result.output


def test_builtin_sync_unknown_group_exits_1():
    app = make_app()
    result = runner.invoke(app.app, ["sync", "zzz", "--dry-run"])
    assert result.exit_code == 1
    assert "Grupo desconhecido: 'zzz'" in result.output


def test_builtin_list():
    app = make_app()
    result = runner.invoke(app.app, ["list"])
    assert result.exit_code == 0, result.output
    assert "2 dataset(s) no catálogo." in result.output
