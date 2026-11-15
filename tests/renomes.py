"""Resolução pelo ``historico``: o que os testes de tabela aceitam depois de um renome.

Os testes que fixam a tabela de 14/11 (``TABELA_ANTIGA``, ``_RESERVA_OFFLINE``)
rodam contra o ambiente de quem os executa. Num ambiente com um fetcher já
renomeado instalado, o id antigo passa a resolver para a distribuição nova
(pelo apelido do ``FetcherInfo`` ou pelo ``historico`` do registro público), e
isso é o comportamento certo. ``aceita_resolucao`` aceita a distribuição da
tabela ou o seu renome, seguindo ``distribuicoes_renomeadas``.
"""

from __future__ import annotations

from quantilica.cli import registry as reg
from quantilica.cli import sources


def renomes_conhecidos() -> dict[str, str]:
    """Distribuição antiga -> nova: o ``historico`` e os fetchers instalados.

    Sem rede, o ``historico`` do registro não existe; o apelido ``X`` no
    ``FetcherInfo`` de um fetcher instalado diz que ``X-fetcher`` foi o seu nome.
    """
    out = dict(sources.distribuicoes_renomeadas())
    for nome, dist in reg.declared_source_names().items():
        antiga = f"{nome}{sources.FETCHER_SUFFIX}"
        if antiga != dist:
            out.setdefault(antiga, dist)
    return out


def segue_renomes(dist: str, renomes: dict[str, str]) -> str:
    """Distribuição final de ``dist`` depois de seguir os renomes em cadeia."""
    vistos = {dist}
    while dist in renomes and renomes[dist] not in vistos:
        dist = renomes[dist]
        vistos.add(dist)
    return dist


def aceita_resolucao(obtida: str, esperada: str) -> bool:
    """``obtida`` é a distribuição da tabela ou o que ela virou num renome."""
    return obtida == esperada or obtida == segue_renomes(esperada, renomes_conhecidos())
