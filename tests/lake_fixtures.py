"""Acervo sintético mínimo para os testes de `quantilica lake`.

Cópia reduzida de ``quantilica-analytics/tests/lake_corpus.py`` (importar de
outro repositório seria frágil). ``build_corpus(tmp_path)`` cria duas fontes
falsas com manifestos reais, a visão vigente e o lake convertido dela::

    alfa  vendas  vendas_2025@20260101.csv   superado por @20261021
                  vendas_2025@20261021.csv   -> ano=2025
                  vendas_2026@20261021.csv   -> ano=2026
    beta  precos  precos_202501@20261021.csv -> ano=2025/mes=01

Um lake limpo passa em ``quantilica lake audit --lake ... --input <visão>``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import polars as pl
from quantilica.analytics.lake import LakeWriter
from quantilica.core.lake import DatasetDescriptor, materialize_view
from quantilica.core.manifests import DownloadManifest, write_manifest_sidecar

# nome do bruto -> (fonte, pasta)
RAW: dict[str, tuple[str, str]] = {
    "vendas_2025@20260101.csv": ("alfa", "vendas"),
    "vendas_2025@20261021.csv": ("alfa", "vendas"),
    "vendas_2026@20261021.csv": ("alfa", "vendas"),
    "precos_202501@20261021.csv": ("beta", "precos"),
}

# arquivo vigente -> (fonte, dataset, partições)
CONVERT: dict[str, tuple[str, str, dict[str, str]]] = {
    "vendas_2025@20261021.csv": ("alfa", "vendas", {"ano": "2025"}),
    "vendas_2026@20261021.csv": ("alfa", "vendas", {"ano": "2026"}),
    "precos_202501@20261021.csv": ("beta", "precos", {"ano": "2025", "mes": "01"}),
}


@dataclass
class Corpus:
    root: Path
    raw: Path
    view: Path
    lake: Path

    def raw_file(self, name: str) -> Path:
        source, folder = RAW[name]
        return self.raw / source / folder / name

    def view_file(self, name: str) -> Path:
        source, folder = RAW[name]
        return self.view / source / folder / name

    def lake_parquets(self) -> list[Path]:
        return sorted(self.lake.rglob("*.parquet"))


def write_raw(raw: Path) -> None:
    """Cria o bruto (todas as versões) com um manifesto ao lado de cada arquivo."""
    for name, (source, folder) in RAW.items():
        path = raw / source / folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        # O nome entra no conteúdo: dois arquivos nunca têm o mesmo sha256.
        path.write_text(f"valor\n1\n2\n3\n# {name}\n", encoding="utf-8")
        manifest = DownloadManifest.from_file(
            source_id=source,
            dataset_id=folder,
            url=f"https://example.invalid/{name}",
            file_path=path,
        )
        write_manifest_sidecar(path, manifest)


def build_corpus(tmp_path: Path) -> Corpus:
    corpus = Corpus(
        root=tmp_path,
        raw=tmp_path / "raw",
        view=tmp_path / "view",
        lake=tmp_path / "lake",
    )
    write_raw(corpus.raw)
    materialize_view(corpus.raw, corpus.view, mode="copy")
    for name, (source, dataset, partitions) in CONVERT.items():
        writer = LakeWriter(
            corpus.lake / source / dataset,
            DatasetDescriptor(
                source_id=source,
                dataset_id=dataset,
                alias=dataset,
                partition_keys=list(partitions),
                partition_types={k: "varchar" for k in partitions},
            ),
        )
        writer.write_polars(
            pl.DataFrame({"valor": [1, 2, 3]}),
            corpus.view_file(name),
            partitions=partitions,
        )
    return corpus
