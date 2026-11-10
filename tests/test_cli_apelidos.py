"""Apelidos como subcomandos ocultos: o id é o nome canônico."""

from __future__ import annotations

import subprocess
import sys
from types import SimpleNamespace

import pytest
import typer
from quantilica.core.registry import FetcherInfo
from typer.testing import CliRunner

from quantilica.cli import cli

runner = CliRunner()


def _plugin(
    fid: str | None, aliases: tuple[str, ...] = (), source_id: str = "x"
) -> typer.Typer:
    plugin = typer.Typer(help=f"Ajuda de {fid or source_id}")

    @plugin.command("sync")
    def sync() -> None:
        """Sincroniza."""
        typer.echo(f"sync:{source_id}")

    if fid is not None:
        plugin.fetcher_app = SimpleNamespace(  # type: ignore[attr-defined]
            info=FetcherInfo(id=fid, nome=fid, aliases=aliases),
            info_declared=True,
            source_id=source_id,
        )
    return plugin


@pytest.fixture
def fresh_app(monkeypatch):
    app = typer.Typer(no_args_is_help=True)

    @app.command("lake")
    def lake() -> None:
        typer.echo("builtin")

    @app.command("docs")
    def docs() -> None:
        typer.echo("builtin")

    monkeypatch.setattr(cli, "app", app)
    return app


def _mount(plugins, reserved=("lake", "docs")):
    cli._mount_fetchers(plugins, set(reserved))


def _help_names(app) -> list[str]:
    out = runner.invoke(app, ["--help"]).output
    return out.split("Commands")[-1].split()


def test_id_e_visivel_apelidos_e_entry_point_ocultos(fresh_app):
    _mount({"td": _plugin("tesouro-direto", ("td", "tddata", "tesouro_direto"), "td")})
    for nome in ("tesouro-direto", "td", "tddata", "tesouro_direto"):
        r = runner.invoke(fresh_app, [nome, "sync"])
        assert r.exit_code == 0, nome
        assert r.output.strip() == "sync:td"
    visiveis = runner.invoke(fresh_app, ["--help"]).output
    assert "tesouro-direto" in visiveis
    assert "tddata" not in visiveis
    assert "tesouro_direto" not in visiveis


def test_help_igual_pelo_id_e_pelo_nome_antigo(fresh_app):
    _mount({"td": _plugin("tesouro-direto", ("tddata",))})
    a = runner.invoke(fresh_app, ["td", "--help"]).output
    b = runner.invoke(fresh_app, ["tesouro-direto", "--help"]).output
    c = runner.invoke(fresh_app, ["tddata", "--help"]).output
    norm = lambda t: [ln for ln in t.splitlines() if "Usage" not in ln]  # noqa: E731
    assert norm(a) == norm(b) == norm(c)


def test_plugin_sem_info_mantem_nome_do_entry_point(fresh_app):
    _mount({"antigo": _plugin(None, source_id="antigo")})
    assert runner.invoke(fresh_app, ["antigo", "sync"]).output.strip() == "sync:antigo"


def test_apelido_colide_com_embutido(fresh_app, capsys):
    _mount({"abc": _plugin("abc", ("lake", "abcd"))})
    assert runner.invoke(fresh_app, ["lake"]).output.strip() == "builtin"
    assert runner.invoke(fresh_app, ["abcd", "sync"]).exit_code == 0
    err = capsys.readouterr().err
    assert err.count("'lake'") == 1
    assert "Aviso" in err


def test_apelido_colide_com_id_de_outro_plugin(fresh_app, capsys):
    _mount(
        {
            "a": _plugin("alfa", ("beta",), "alfa"),
            "b": _plugin("beta", (), "beta"),
        }
    )
    assert runner.invoke(fresh_app, ["beta", "sync"]).output.strip() == "sync:beta"
    assert "'beta'" in capsys.readouterr().err


def test_apelido_colide_com_entry_point_de_outro(fresh_app, capsys):
    _mount(
        {
            "a": _plugin("alfa", ("legado",), "alfa"),
            "legado": _plugin(None, source_id="legado"),
        }
    )
    assert runner.invoke(fresh_app, ["legado", "sync"]).output.strip() == "sync:legado"
    assert "'legado'" in capsys.readouterr().err


def test_apelido_reivindicado_por_dois_plugins_nao_e_montado(fresh_app, capsys):
    _mount(
        {
            "a": _plugin("alfa", ("curto",), "alfa"),
            "b": _plugin("beta", ("curto",), "beta"),
        }
    )
    assert runner.invoke(fresh_app, ["curto", "sync"]).exit_code != 0
    assert runner.invoke(fresh_app, ["alfa", "sync"]).exit_code == 0
    assert runner.invoke(fresh_app, ["beta", "sync"]).exit_code == 0
    assert capsys.readouterr().err.count("'curto'") == 1


def test_nao_carrega_plugin_duas_vezes(monkeypatch):
    chamadas = []

    class EP:
        name = "td"

        def load(self):
            chamadas.append(1)
            return _plugin("tesouro-direto", ("tddata",))

    monkeypatch.setattr(
        cli, "entry_points", lambda group: [EP()] if "fetchers" in group else []
    )
    plugins = cli._load_plugins(cli.FETCHER_GROUP)
    cli._mount_fetchers(plugins, set())
    assert len(chamadas) == 1


def test_source_id_e_telemetria_nao_dependem_do_nome_do_subcomando():
    """O source_id vem do FetcherApp, não do nome pelo qual o usuário chamou."""
    from quantilica.cli.sdk import FetcherApp

    fa = FetcherApp(
        "tesouro-direto-fetcher",
        source_id="td",
        groups_dict={},
        aliases_dict={},
        list_datasets=lambda g: [],
        info=FetcherInfo(id="tesouro-direto", nome="TD", aliases=("td",)),
    )
    assert fa.source_id == "td"
    assert fa.default_output.as_posix() == "/data/td"


def _help(nome: str) -> str:
    r = subprocess.run(
        [sys.executable, "-m", "quantilica.cli.cli", nome, "--help"],
        capture_output=True,
        text=True,
        timeout=120,
    )
    return "\n".join(ln for ln in r.stdout.splitlines() if "Usage" not in ln)


def test_fumaca_real_td_e_tesouro_direto():
    pytest.importorskip("tesouro_direto_fetcher")
    a, b = _help("td"), _help("tesouro-direto")
    assert a and a == b
