"""Testes de `quantilica registry export` e da compatibilidade do FetcherApp."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from quantilica.core.registry import FetcherInfo
from typer.testing import CliRunner

from quantilica.cli import registry as reg
from quantilica.cli import sources
from quantilica.cli.registry import app
from quantilica.cli.sdk import FetcherApp, derive_fetcher_info

runner = CliRunner()


def _fetcher(name: str, info: FetcherInfo | None = None, **kw) -> FetcherApp:
    return FetcherApp(
        name,
        groups_dict={"g1": {"name": "g1"}, "g0": {"name": "g0"}},
        aliases_dict={},
        list_datasets=lambda g: [],
        client=object(),  # type: ignore[arg-type]
        info=info,
        **kw,
    )


def _ep(name: str, obj, dist: str | None = None, boom: bool = False):
    def load():
        if boom:
            raise ImportError("quebrado")
        return obj

    d = SimpleNamespace(name=dist) if dist else None
    return SimpleNamespace(name=name, dist=d, load=load)


@pytest.fixture
def fake_eps(monkeypatch: pytest.MonkeyPatch):
    def install(fetchers=(), lake=()):
        def fake(group: str):
            return {
                "quantilica.fetchers": list(fetchers),
                "quantilica.lake": list(lake),
            }[group]

        monkeypatch.setattr(reg, "entry_points", fake)

    return install


def test_sem_info_deriva_do_name():
    f = _fetcher("tesouro-direto-fetcher")
    assert f.info_declared is False
    assert f.info.id == "tesouro-direto" and f.info.nome == "tesouro-direto"
    assert f.source_id == "tesouro-direto"
    assert derive_fetcher_info("bcb_sgs-fetcher").id == "bcb-sgs"
    assert f.app.fetcher_app is f


def test_com_info_declarada_e_source_id_independente():
    info = FetcherInfo("tesouro-direto", nome="Tesouro Direto", aliases=("td",))
    f = _fetcher("tddata-fetcher", info=info, source_id="td")
    assert f.info_declared is True and f.info is info
    assert f.source_id == "td"


def test_export_json(fake_eps):
    info = FetcherInfo(
        "tesouro-direto", nome="Tesouro Direto", aliases=("td",), cadencia="daily"
    )
    a = _fetcher("tesouro-direto-fetcher", info=info, freshness_policy="remote_only")
    b = _fetcher("anp-fetcher")
    fake_eps(
        fetchers=[
            _ep("td", a.app, "tesouro-direto-fetcher"),
            _ep("anp", b.app, "anp-fetcher"),
            _ep("morto", None, "morto-fetcher", boom=True),
        ],
        lake=[_ep("tesouro-direto", object(), "tesouro-direto-fetcher")],
    )
    res = runner.invoke(app, ["export", "--json"])
    assert res.exit_code == 0
    data = json.loads(res.stdout)
    assert data["version"] == 1
    assert [f["id"] for f in data["fetchers"]] == ["anp", "morto", "tesouro-direto"]
    anp, morto, td = data["fetchers"]
    assert td["info_declared"] is True and td["aliases"] == ["td"]
    assert td["lake"] is True and td["lake_source_id"] == "tesouro-direto"
    assert td["entry_point"] == "td" and td["freshness_policy"] == "remote_only"
    assert td["groups"] == ["g0", "g1"] and td["cadencia"] == "daily"
    assert anp["info_declared"] is False and anp["lake"] is False
    assert "ImportError" in morto["error"]


def test_export_tabela(fake_eps):
    fake_eps(fetchers=[_ep("anp", _fetcher("anp-fetcher").app, "anp-fetcher")])
    res = runner.invoke(app, ["export"])
    assert res.exit_code == 0
    assert "anp" in res.stdout and "Registro de fetchers" in res.stdout


def test_strict_falha_sem_info(fake_eps):
    fake_eps(fetchers=[_ep("anp", _fetcher("anp-fetcher").app, "anp-fetcher")])
    assert runner.invoke(app, ["export", "--strict"]).exit_code == 1
    assert runner.invoke(app, ["export"]).exit_code == 0


def test_strict_ok_com_info_e_duplicidade_falha(fake_eps):
    def decl(i: str, aliases=()):
        info = FetcherInfo(i, nome=i, aliases=aliases)
        return _fetcher(f"{i}-fetcher", info=info).app

    fake_eps(fetchers=[_ep("a", decl("a"), "a-fetcher")])
    assert runner.invoke(app, ["export", "--strict", "--json"]).exit_code == 0

    fake_eps(
        fetchers=[
            _ep("a", decl("a"), "a-fetcher"),
            _ep("b", decl("b", aliases=("a",)), "b-fetcher"),
        ]
    )
    res = runner.invoke(app, ["export", "--strict", "--json"])
    assert res.exit_code == 1
    plain = runner.invoke(app, ["export", "--json"])
    assert plain.exit_code == 0 and len(json.loads(plain.stdout)["fetchers"]) == 2


def test_fonte_instalada_usa_apelidos_do_info(fake_eps, monkeypatch):
    info = FetcherInfo("tesouro-direto", nome="TD", aliases=("td", "tddata"))
    fake_eps(
        fetchers=[
            _ep(
                "td",
                _fetcher("tesouro-direto-fetcher", info=info).app,
                "tesouro-direto-fetcher",
            ),
            _ep("anp", _fetcher("anp-fetcher").app, "anp-fetcher"),
        ]
    )
    names = reg.declared_source_names()
    assert names == {
        "tesouro-direto": "tesouro-direto-fetcher",
        "td": "tesouro-direto-fetcher",
        "tddata": "tesouro-direto-fetcher",
    }
    monkeypatch.setattr(sources, "fetch_remote_sources", lambda: {})
    merged = sources._merged_registry()
    assert merged["tddata"] == "tesouro-direto-fetcher"
    assert merged["anp"] == "anp-fetcher"  # reserva manual


def test_apelidos_do_info_instalado_nao_contradizem_a_tabela():
    """Falha se um fetcher instalado apelida algo que a tabela manual aponta para outro pacote."""
    names = reg.declared_source_names()
    conflicts = {
        n: (dist, sources.SOURCES_REGISTRY[n])
        for n, dist in names.items()
        if n in sources.SOURCES_REGISTRY and sources.SOURCES_REGISTRY[n] != dist
    }
    assert not conflicts, f"FetcherInfo contradiz SOURCES_REGISTRY: {conflicts}"
