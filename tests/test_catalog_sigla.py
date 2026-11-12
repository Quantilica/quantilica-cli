"""Sigla da fonte tirada do órgão declarado no registro."""

from __future__ import annotations

import pytest
from quantilica.core.registry import FetcherInfo

from quantilica.cli.catalog import _sigla, _source_object

# --- sigla ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("orgao", "sigla"),
    [
        ("Agência Nacional de Aviação Civil (ANAC)", "ANAC"),
        (
            "Departamento de Informática do Sistema Único de Saúde (DATASUS), "
            "Ministério da Saúde",
            "DATASUS",
        ),
        ("Secretaria do Tesouro Nacional (STN/MF)", "STN/MF"),
        ("Caixa Econômica Federal", None),
        ("Órgão (A), Outro (B)", "A"),
        ("Órgão ()", None),
        (None, None),
        ("", None),
    ],
)
def test_sigla_do_orgao(orgao: str | None, sigla: str | None) -> None:
    assert _sigla(orgao) == sigla
    if orgao:  # FetcherInfo recusa órgão vazio
        info = FetcherInfo(id="x", nome="X", orgao=orgao)
        assert _source_object("x", info, 0)["sigla"] == sigla


def test_sigla_sem_registro_e_none() -> None:
    assert _source_object("x", None, 0)["sigla"] is None
