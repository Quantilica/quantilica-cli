"""Máscara de dados pessoais nas amostras do ``catalog sync``.

As amostras são linhas cruas de Parquet e saem da máquina rumo ao portal.
Antes disso, valores pessoais viram :data:`MASK`, por dois critérios:

* **nome da coluna**: a coluna inteira é mascarada quando o nome indica CPF,
  e-mail, telefone, investidor ou nome de pessoa;
* **valor**: em qualquer outra coluna, o valor de texto que contém CPF,
  e-mail ou telefone brasileiro é mascarado.

Na dúvida, mascara: mascarar demais é aceitável, deixar passar não.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

MASK = "•••"

# Tokens que dizem que uma coluna ``nome``/``nm_``/``no_`` não é nome de pessoa.
_NOT_PERSON = frozenset(
    {
        "municipio",
        "uf",
        "ue",
        "estado",
        "pais",
        "regiao",
        "cidade",
        "bairro",
        "fantasia",
        "empresa",
        "razao",
        "orgao",
        "partido",
        "coligacao",
        "federacao",
        "curso",
        "programa",
        "ies",
        "instituicao",
        "escola",
        "entidade",
        "produto",
        "procedimento",
        "aeroporto",
        "aerodromo",
        "aeronave",
        "fabricante",
        "modelo",
        "operador",
        "unidade",
        "estabelecimento",
        "fundo",
        "banco",
    }
)

# Tokens que indicam pessoa e valem mais que um sujeito neutro no mesmo nome
# (``nome_municipio_candidato`` continua mascarado).
_PERSON = frozenset(
    {
        "pessoa",
        "socio",
        "candidato",
        "vice",
        "servidor",
        "devedor",
        "titular",
        "profissional",
        "medico",
        "paciente",
        "investidor",
        "representante",
        "responsavel",
        "proprietario",
        "motorista",
        "piloto",
        "aluno",
        "docente",
        "professor",
        "funcionario",
        "empregado",
        "beneficiario",
        "favorecido",
        "mae",
        "pai",
        "cliente",
        "contribuinte",
    }
)

_NAME_TOKENS = frozenset({"nome", "nomes", "nm", "no"})
_CONTACT_SUBSTRINGS = ("cpf", "mail", "fone", "celular", "investidor")


def normalize(name: str) -> str:
    """Minúsculas, sem acento, separadores viram ``_``."""
    ascii_ = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "_", ascii_.lower()).strip("_")


def _tokens(norm: str) -> set[str]:
    tokens = set(norm.split("_"))
    # ``nomefantasia`` / ``nmcandidato``: o que vem depois do prefixo também
    # conta como token, para a exceção e o override enxergarem.
    for tok in list(tokens):
        for prefix in ("nome", "nm"):
            if tok.startswith(prefix) and len(tok) > len(prefix):
                tokens.add(prefix)
                tokens.add(tok[len(prefix) :])
    return tokens


def is_personal_column(name: str) -> bool:
    """O nome da coluna indica dado pessoal?"""
    norm = normalize(name)
    if any(s in norm for s in _CONTACT_SUBSTRINGS):  # cpf, e-mail, telefone...
        return True
    tokens = _tokens(norm)
    if "fax" in tokens:
        return True
    if not (tokens & _NAME_TOKENS) or norm.startswith("nomenclatura"):
        return False
    if tokens & _PERSON:
        return True
    return not (tokens & _NOT_PERSON)


_CPF = r"(?<![\d*])[\d*]{3}\.?[\d*]{3}\.?[\d*]{3}-?[\d*]{2}(?![\d*])"
_EMAIL = r"[^\s@]+@[^\s@]+\.[^\s@]+"
_PHONE = r"(?<![\d.,])(?:\+?55\s?)?\(?\d{2}\)?\s?9?\d{4}-?\d{4}(?!\d)(?![.,]\d)"
_VALUE_RE = re.compile(f"{_CPF}|{_EMAIL}|{_PHONE}")


def _has_personal_value(value: Any) -> bool:
    """Só texto é checado (inteiro não é texto), inclusive dentro de listas e dicts."""
    if isinstance(value, str):
        return _VALUE_RE.search(value) is not None
    if isinstance(value, dict):
        return any(_has_personal_value(v) for v in value.values())
    if isinstance(value, (list, tuple)):
        return any(_has_personal_value(v) for v in value)
    return False


def mask_personal(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    """Devolve as linhas com valores pessoais trocados por :data:`MASK`.

    O segundo item é a lista ordenada das colunas mascaradas: as mascaradas
    por nome e as que tiveram ao menos um valor mascarado. ``None`` fica
    ``None``. A lista de entrada não é alterada.
    """
    by_name = {col for row in rows for col in row if is_personal_column(col)}
    masked_cols: set[str] = set(by_name)
    out: list[dict[str, Any]] = []
    for row in rows:
        new: dict[str, Any] = {}
        for col, value in row.items():
            if value is None:
                new[col] = None
            elif col in by_name or _has_personal_value(value):
                new[col] = MASK
                masked_cols.add(col)
            else:
                new[col] = value
        out.append(new)
    return out, sorted(masked_cols)
