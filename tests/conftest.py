"""Configuração global dos testes: saída sem cor.

No GitHub Actions (``GITHUB_ACTIONS``/``FORCE_COLOR``) o Typer e o Rich forçam
códigos ANSI, que partem os nomes das opções e dos textos nas asserções
(``'--raw' in '\\x1b[1m...'``). Os ``Console()`` do pacote são criados na
importação, então o ambiente é limpo aqui, antes de qualquer teste importar o
``quantilica.cli``.
"""

from __future__ import annotations

import os

import pytest

for _var in ("FORCE_COLOR", "GITHUB_ACTIONS", "PY_COLORS", "CLICOLOR_FORCE"):
    os.environ.pop(_var, None)
os.environ["NO_COLOR"] = "1"
os.environ["TERM"] = "dumb"


@pytest.fixture(autouse=True)
def _indice_publica_tudo(monkeypatch, request):
    """Os testes não consultam o índice real: por padrão ele "publica" tudo."""
    if "indice_real" in request.keywords:
        return
    from quantilica.cli import sources

    monkeypatch.setattr(sources, "index_has", lambda dist, idx: True)
