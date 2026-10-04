"""Guardas de sanidade do pacote `quantilica.cli` (fontes, não testes de CLI)."""

from __future__ import annotations

import warnings
from pathlib import Path

import quantilica.cli


def test_fontes_sem_invalid_escape_sequence() -> None:
    """Nenhum módulo pode emitir ``SyntaxWarning`` na importação.

    Um escape inválido em f-string (``"\\["`` fora de raw string) passa
    silenciosamente pelo ruff e pelo import, mas polui a saída de **todo**
    ``quantilica <fonte> --help`` com três linhas de warning — e vira
    ``SyntaxError`` em uma versão futura do Python.
    """
    raiz = Path(quantilica.cli.__file__).parent
    com_warning: list[str] = []
    for caminho in sorted(raiz.rglob("*.py")):
        fonte = caminho.read_text(encoding="utf-8")
        with warnings.catch_warnings():
            warnings.simplefilter("error", SyntaxWarning)
            try:
                compile(fonte, str(caminho), "exec")
            except SyntaxWarning as exc:  # pragma: no cover - só em regressão
                com_warning.append(f"{caminho.name}: {exc.messages[0]}")
    assert not com_warning, "SyntaxWarning ao compilar: " + "; ".join(com_warning)
