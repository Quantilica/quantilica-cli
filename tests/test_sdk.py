"""Testes unitários para quantilica.cli.sdk (Onda 2: FetcherApp melhorias)."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Annotated

import httpx2
import pytest
import typer
from quantilica.core.http import HttpClient
from typer.testing import CliRunner

from quantilica.cli.sdk import (
    CheckPlan,
    DataRepository,
    FetcherApp,
    default_client,
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


# ---------------------------------------------------------------
# check (verificação remoto × local, sem download) + sync --from-plan
# ---------------------------------------------------------------


def _head_handler(payload: bytes = b"0123456789"):
    def handler(request):
        if request.method == "HEAD":
            return httpx2.Response(
                200,
                headers={
                    "Content-Length": str(len(payload)),
                    "Last-Modified": "Mon, 01 Jan 2024 00:00:00 GMT",
                    "ETag": '"abc123"',
                },
            )
        return httpx2.Response(200, content=payload)

    return handler


def _mock_client(payload: bytes = b"0123456789") -> HttpClient:
    return HttpClient(
        attempts=1, transport=httpx2.MockTransport(_head_handler(payload))
    )


def test_check_entry_missing_downloads(tmp_path: Path):
    app = make_app(client=_mock_client())
    entry = sample_list_datasets("exp")[0]
    item = app.check_entry(entry, tmp_path)
    assert item.action == "download"
    assert item.reason == "not-present"
    assert item.remote_etag == '"abc123"'
    assert item.remote_size == 10
    assert item.remote_last_modified == "Mon, 01 Jan 2024 00:00:00 GMT"
    assert item.local_exists is False


def test_check_entry_fresh_skips(tmp_path: Path):
    from quantilica.cli.sdk import default_path_builder

    app = make_app(client=_mock_client())
    entry = sample_list_datasets("exp")[0]
    local = default_path_builder(tmp_path, entry, dt.date(2024, 1, 1))
    local.parent.mkdir(parents=True, exist_ok=True)
    local.write_bytes(b"0123456789")
    item = app.check_entry(entry, tmp_path)
    assert item.action == "skip-up-to-date"
    assert item.reason == "local-fresh"
    assert item.local_exists is True


def test_check_entry_head_error_downloads(tmp_path: Path):
    def handler(request):
        raise httpx2.ConnectError("down")

    app = make_app(
        client=HttpClient(attempts=1, transport=httpx2.MockTransport(handler))
    )
    entry = sample_list_datasets("exp")[0]
    item = app.check_entry(entry, tmp_path)
    assert item.action == "download"
    assert item.reason.startswith("metadata-unavailable")


def test_check_plan_json_roundtrip(tmp_path: Path):
    app = make_app(client=_mock_client())
    entries = sample_list_datasets("exp")
    plan = app.check_datasets(entries, tmp_path, workers=1)
    restored = CheckPlan.from_json(plan.to_json())
    assert restored.fetcher == plan.fetcher
    assert [it.id for it in restored.items] == [it.id for it in plan.items]
    assert restored.items[0].entry["url"] == entries[0]["url"]


def test_check_plan_from_json_rejects_garbage():
    with pytest.raises(ValueError, match="not a check plan"):
        CheckPlan.from_json('{"foo": 1}')
    with pytest.raises(ValueError, match="not a check plan"):
        CheckPlan.from_json("not json")


def test_builtin_check_command(tmp_path: Path):
    app = make_app(client=_mock_client())
    result = runner.invoke(app.app, ["check", "exp", "-o", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "download" in result.output


def test_builtin_check_json_output(tmp_path: Path):
    app = make_app(client=_mock_client())
    result = runner.invoke(app.app, ["check", "exp", "-o", str(tmp_path), "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["items"][0]["action"] == "download"


def test_builtin_sync_from_plan_downloads_only_planned(tmp_path: Path):
    from quantilica.cli.sdk import default_path_builder

    payload = b"0123456789"
    app = make_app(client=_mock_client(payload))
    fresh_entry = sample_list_datasets("exp")[0]
    stale_entry = dict(sample_list_datasets("imp")[0])
    local = default_path_builder(tmp_path, fresh_entry, dt.date(2024, 1, 1))
    local.parent.mkdir(parents=True, exist_ok=True)
    local.write_bytes(payload)

    plan = app.check_datasets([fresh_entry, stale_entry], tmp_path, workers=1)
    actions = {it.id: it.action for it in plan.items}
    assert actions == {"exp-monthly": "skip-up-to-date", "imp-monthly": "download"}

    plan_file = tmp_path / "plan.json"
    plan_file.write_text(plan.to_json(), encoding="utf-8")
    result = runner.invoke(
        app.app, ["sync", "--from-plan", str(plan_file), "-o", str(tmp_path)]
    )
    assert result.exit_code == 0, result.output
    assert "1/1 arquivo(s) baixado(s)" in result.output


def test_builtin_sync_from_plan_invalid_file(tmp_path: Path):
    app = make_app(client=_mock_client())
    bad = tmp_path / "bad.json"
    bad.write_text('{"nope": true}', encoding="utf-8")
    result = runner.invoke(app.app, ["sync", "--from-plan", str(bad)])
    assert result.exit_code == 1
    assert "Plano inválido" in result.output


# ---------------------------------------------------------------
# default_client: overrides via ambiente
# ---------------------------------------------------------------


def test_default_client_verify_defaults_true(monkeypatch):
    monkeypatch.delenv("QUANTILICA_SSL_VERIFY", raising=False)
    monkeypatch.delenv("QUANTILICA_CA_BUNDLE", raising=False)
    assert default_client().verify is True


def test_default_client_verify_env_disable(monkeypatch):
    monkeypatch.delenv("QUANTILICA_CA_BUNDLE", raising=False)
    monkeypatch.setenv("QUANTILICA_SSL_VERIFY", "0")
    assert default_client().verify is False


def test_default_client_attempts_env_override(monkeypatch):
    monkeypatch.delenv("QUANTILICA_SSL_VERIFY", raising=False)
    monkeypatch.delenv("QUANTILICA_CA_BUNDLE", raising=False)
    monkeypatch.setenv("QUANTILICA_HTTP_ATTEMPTS", "9")
    assert default_client().attempts == 9


def test_check_entry_ephemeral_manifest_fresh(tmp_path: Path):
    """Raw file deleted but sidecar manifest survives -> manifest-fresh skip."""
    from quantilica.core.manifests import (
        DownloadManifest,
        SourceMetadata,
        write_manifest_sidecar,
    )

    app = make_app(client=_mock_client())
    entry = sample_list_datasets("exp")[0]
    local = default_path_builder(tmp_path, entry, dt.date(2024, 1, 1))
    local.parent.mkdir(parents=True, exist_ok=True)
    write_manifest_sidecar(
        local,
        DownloadManifest(
            source_id="src",
            dataset_id="ds",
            url=entry["url"],
            fetched_at="2024-01-02T00:00:00+00:00",
            sha256="0" * 64,
            size_bytes=10,
            source_meta=SourceMetadata(
                etag='"abc123"',
                last_modified="Mon, 01 Jan 2024 00:00:00 GMT",
            ),
        ),
    )
    item = app.check_entry(entry, tmp_path)
    assert item.action == "skip-up-to-date"
    assert item.reason == "manifest-fresh"
    assert item.local_exists is True


def test_check_entry_ephemeral_stale_downloads(tmp_path: Path):
    """Sidecar with stale metadata (remote ETag differs) -> download."""
    from quantilica.core.manifests import (
        DownloadManifest,
        SourceMetadata,
        write_manifest_sidecar,
    )

    app = make_app(client=_mock_client())
    entry = sample_list_datasets("exp")[0]
    local = default_path_builder(tmp_path, entry, dt.date(2024, 1, 1))
    local.parent.mkdir(parents=True, exist_ok=True)
    write_manifest_sidecar(
        local,
        DownloadManifest(
            source_id="src",
            dataset_id="ds",
            url=entry["url"],
            fetched_at="2023-12-31T00:00:00+00:00",
            sha256="0" * 64,
            size_bytes=10,
            source_meta=SourceMetadata(etag='"outdated"', last_modified=None),
        ),
    )
    item = app.check_entry(entry, tmp_path)
    assert item.action == "download"
    assert item.reason == "remote-newer"


def test_source_id_defaults_to_name_without_suffix():
    app = make_app()
    assert app.source_id == "comex"
    assert app.default_output == Path("/data/comex")


def test_explicit_source_id_derives_default_output():
    app = make_app(source_id="custom")
    assert app.source_id == "custom"
    assert app.default_output == Path("/data/custom")


def test_explicit_default_output_wins_over_source_id(tmp_path: Path):
    app = make_app(source_id="custom", default_output=tmp_path)
    assert app.default_output == tmp_path


def test_check_datasets_isolates_entry_failure(tmp_path: Path, monkeypatch):
    """Uma entrada que levanta (ex.: OSError) não derruba o check inteiro."""
    monkeypatch.setattr("quantilica.cli.sdk.time.sleep", lambda _s: None)
    app = make_app(
        client=HttpClient(
            attempts=1,
            transport=httpx2.MockTransport(lambda r: httpx2.Response(200)),
        )
    )
    entries = sample_list_datasets("exp")
    calls: dict[str, int] = {}
    real = app.check_entry

    def flaky(entry, output_dir):
        key = str(entry["id"])
        calls[key] = calls.get(key, 0) + 1
        if key == str(entries[0]["id"]):
            raise FileExistsError(17, "File exists", "/data")
        return real(entry, output_dir)

    monkeypatch.setattr(app, "check_entry", flaky)
    plan = app.check_datasets(entries, tmp_path)
    assert len(plan.items) == len(entries)
    bad = next(i for i in plan.items if i.id == str(entries[0]["id"]))
    assert bad.action == "download"
    assert bad.reason.startswith("check-error: FileExistsError")
    assert calls[str(entries[0]["id"])] == 2  # uma nova tentativa


def test_check_datasets_retry_recovers_transient_failure(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("quantilica.cli.sdk.time.sleep", lambda _s: None)
    app = make_app(
        client=HttpClient(
            attempts=1,
            transport=httpx2.MockTransport(lambda r: httpx2.Response(200)),
        )
    )
    entries = sample_list_datasets("exp")[:1]
    real = app.check_entry
    state = {"n": 0}

    def once(entry, output_dir):
        state["n"] += 1
        if state["n"] == 1:
            raise OSError("transiente")
        return real(entry, output_dir)

    monkeypatch.setattr(app, "check_entry", once)
    plan = app.check_datasets(entries, tmp_path)
    assert plan.items[0].reason != "" and not plan.items[0].reason.startswith(
        "check-error"
    )


def test_check_entry_403_head_without_probe_support_downloads(tmp_path: Path):
    """HEAD 403 e probe Range 403 → download (nunca exceção/exit 1)."""
    app = make_app(
        client=HttpClient(
            attempts=1,
            transport=httpx2.MockTransport(lambda r: httpx2.Response(403)),
        )
    )
    item = app.check_entry(sample_list_datasets("exp")[0], tmp_path)
    assert item.action == "download"


# ---------------------------------------------------------------
# command_convert / command_pipeline: Lake Hive
# ---------------------------------------------------------------


def test_convert_lake_flag_and_output_alias(tmp_path: Path):
    app = make_app()
    calls: list[tuple[Path, Path]] = []

    @app.command_convert
    def conv(input: Path, lake: Path) -> None:
        calls.append((input, lake))

    for flag in ("--lake", "-o", "--output"):
        result = runner.invoke(
            _grouped(app.app),
            ["fd", "convert", "-i", str(tmp_path), flag, str(tmp_path / "l")],
        )
        assert result.exit_code == 0, result.output
    assert calls == [(tmp_path, tmp_path / "l")] * 3


def test_convert_passes_dataset_and_force_when_accepted(tmp_path: Path):
    app = make_app()
    seen: dict = {}

    @app.command_convert
    def conv(input: Path, lake: Path, dataset=None, force=False) -> None:
        seen.update(dataset=dataset, force=force)

    result = runner.invoke(
        _grouped(app.app),
        ["fd", "convert", "--dataset", "a", "--dataset", "b", "--force"],
    )
    assert result.exit_code == 0, result.output
    assert seen == {"dataset": ["a", "b"], "force": True}


def test_convert_kwargs_func_receives_dataset_and_force():
    app = make_app()
    seen: dict = {}

    @app.command_convert
    def conv(input: Path, lake: Path, **kwargs) -> None:
        seen.update(kwargs)

    result = runner.invoke(_grouped(app.app), ["fd", "convert"])
    assert result.exit_code == 0, result.output
    assert seen == {"dataset": None, "force": False}


def test_convert_legacy_two_arg_func_ignores_unused_defaults():
    app = make_app()
    calls: list[tuple[Path, Path]] = []

    @app.command_convert
    def conv(input: Path, lake: Path) -> None:
        calls.append((input, lake))

    result = runner.invoke(_grouped(app.app), ["fd", "convert"])
    assert result.exit_code == 0, result.output
    assert len(calls) == 1


@pytest.mark.parametrize("flags", [["--dataset", "x"], ["--force"]])
def test_convert_legacy_func_rejects_requested_unsupported_flag(flags: list[str]):
    app = make_app()

    @app.command_convert
    def conv(input: Path, lake: Path) -> None: ...

    result = runner.invoke(_grouped(app.app), ["fd", "convert", *flags])
    assert result.exit_code == 2
    assert "não suporta" in result.output


def test_convert_summary_from_object_and_exit_0_without_errors():
    from types import SimpleNamespace

    app = make_app()

    @app.command_convert
    def conv(input: Path, lake: Path):
        return SimpleNamespace(converted=3, skipped=2, errors=0)

    result = runner.invoke(_grouped(app.app), ["fd", "convert"])
    assert result.exit_code == 0, result.output
    assert "convertidos" in result.output
    assert "pulados" in result.output
    assert "Conversão concluída" in result.output


def test_convert_summary_from_dict_with_errors_exits_1():
    app = make_app()

    @app.command_convert
    def conv(input: Path, lake: Path):
        return {"converted": ["a"], "skipped": [], "errors": ["boom [x]"]}

    result = runner.invoke(_grouped(app.app), ["fd", "convert"])
    assert result.exit_code == 1
    assert "boom [x]" in result.output
    assert "com erros" in result.output


def test_convert_unrelated_return_value_is_ignored():
    app = make_app()

    @app.command_convert
    def conv(input: Path, lake: Path):
        return 42

    result = runner.invoke(_grouped(app.app), ["fd", "convert"])
    assert result.exit_code == 0, result.output
    assert "convertidos" not in result.output


def test_pipeline_lake_flag_and_parquet_dir_alias(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    app = make_app()
    monkeypatch.setattr(
        app, "download_datasets", lambda entries, output_dir, workers=4: (0, 0, [])
    )
    calls: list[tuple[Path, Path]] = []

    @app.command_pipeline
    def conv(input: Path, lake: Path) -> None:
        calls.append((input, lake))

    for flag in ("--lake", "--parquet-dir"):
        result = runner.invoke(
            _grouped(app.app),
            ["fd", "pipeline", "exp", "-o", str(tmp_path), flag, str(tmp_path / "l")],
        )
        assert result.exit_code == 0, result.output
    assert calls == [(tmp_path, tmp_path / "l")] * 2
