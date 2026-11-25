"""Visão vigente do bruto para quem lê uma pasta de arquivos brutos.

O bruto guarda versões carimbadas (``nome@AAAAMMDD.ext``). Um conversor que
recebe a pasta inteira converteria todas elas. ``vigente_view`` entrega ao
conversor uma árvore só com a versão vigente de cada arquivo (a escolha é de
``quantilica.core.lake.select_current``), montada com links numa pasta
temporária e removida ao final, inclusive em erro.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path

from quantilica.core.lake import (
    MANIFEST_SIDECAR_SUFFIX,
    materialize_view,
    select_current,
)

SCRATCH_ENV = "QUANTILICA_LAKE_SCRATCH"
_TEMP_SUFFIX = ".tmp"
_VIEW_PREFIX = "quantilica-vigente-"


def _relative_files(root: Path) -> list[str]:
    rels: list[str] = []
    for current, _, names in os.walk(root):
        base = Path(current).relative_to(root)
        rels.extend((base / name).as_posix() for name in names)
    return rels


def superseded_versions(root: Path) -> list[str]:
    """Arquivos de ``root`` que não são a versão vigente do seu arquivo.

    Caminhos relativos, em ordem. Manifestos e ``*.tmp`` não contam. Vazio
    quando nenhum arquivo tem mais de uma versão.
    """
    rels = _relative_files(root)
    data = [
        rel
        for rel in rels
        if not rel.endswith(MANIFEST_SIDECAR_SUFFIX) and not rel.endswith(_TEMP_SUFFIX)
    ]
    current = set(select_current(rels))
    return sorted(rel for rel in data if rel not in current)


def scratch_base() -> Path:
    """Pasta onde a visão é montada: ``QUANTILICA_LAKE_SCRATCH`` ou o temporário
    do sistema (``TMPDIR``)."""
    configured = os.environ.get(SCRATCH_ENV)
    if configured:
        base = Path(configured)
        base.mkdir(parents=True, exist_ok=True)
        return base
    return Path(tempfile.gettempdir())


@contextlib.contextmanager
def vigente_view(root: Path) -> Iterator[tuple[Path, int]]:
    """Entrega ``(pasta, n)``: a pasta que o conversor deve ler e quantos
    arquivos de versões antigas ela deixa de fora.

    Sem nenhum arquivo com mais de uma versão devolve ``root`` e ``0``, sem
    montar nada. Caso contrário monta a visão (hardlink, depois symlink, e só
    então cópia, que é o recurso do próprio ``materialize_view``) e a remove ao
    sair, também quando o bloco levanta.
    """
    if not root.is_dir():
        yield root, 0
        return
    old = superseded_versions(root)
    if not old:
        yield root, 0
        return
    tmp = Path(tempfile.mkdtemp(prefix=_VIEW_PREFIX, dir=scratch_base()))
    try:
        view = tmp / (root.resolve().name or "bruto")
        materialize_view(root, view)
        yield view, len(old)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
