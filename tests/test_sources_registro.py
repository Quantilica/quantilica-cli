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
    "ibge": "ibge-sidra-fetcher",
    "ibge-sidra": "ibge-sidra-fetcher",
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
    "sidra": "ibge-sidra-fetcher",
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
    for nome, dist in sources._RESERVA_OFFLINE.items():
        assert dist != f"{nome}-fetcher", f"{nome!r} segue a regra; tire da tabela"


def test_reserva_offline_mantem_o_nome_antigo_como_alias():
    assert sources.SOURCES_REGISTRY is sources._RESERVA_OFFLINE


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


def test_sidra_e_ibge_levam_ao_fetcher_renomeado():
    assert sources.resolve_dist("ibge-sidra") == "ibge-sidra-fetcher"
    assert sources.resolve_dist("sidra") == "ibge-sidra-fetcher"
    assert sources.resolve_dist("ibge") == "ibge-sidra-fetcher"
    # O nome antigo da distribuição resolve para a nova (como install e doctor).
    assert sources.resolve_dist("sidra-fetcher") == "ibge-sidra-fetcher"


def test_nome_antigo_da_distribuicao_avisa_no_stderr(capsys):
    assert sources.resolve_dist("sidra-fetcher") == "ibge-sidra-fetcher"
    assert "renomeada para 'ibge-sidra-fetcher'" in capsys.readouterr().err
