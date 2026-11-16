"""Quais fontes do lake um comando de leitura pode usar.

Um lake numa pasta sincronizada é escrito por uma máquina e lido por outra, e o
cliente de sincronização envia os arquivos em ordem qualquer. Por isso quem lê
precisa de um critério próprio de "publicado", que vem de três arquivos (escritos
pelo ``quantilica-analytics``; este módulo só os lê e não depende dele):

* ``<lake>/<fonte>/_em-atualizacao.json``: a fonte está sendo reconstruída;
* ``<lake>/<fonte>/_build.json``: manifesto do último build concluído
  (``build_id`` e o tamanho de cada Parquet);
* o arquivo de **estado**, fora do lake, gravado por
  ``quantilica lake audit --somente-leitura --estado``: por fonte, o ``build_id``
  auditado e o resultado (``publicado``, ``em_atualizacao``, ``incompleto``,
  ``falhou``).

Regras de :meth:`PoliticaDePublicacao.motivo`:

* com ``_em-atualizacao.json``, a fonte é pulada (com ou sem estado);
* sem estado, o resto segue como sempre (um lake antigo, sem ``_build.json``,
  também);
* com estado, só entra a fonte cujo resultado é ``publicado`` e cujo ``build_id``
  é o do ``_build.json`` atual (sem ``_build.json``, o estado também não pode
  citar um ``build_id``). As demais são puladas, com o motivo.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

MARCADOR = "_em-atualizacao.json"
MANIFESTO = "_build.json"
ESTADO_VERSAO = 1
ESTADO_ENV = "QUANTILICA_LAKE_ESTADO"
PUBLICADO = "publicado"


class EstadoInvalido(ValueError):
    """O arquivo de estado não existe ou não tem o formato esperado."""


def caminho_do_estado(opcao: Path | None) -> Path | None:
    """O estado pedido: ``--estado`` ou, na falta, ``QUANTILICA_LAKE_ESTADO``."""
    if opcao is not None:
        return opcao
    valor = os.environ.get(ESTADO_ENV, "").strip()
    return Path(valor).expanduser() if valor else None


def ler_estado(caminho: Path) -> dict[str, dict[str, Any]]:
    """As entradas por fonte do arquivo de estado.

    Raises:
        EstadoInvalido: não existe, não é JSON ou não é um arquivo de estado.
    """
    try:
        dados = json.loads(caminho.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise EstadoInvalido(
            f"arquivo de estado não encontrado: {caminho} "
            "(grave-o com 'quantilica lake audit --somente-leitura --estado')"
        ) from None
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise EstadoInvalido(f"não consegui ler o estado {caminho}: {exc}") from exc
    fontes = dados.get("fontes") if isinstance(dados, dict) else None
    if not isinstance(fontes, dict) or dados.get("versao") != ESTADO_VERSAO:
        raise EstadoInvalido(
            f"{caminho} não é um arquivo de estado do lake "
            f"(esperado versao={ESTADO_VERSAO} e o objeto 'fontes')"
        )
    return {k: v for k, v in fontes.items() if isinstance(v, dict)}


def _build_id_do_manifesto(pasta: Path) -> tuple[bool, str | None]:
    """``(há manifesto legível, build_id)``."""
    try:
        dados = json.loads((pasta / MANIFESTO).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return False, None
    except (OSError, UnicodeDecodeError, ValueError):
        return True, None
    build_id = dados.get("build_id") if isinstance(dados, dict) else None
    return True, build_id if isinstance(build_id, str) else None


class PoliticaDePublicacao:
    """Decide, por fonte, se um comando de leitura pode usá-la.

    ``estado`` é o conteúdo de :func:`ler_estado` ou ``None`` (sem estado).
    """

    def __init__(
        self, lake: Path, estado: dict[str, dict[str, Any]] | None = None
    ) -> None:
        self.lake = Path(lake)
        self.estado = estado
        self._cache: dict[str, str | None] = {}

    @classmethod
    def de(cls, lake: Path, opcao: Path | None) -> PoliticaDePublicacao:
        """Política com o estado de ``--estado`` ou do ambiente, se houver."""
        caminho = caminho_do_estado(opcao)
        return cls(lake, ler_estado(caminho) if caminho is not None else None)

    def motivo(self, fonte: str) -> str | None:
        """Por que ``fonte`` não deve ser lida; ``None`` se pode."""
        if fonte not in self._cache:
            self._cache[fonte] = self._decide(fonte)
        return self._cache[fonte]

    def _decide(self, fonte: str) -> str | None:
        pasta = self.lake / fonte
        if (pasta / MARCADOR).exists():
            return f"em atualização ({MARCADOR} presente)"
        if self.estado is None:
            return None
        entrada = self.estado.get(fonte)
        if entrada is None:
            return "sem registro no arquivo de estado"
        resultado = entrada.get("resultado")
        if resultado != PUBLICADO:
            return f"o estado diz '{resultado}', não '{PUBLICADO}'"
        auditado = entrada.get("build_id")
        existe, atual = _build_id_do_manifesto(pasta)
        if not existe:
            if auditado is None:
                return None  # lake anterior ao manifesto, auditado assim
            return f"o estado cita o build {auditado}, mas não há {MANIFESTO}"
        if atual is None:
            return f"{MANIFESTO} ilegível"
        if auditado != atual:
            return (
                f"o estado auditou o build {auditado}, o lake está no build {atual} "
                "(rode a auditoria de novo)"
            )
        return None

    def avisos(self, puladas: dict[str, str]) -> list[str]:
        """Uma linha de aviso por fonte pulada."""
        return [f"fonte {f} pulada: {m}" for f, m in sorted(puladas.items())]
