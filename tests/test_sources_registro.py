"""Guarda: ``sources`` resolve pelo registro e pela regra ``<id>-fetcher``."""

from __future__ import annotations

import pytest

from quantilica.cli import sources
from quantilica.cli.registry import discover_fetchers

# Tabela antiga (escrita à mão) congelada: tudo isso precisa continuar valendo.
TABELA_ANTIGA = {
    "anac": "anac-fetcher",
    "anp": "anp-fetcher",
    "antt": "antt-fetcher",
    "b3": "b3-fetcher",
    "bcb": "bcb-sgs-fetcher",
    "bcb-microdados": "bcb-microdados-fetcher",
    "bcb_microdados": "bcb-microdados-fetcher",
    "bcb-sgs": "bcb-sgs-fetcher",
    "capes": "capes-fetcher",
    "comex": "comex-fetcher",
    "cvm": "cvm-fetcher",
    "datasus": "datasus-fetcher",
    "datasus-sigtap": "sigtap-fetcher",
    "inep": "inep-fetcher",
    "infosiga": "infosiga-fetcher",
    "inmet": "inmet-fetcher",
    "ipeadata": "ipeadata-fetcher",
    "loterias": "loterias-fetcher",
    "ons": "ons-fetcher",
    "pdet": "pdet-fetcher",
    "pgfn": "pgfn-fetcher",
    "rfb": "rfb-cnpj-fetcher",
    "rfb-cnpj": "rfb-cnpj-fetcher",
    "rtn": "rtn-fetcher",
    "sidra": "sidra-fetcher",
    "sigtap": "sigtap-fetcher",
    "sql": "quantilica-sql",
    "td": "tesouro-direto-fetcher",
    "tesouro-direto": "tesouro-direto-fetcher",
    "tesouro_direto": "tesouro-direto-fetcher",
    "transparencia-sp": "transparencia-sp-fetcher",
    "transparencia_sp": "transparencia-sp-fetcher",
    "tse": "tse-fetcher",
}


@pytest.mark.parametrize(("nome", "dist"), sorted(TABELA_ANTIGA.items()))
def test_nomes_antigos_resolvem_para_a_mesma_distribuicao(nome, dist):
    assert sources.resolve_dist(nome) == dist


def test_tabela_manual_so_tem_o_que_a_regra_nao_cobre():
    for nome, dist in sources.SOURCES_REGISTRY.items():
        assert dist != f"{nome}-fetcher", f"{nome!r} segue a regra; tire da tabela"


def test_fetchers_instalados_seguem_a_regra():
    for d in discover_fetchers():
        if not d.info_declared:
            continue
        assert sources.resolve_dist(d.info.id) == f"{d.info.id}-fetcher"
        assert d.distribution in (None, f"{d.info.id}-fetcher")


def test_fetcher_novo_sem_editar_a_tabela():
    assert sources.resolve_dist("novo") == "novo-fetcher"


def test_nomes_que_ja_sao_distribuicao_passam_como_vieram():
    assert sources.resolve_dist("quantilica-analytics") == "quantilica-analytics"
    assert sources.resolve_dist("anp-fetcher") == "anp-fetcher"
