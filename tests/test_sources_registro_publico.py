"""Registro público no cli: cache de 24 h, offline e ordem de resolução."""

from __future__ import annotations

import copy
import json
import os
import time

import pytest
from quantilica.core.registry import registry_sha256

from quantilica.cli import health, sources


def _doc(*fetchers: dict, sha: bool = True) -> dict:
    doc = {"version": 2, "fetchers": copy.deepcopy(list(fetchers))}
    if sha:
        doc["sha256"] = registry_sha256(doc)
    return doc


NOVO = {
    "id": "novo",
    "aliases": ["nv"],
    "distribution": "novo-fetcher",
    "historico": [
        {
            "distribution": "antigo-fetcher",
            "repo": "Quantilica/antigo-fetcher",
            "id": "antigo",
            "data": "2026-11-14",
        }
    ],
    "health_url": "https://example.org/ping",
}
FORA_DA_REGRA = {"id": "peculiar", "aliases": ["pec"], "distribution": "outro-pkg"}


@pytest.fixture
def instalados(monkeypatch):
    """Define quais ids/apelidos aparecem como instalados."""

    def set_(nomes: dict[str, str]):
        monkeypatch.setattr(sources, "installed_declared_names", lambda: dict(nomes))

    set_({})
    return set_


@pytest.fixture
def rede(monkeypatch):
    """Controla o que o download devolve e conta as chamadas."""
    estado = {"doc": None, "chamadas": 0}

    def baixa():
        estado["chamadas"] += 1
        return estado["doc"]

    monkeypatch.setattr(sources, "_baixa_registro", baixa)
    return estado


# --- resolve_dist: instalado -> registro público -> reserva offline ---------


def test_ordem_instalado_vence_registro_e_reserva(instalados, rede):
    rede["doc"] = _doc(FORA_DA_REGRA)
    instalados({"sidra": "instalado-fetcher"})
    assert sources.resolve_dist("sidra") == "instalado-fetcher"
    assert rede["chamadas"] == 0  # instalado resolve sem rede


@pytest.mark.registro_real
def test_ordem_registro_publico_vence_a_reserva(instalados, rede):
    rede["doc"] = _doc({"id": "sidra", "distribution": "do-registro-fetcher"})
    assert sources.resolve_dist("sidra") == "do-registro-fetcher"
    assert sources.resolve_dist("peculiar") == "peculiar-fetcher"  # regra, não achou


@pytest.mark.registro_real
def test_registro_publico_traz_id_apelido_e_id_antigo(instalados, rede):
    rede["doc"] = _doc(NOVO, FORA_DA_REGRA)
    assert sources.resolve_dist("novo") == "novo-fetcher"
    assert sources.resolve_dist("nv") == "novo-fetcher"
    assert sources.resolve_dist("antigo") == "novo-fetcher"  # id do historico
    assert sources.resolve_dist("pec") == "outro-pkg"  # fora da regra
    assert sources.resolve_dist("peculiar") == "outro-pkg"
    # o nome antigo da distribuição passa como veio (wheels congeladas)
    assert sources.resolve_dist("antigo-fetcher") == "antigo-fetcher"


def test_sem_registro_a_reserva_offline_resolve(instalados, rede):
    assert rede["doc"] is None
    assert sources.resolve_dist("sidra") == "ibge-sidra-fetcher"
    assert sources.resolve_dist("sql") == "quantilica-sql"
    assert sources.resolve_dist("td") == "tesouro-direto-fetcher"
    assert sources.resolve_dist("qualquer") == "qualquer-fetcher"


def test_registry_explicito_vale_primeiro(instalados, rede):
    assert sources.resolve_dist("sidra", {"sidra": "x-fetcher"}) == "x-fetcher"
    assert rede["chamadas"] == 0


@pytest.mark.registro_real
def test_merged_registry_junta_as_camadas(instalados, rede, monkeypatch):
    rede["doc"] = _doc(NOVO)
    monkeypatch.setattr(sources, "fetch_remote_sources", lambda: {"remoto": "r-f"})
    instalados({"inst": "inst-fetcher"})
    merged = sources._merged_registry()
    assert merged["nv"] == "novo-fetcher" and merged["remoto"] == "r-f"
    assert merged["inst"] == "inst-fetcher" and merged["sql"] == "quantilica-sql"


# --- cache de 24 h e offline -------------------------------------------------


@pytest.mark.registro_real
def test_baixa_e_grava_o_cache(rede):
    rede["doc"] = _doc(NOVO)
    assert sources.registro_publico() == rede["doc"]
    cache = sources.registry_cache_path()
    assert json.loads(cache.read_text())["sha256"] == rede["doc"]["sha256"]
    assert rede["chamadas"] == 1


@pytest.mark.registro_real
def test_cache_fresco_nao_vai_a_rede(rede):
    rede["doc"] = _doc(NOVO)
    sources.registro_publico()
    rede["doc"] = _doc(FORA_DA_REGRA)  # o índice mudou, mas o cache vale 24 h
    assert sources.registro_publico()["fetchers"][0]["id"] == "novo"
    assert rede["chamadas"] == 1


@pytest.mark.registro_real
def test_refresh_ignora_o_cache_fresco(rede):
    rede["doc"] = _doc(NOVO)
    sources.registro_publico()
    rede["doc"] = _doc(FORA_DA_REGRA)
    assert sources.registro_publico(refresh=True)["fetchers"][0]["id"] == "peculiar"
    assert sources.registro_publico()["fetchers"][0]["id"] == "peculiar"  # recacheado
    assert rede["chamadas"] == 2


@pytest.mark.registro_real
def test_cache_vencido_baixa_de_novo(rede):
    rede["doc"] = _doc(NOVO)
    sources.registro_publico()
    cache = sources.registry_cache_path()
    velho = time.time() - sources.REGISTRY_CACHE_TTL - 60
    os.utime(cache, (velho, velho))
    rede["doc"] = _doc(FORA_DA_REGRA)
    assert sources.registro_publico()["fetchers"][0]["id"] == "peculiar"
    assert rede["chamadas"] == 2


@pytest.mark.registro_real
def test_cache_vencido_e_rede_fora_usa_o_cache_vencido(rede):
    rede["doc"] = _doc(NOVO)
    sources.registro_publico()
    cache = sources.registry_cache_path()
    velho = time.time() - sources.REGISTRY_CACHE_TTL - 60
    os.utime(cache, (velho, velho))
    rede["doc"] = None  # offline
    assert sources.registro_publico()["fetchers"][0]["id"] == "novo"


@pytest.mark.registro_real
def test_offline_sem_cache_devolve_none_e_so_tenta_uma_vez(rede):
    assert sources.registro_publico() is None
    assert sources.registro_publico() is None
    assert rede["chamadas"] == 1  # a falha fica marcada no processo
    rede["doc"] = _doc(NOVO)
    assert sources.registro_publico(refresh=True) is not None  # --refresh tenta


@pytest.mark.registro_real
def test_cache_adulterado_e_ignorado(rede):
    rede["doc"] = None
    cache = sources.registry_cache_path()
    cache.parent.mkdir(parents=True)
    doc = _doc(NOVO)
    doc["fetchers"][0]["id"] = "adulterado"  # sha256 deixa de bater
    cache.write_text(json.dumps(doc))
    assert sources.registro_publico() is None


def test_registro_v1_sem_sha256_e_aceito():
    doc = {"version": 1, "fetchers": [{"id": "anp", "aliases": []}]}
    assert sources._registro_valido(doc) == doc
    assert sources._registro_valido({"fetchers": "x"}) is None
    assert sources._registro_valido([]) is None


def test_download_com_sha256_errado_e_recusado(monkeypatch):
    class Resp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            doc = _doc(NOVO)
            doc["sha256"] = "0" * 64
            return json.dumps(doc).encode()

    monkeypatch.setattr(sources.urllib.request, "urlopen", lambda *a, **k: Resp())
    monkeypatch.setattr(sources, "get_index_url", lambda: "https://i.example/simple/")
    assert sources.registry_url() == "https://i.example/registry.json"
    assert sources._baixa_registro() is None


# --- renomes e health vindos do registro -------------------------------------


@pytest.mark.registro_real
def test_distribuicoes_renomeadas_vem_do_historico_com_reserva(instalados, rede):
    assert sources.distribuicoes_renomeadas() == {
        "sidra-fetcher": "ibge-sidra-fetcher"  # reserva, sem registro
    }
    rede["doc"] = _doc(NOVO)
    sources.registro_publico(refresh=True)
    assert sources.distribuicoes_renomeadas() == {
        "sidra-fetcher": "ibge-sidra-fetcher",
        "antigo-fetcher": "novo-fetcher",
    }


@pytest.mark.registro_real
def test_renamed_old_dists_usa_o_historico(instalados, rede, monkeypatch):
    rede["doc"] = _doc(NOVO)
    monkeypatch.setattr(sources, "is_dist_installed", lambda d: d == "antigo-fetcher")
    assert sources.renamed_old_dists("novo-fetcher") == ["antigo-fetcher"]
    registered = {"antigo": "antigo-fetcher", "anp": "anp-fetcher"}
    assert sources._translate_state(registered) is True
    assert registered == {"antigo": "novo-fetcher", "anp": "anp-fetcher"}


def test_health_sources_reserva_sem_registro(rede):
    assert health.health_sources() == health.HEALTH_SOURCES


@pytest.mark.registro_real
def test_health_sources_vem_do_health_url(rede):
    rede["doc"] = _doc(NOVO, FORA_DA_REGRA)
    assert health.health_sources() == [("novo", "https://example.org/ping")]


@pytest.mark.registro_real
def test_health_sources_registro_sem_sondas_cai_na_reserva(rede):
    rede["doc"] = _doc(FORA_DA_REGRA)
    assert health.health_sources() == health.HEALTH_SOURCES
