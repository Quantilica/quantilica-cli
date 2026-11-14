"""Export v2 do registro: identidade de empacotamento e ecossistema.

Usa um pacote fictício instalado em ``tmp_path`` (``*.dist-info`` real, lido por
``importlib.metadata``), sem depender dos fetchers do ambiente.
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.metadata as md
import json
import sys
import textwrap
from pathlib import Path

import pytest
from quantilica.core.registry import registry_sha256
from typer.testing import CliRunner

from quantilica.cli import registry as reg
from quantilica.cli.registry import app

runner = CliRunner()


def _instala(
    raiz: Path,
    dist: str,
    fid: str | None = None,
    *,
    version: str = "1.2.3",
    urls: tuple[str, ...] = (),
    lake: bool = False,
    declara_info: bool = True,
) -> None:
    """Cria o módulo e o ``dist-info`` de um fetcher fictício em ``raiz``."""
    fid = fid or dist.removesuffix("-fetcher")
    mod = dist.replace("-", "_")
    pkg = raiz / mod
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    info = (
        f'info=FetcherInfo("{fid}", nome="{fid.upper()}", aliases=("{fid}x",))'
        if declara_info
        else "info=None"
    )
    (pkg / "plugin.py").write_text(
        textwrap.dedent(
            f"""
            from quantilica.core.registry import FetcherInfo
            from quantilica.cli.sdk import FetcherApp

            fetcher = FetcherApp(
                "{dist}",
                groups_dict={{"g": {{"name": "g"}}}},
                aliases_dict={{}},
                list_datasets=lambda g: [],
                client=object(),
                {info},
            )
            app = fetcher.app
            """
        )
    )
    di = raiz / f"{mod}-{version}.dist-info"
    di.mkdir()
    meta = [
        "Metadata-Version: 2.1",
        f"Name: {dist}",
        f"Version: {version}",
        *(f"Project-URL: {u}" for u in urls),
    ]
    (di / "METADATA").write_text("\n".join(meta) + "\n")
    eps = f"[quantilica.fetchers]\n{fid} = {mod}.plugin:app\n"
    if lake:
        eps += f"\n[quantilica.lake]\n{fid} = {mod}.lake:converter\n"
    (di / "entry_points.txt").write_text(eps)


@pytest.fixture
def raiz(tmp_path, monkeypatch):
    """Só as distribuições de ``raiz`` aparecem como plugins."""
    r = tmp_path / "site"
    r.mkdir()
    monkeypatch.syspath_prepend(str(r))
    importlib.invalidate_caches()

    def only_here(group: str):
        return [
            ep
            for d in md.distributions(path=[str(r)])
            for ep in d.entry_points
            if ep.group == group
        ]

    monkeypatch.setattr(reg, "entry_points", only_here)
    yield r
    pacotes = {d.name for d in r.iterdir() if d.is_dir() and d.suffix != ".dist-info"}
    for nome in list(sys.modules):  # módulos fictícios não vazam
        if nome.split(".")[0] in pacotes:
            del sys.modules[nome]


def _export(*args: str):
    return runner.invoke(app, ["export", "--json", *args])


def test_export_v2_identidade_vem_dos_metadados(raiz):
    _instala(
        raiz,
        "alfa-fetcher",
        version="0.7.1",
        urls=(
            "Homepage, https://example.org/alfa",
            "Repository, https://github.com/Quantilica/alfa-fetcher/",
        ),
        lake=True,
    )
    res = _export("--strict")
    assert res.exit_code == 0, res.output
    doc = json.loads(res.stdout)
    assert doc["version"] == 2
    (alfa,) = doc["fetchers"]
    assert alfa["id"] == "alfa" and alfa["aliases"] == ["alfax"]
    assert alfa["distribution"] == "alfa-fetcher"
    assert alfa["module"] == "alfa_fetcher"
    assert alfa["repo"] == "https://github.com/Quantilica/alfa-fetcher"
    assert alfa["version"] == "0.7.1"
    assert alfa["entry_point"] == "alfa" and alfa["lake_entry_point"] == "alfa"
    assert alfa["lake"] is True and alfa["lake_source_id"] == "alfa"
    assert alfa["source_id"] == "alfa"
    # campos da versão 1 seguem presentes, com o mesmo nome
    for chave in ("id", "nome", "aliases", "orgao", "licenca", "homepage",
                  "cadencia", "dominio", "redistribuicao", "restricao_nota",
                  "info_declared", "freshness_policy", "default_output",
                  "groups", "error"):  # fmt: skip
        assert chave in alfa
    # sem --ecossistema, a camada 3 não aparece
    assert "docs_pasta" not in alfa and "historico" not in alfa


def test_repo_ausente_cai_no_padrao_com_aviso(raiz):
    _instala(raiz, "beta-fetcher")
    res = _export()
    assert res.exit_code == 0
    (beta,) = json.loads(res.stdout)["fetchers"]
    assert beta["repo"] == "https://github.com/Quantilica/beta-fetcher"
    assert "sem [project.urls]" in res.stderr


def test_repo_usa_source_quando_nao_ha_repository(raiz):
    _instala(raiz, "beta-fetcher", urls=("Source, https://git.example/beta",))
    (beta,) = json.loads(_export().stdout)["fetchers"]
    assert beta["repo"] == "https://git.example/beta"


def test_sha256_confere_com_o_conteudo(raiz):
    _instala(raiz, "alfa-fetcher", urls=("Repository, https://github.com/Q/alfa",))
    doc = json.loads(_export().stdout)
    assert doc["sha256"] == registry_sha256(doc)
    canon = json.dumps(
        {"version": doc["version"], "fetchers": doc["fetchers"]},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    assert doc["sha256"] == hashlib.sha256(canon.encode()).hexdigest()


TOML = """
[alfa]
docs_pasta = "ibge"
vitrine = true
ci_integracao = true
json_leve = true
mascote = "gata"
health_url = "https://example.org/ping"

[[alfa.historico]]
distribution = "velho-fetcher"
repo = "Quantilica/velho-fetcher"
id = "velho"
data = 2026-11-13
"""


def test_export_com_ecossistema_junta_a_terceira_camada(raiz, tmp_path):
    _instala(raiz, "alfa-fetcher", urls=("Repository, https://github.com/Q/alfa",))
    toml = tmp_path / "registro.toml"
    toml.write_text(TOML)
    res = _export("--strict", "--ecossistema", str(toml))
    assert res.exit_code == 0, res.output
    (alfa,) = json.loads(res.stdout)["fetchers"]
    assert alfa["docs_pasta"] == "ibge" and alfa["vitrine"] is True
    assert alfa["ci_integracao"] is True and alfa["json_leve"] is True
    assert alfa["mascote"] == "gata"
    assert alfa["health_url"] == "https://example.org/ping"
    assert alfa["historico"] == [
        {
            "distribution": "velho-fetcher",
            "repo": "Quantilica/velho-fetcher",
            "id": "velho",
            "data": "2026-11-13",
        }
    ]


def test_ecossistema_invalido_falha(raiz, tmp_path):
    _instala(raiz, "alfa-fetcher", urls=("Repository, https://github.com/Q/alfa",))
    toml = tmp_path / "registro.toml"
    toml.write_text('[alfa]\ndocs_pasta = "nao-existe"\n')
    res = _export("--ecossistema", str(toml))
    assert res.exit_code == 1 and "vocabulário" in res.stderr


def test_ecossistema_com_id_fora_do_export_falha(raiz, tmp_path):
    _instala(raiz, "alfa-fetcher", urls=("Repository, https://github.com/Q/alfa",))
    toml = tmp_path / "registro.toml"
    toml.write_text('[alfa]\ndocs_pasta = "ibge"\n[zeta]\ndocs_pasta = "bcb"\n')
    res = _export("--ecossistema", str(toml))
    assert res.exit_code == 1 and "zeta" in res.stderr


def test_strict_exige_secao_de_todo_fetcher_do_export(raiz, tmp_path):
    _instala(raiz, "alfa-fetcher", urls=("Repository, https://github.com/Q/alfa",))
    _instala(raiz, "beta-fetcher", urls=("Repository, https://github.com/Q/beta",))
    toml = tmp_path / "registro.toml"
    toml.write_text('[alfa]\ndocs_pasta = "ibge"\n')
    assert _export("--ecossistema", str(toml)).exit_code == 0
    res = _export("--strict", "--ecossistema", str(toml))
    assert res.exit_code == 1 and "beta" in res.stderr


def test_strict_distribuicao_fora_da_regra_sem_historico(raiz, tmp_path):
    # id "gama", distribuição "outro-fetcher": a regra pede "gama-fetcher"
    _instala(
        raiz,
        "outro-fetcher",
        fid="gama",
        urls=("Repository, https://github.com/Q/outro-fetcher",),
    )
    res = _export("--strict")
    assert res.exit_code == 1 and "foge da regra" in res.stderr
    assert _export().exit_code == 0  # sem --strict só informa
    toml = tmp_path / "registro.toml"
    toml.write_text('[gama]\ndocs_pasta = "ibge"\n')
    assert _export("--strict", "--ecossistema", str(toml)).exit_code == 1
    toml.write_text(
        '[gama]\ndocs_pasta = "ibge"\n[[gama.historico]]\n'
        'distribution = "outro-fetcher"\nrepo = "Quantilica/outro-fetcher"\n'
        'id = "outro"\ndata = 2026-11-14\n'
    )
    res = _export("--strict", "--ecossistema", str(toml))
    assert res.exit_code == 0, res.output


def test_plugin_quebrado_ainda_exporta_identidade(raiz):
    _instala(raiz, "alfa-fetcher", urls=("Repository, https://github.com/Q/alfa",))
    (raiz / "alfa_fetcher" / "plugin.py").write_text("raise ImportError('quebrado')")
    res = _export()
    assert res.exit_code == 0
    (alfa,) = json.loads(res.stdout)["fetchers"]
    assert "ImportError" in alfa["error"]
    assert alfa["distribution"] == "alfa-fetcher" and alfa["module"] == "alfa_fetcher"


def test_duplicidade_continua_exportando_com_sha256(raiz):
    _instala(raiz, "alfa-fetcher", urls=("Repository, https://github.com/Q/alfa",))
    _instala(raiz, "beta-fetcher", fid="alfa", urls=("Repository, https://x.org/b",))
    res = _export()
    assert res.exit_code == 0 and "Duplicidade" in res.stderr
    doc = json.loads(res.stdout)
    assert len(doc["fetchers"]) == 2 and doc["sha256"] == registry_sha256(doc)
    assert _export("--strict").exit_code == 1
