"""Perfil de colunas no `quantilica catalog sync` (Lote 3 do plano de perfis).

Sem rede: o servidor é um falso que valida o contrato do portal (perfil recusado
com 400 quando o hash não confere ou quando falta a coluna em ``columns``) e
conta ``profiles`` e ``profiles_changed`` como o portal.
"""

from __future__ import annotations

import hashlib
import io
import json
import urllib.error
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from quantilica.cli import catalog as catalog_mod
from quantilica.cli.cli import app

pytest.importorskip("pyarrow")
pytest.importorskip("quantilica.core.lake_profile")

import pyarrow as pa  # noqa: E402
import pyarrow.parquet as pq  # noqa: E402

runner = CliRunner()


@pytest.fixture(autouse=True)
def _registro(monkeypatch: pytest.MonkeyPatch) -> None:
    from quantilica.core.registry import FetcherInfo

    registro = {fid: FetcherInfo(id=fid, nome=fid.upper()) for fid in ("anp", "cvm")}
    monkeypatch.setattr(
        "quantilica.cli.catalog.load_registry", lambda *a, **k: registro
    )


# Espelho do portal (``quantilica_portal.core.catalog.perfil``): copiado, não
# importado. Se o formato do hash mudar de um lado, a paridade quebra aqui.
def _portal_canonical_json(perfil: Any) -> str:
    return json.dumps(perfil, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _portal_perfil_sha256(perfil: Any) -> str:
    return hashlib.sha256(_portal_canonical_json(perfil).encode("utf-8")).hexdigest()


# Vetor de paridade: o mesmo documento e o mesmo hash nos três lados (core, portal
# e cli). Nome com acento, para pegar um ``ensure_ascii`` diferente.
VETOR_DOC = {
    "versao": 1,
    "granularidade": "mensal",
    "periodos": ["2019-12", "2020-01", "2020-02"],
    "linhas": [3, 2, 0],
    "colunas": {
        "preço_médio": {"nulos": [None, 1, 0]},
        "n_aih": {"nulos": [0, 0, 0]},
    },
}
VETOR_JSON = (
    '{"colunas":{"n_aih":{"nulos":[0,0,0]},"preço_médio":{"nulos":[null,1,0]}},'
    '"granularidade":"mensal","linhas":[3,2,0],'
    '"periodos":["2019-12","2020-01","2020-02"],"versao":1}'
)
VETOR_SHA = "f31b62d57d2cd7fc982a66be42ff99913554b30df581d24fb57f32a775109913"


def test_paridade_do_hash_core_portal_e_vetor() -> None:
    from quantilica.core.lake_profile import canonical_json, perfil_sha256

    assert canonical_json(VETOR_DOC) == VETOR_JSON
    assert _portal_canonical_json(VETOR_DOC) == VETOR_JSON
    assert perfil_sha256(VETOR_DOC) == VETOR_SHA
    assert _portal_perfil_sha256(VETOR_DOC) == VETOR_SHA


def _escreve(base: Path, periodo: str, colunas: dict[str, list]) -> None:
    ano, mes = periodo.split("-")
    pasta = base / f"ano={ano}" / f"mes={mes}"
    pasta.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.table(colunas), pasta / "part-0.parquet")


def _dataset(lake: Path, fonte: str, dataset: str, *, com_parquet: bool = True) -> Path:
    base = lake / fonte / dataset
    base.mkdir(parents=True)
    desc = {
        "source_id": fonte,
        "dataset_id": dataset,
        "alias": dataset,
        "partition_keys": ["ano", "mes"],
        "partition_types": {"ano": "Int64", "mes": "Int64"},
    }
    (base / "_dataset.json").write_text(json.dumps(desc), encoding="utf-8")
    if com_parquet:
        # A coluna "extra" só existe de 2020-01 em diante, e "velha" só em 2019-12:
        # nenhuma está em todos os arquivos (o primeiro Parquet não tem "extra").
        _escreve(base, "2019-12", {"preco": [1.0, None, 3.0], "velha": ["a", "b", "c"]})
        _escreve(base, "2020-01", {"preco": [1.0, 2.0], "extra": [None, "x"]})
    return base


class _Resp(io.BytesIO):
    def __enter__(self) -> _Resp:
        return self

    def __exit__(self, *a: object) -> None:
        return None


class ServidorFalso:
    """Imita o que o portal faz com ``perfis`` no POST /api/v2/admin/catalog/sync."""

    def __init__(self) -> None:
        self.corpos: list[dict[str, Any]] = []
        self.perfis: dict[str, str] = {}  # dataset_id -> sha gravado
        self.colunas: dict[str, set[str]] = {}
        #: dataset_id -> motivo: o que o portal recusa só no perfil daquele dataset.
        self.recusar: dict[str, str] = {}

    def urlopen(self, req: Any, timeout: float | None = None) -> _Resp:
        corpo = json.loads(req.data.decode("utf-8"))
        self.corpos.append(corpo)
        for col in corpo.get("columns", []):
            self.colunas.setdefault(col["dataset_id"], set()).add(col["nome"])
        profiles = changed = 0
        recusados: list[dict[str, str]] = []
        vistos: set[str] = set()
        for item in corpo.get("perfis", []):
            ds = item["dataset_id"]
            motivo = None
            if ds in vistos:
                motivo = f"perfil repetido no payload: {ds}"
            elif _portal_perfil_sha256(item["perfil"]) != item["perfil_sha256"]:
                motivo = f"perfil de {ds}: perfil_sha256 não confere com o documento"
            if motivo is None:
                fantasmas = set(item["perfil"]["colunas"]) - self.colunas.get(ds, set())
                if fantasmas or ds in self.recusar:
                    # Recusa só este perfil: o sync segue (200) e a resposta o lista.
                    vistos.add(ds)
                    recusados.append(
                        {
                            "dataset_id": ds,
                            "motivo": self.recusar.get(
                                ds, "coluna sem linha em 'columns'"
                            ),
                        }
                    )
                    continue
            if motivo:
                raise urllib.error.HTTPError(
                    req.full_url,
                    400,
                    "Bad Request",
                    {},  # type: ignore[arg-type]
                    io.BytesIO(json.dumps({"detail": motivo}).encode()),
                )
            vistos.add(ds)
            profiles += 1
            if self.perfis.get(ds) != item["perfil_sha256"]:
                self.perfis[ds] = item["perfil_sha256"]
                changed += 1
        resposta = {
            "status": "success",
            "counts": {
                "profiles": profiles,
                "profiles_changed": changed,
                "profiles_refused": len(recusados),
            },
            "profiles_refused": recusados,
        }
        return _Resp(json.dumps(resposta).encode())


def _sync(lake: Path, *extra: str):
    return runner.invoke(
        app,
        [
            "catalog",
            "sync",
            "--lake-dir",
            str(lake),
            "--portal-url",
            "https://portal.test",
            "--api-key",
            "k",
            *extra,
        ],
    )


def _junta(texto: str) -> str:
    return " ".join(texto.split())


def test_payload_traz_perfis_e_colunas_da_uniao(tmp_path: Path) -> None:
    from quantilica.core.lake_profile import perfil_sha256, profile_from_lake

    _dataset(tmp_path, "anp", "precos")
    build = catalog_mod.build_catalog(tmp_path)
    (item,) = build.payload["perfis"]
    esperado = profile_from_lake(tmp_path, "anp", "precos")

    assert item["dataset_id"] == "anp/precos"
    assert item["perfil"] == esperado.perfil
    assert item["perfil_sha256"] == esperado.perfil_sha256
    # Paridade: o hash enviado é o do core e o do espelho do portal.
    assert item["perfil_sha256"] == perfil_sha256(item["perfil"])
    assert item["perfil_sha256"] == _portal_perfil_sha256(item["perfil"])

    cols = {c["nome"]: c for c in build.payload["columns"]}
    # Toda coluna do perfil tem linha em columns, inclusive as que não estão no
    # primeiro Parquet; e as de partição seguem.
    assert set(item["perfil"]["colunas"]) <= set(cols)
    assert {"preco", "velha", "extra", "ano", "mes"} <= set(cols)
    assert cols["extra"]["dtype_arrow"] == "string"
    # O portal calcula esses campos; o cli não os manda mais.
    for c in build.payload["columns"]:
        assert not {"percentual_nulos", "periodo_inicio", "periodo_fim"} & set(c)
    assert len({c["display_order"] for c in build.payload["columns"]}) == len(cols)


def test_primeiro_e_segundo_sync_identicos(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _dataset(tmp_path, "anp", "precos")
    _dataset(tmp_path, "cvm", "fundos")
    srv = ServidorFalso()
    monkeypatch.setattr("urllib.request.urlopen", srv.urlopen)

    r1 = _sync(tmp_path)
    assert r1.exit_code == 0, r1.output
    assert "2 recebidos, 2 alterados" in _junta(r1.output)

    r2 = _sync(tmp_path)
    assert r2.exit_code == 0, r2.output
    assert "2 recebidos, 0 alterados" in _junta(r2.output)
    assert srv.corpos[0] == srv.corpos[1]


def test_perfil_recusado_pelo_portal_avisa_com_dataset_e_motivo_e_sai_com_0(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _dataset(tmp_path, "anp", "precos")
    _dataset(tmp_path, "cvm", "fundos")
    srv = ServidorFalso()
    srv.recusar["cvm/fundos"] = "inválido: linhas: 'linhas' deve ter [1, 2] valores"
    monkeypatch.setattr("urllib.request.urlopen", srv.urlopen)

    res = _sync(tmp_path)
    assert res.exit_code == 0, res.output  # o catálogo foi gravado
    saida = _junta(res.output)
    assert "1 recebidos, 1 alterados" in saida
    assert "o portal recusou 1 perfil(is) de colunas" in saida
    # dataset e motivo aparecem, com colchetes do motivo intactos (não são markup)
    assert "Perfil recusado: cvm/fundos: inválido: linhas:" in saida
    assert "deve ter [1, 2] valores" in saida
    assert "anp/precos: " not in saida.split("Perfil recusado")[-1]


def test_sem_recusa_nao_ha_aviso(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _dataset(tmp_path, "anp", "precos")
    monkeypatch.setattr("urllib.request.urlopen", ServidorFalso().urlopen)
    res = _sync(tmp_path)
    assert res.exit_code == 0, res.output
    assert "recusou" not in _junta(res.output)


def test_somar_respostas_junta_as_listas_de_perfis_recusados() -> None:
    soma = catalog_mod._somar_respostas(
        [
            {
                "counts": {"profiles": 1, "profiles_refused": 1},
                "profiles_refused": [{"dataset_id": "a/x", "motivo": "m1"}],
            },
            {
                "counts": {"profiles": 2, "profiles_refused": 1},
                "profiles_refused": [{"dataset_id": "b/y", "motivo": "m2"}],
            },
        ]
    )
    assert soma["counts"] == {"profiles": 3, "profiles_refused": 2}
    assert [r["dataset_id"] for r in soma["profiles_refused"]] == ["a/x", "b/y"]


def test_dataset_sem_parquet_pula_com_aviso(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _dataset(tmp_path, "anp", "precos")
    _dataset(tmp_path, "cvm", "vazio", com_parquet=False)
    srv = ServidorFalso()
    monkeypatch.setattr("urllib.request.urlopen", srv.urlopen)

    res = _sync(tmp_path)
    assert res.exit_code == 0, res.output
    saida = _junta(res.output)
    assert "Sem perfil de colunas: cvm/vazio (sem arquivo Parquet)" in saida
    (corpo,) = srv.corpos
    assert [p["dataset_id"] for p in corpo["perfis"]] == ["anp/precos"]
    # O dataset sem Parquet continua no catálogo.
    assert {d["id"] for d in corpo["datasets"]} == {"anp/precos", "cvm/vazio"}
    assert "1 recebidos, 1 alterados" in saida


def test_dataset_sem_particao_ano_pula_com_motivo(tmp_path: Path) -> None:
    base = _dataset(tmp_path, "anp", "solto", com_parquet=False)
    pq.write_table(pa.table({"a": [1]}), base / "solto.parquet")
    build = catalog_mod.build_catalog(tmp_path)
    assert build.payload["perfis"] == []
    assert "ano=" in build.sem_perfil["anp/solto"]


def test_erro_400_mostra_o_dataset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _dataset(tmp_path, "anp", "precos")

    def recusa(req: Any, timeout: float | None = None) -> _Resp:
        detalhe = {
            "detail": "perfil de anp/precos inválido: periodos: períodos devem "
            "ser crescentes"
        }
        raise urllib.error.HTTPError(
            req.full_url,
            400,
            "Bad Request",
            {},  # type: ignore[arg-type]
            io.BytesIO(json.dumps(detalhe).encode()),
        )

    monkeypatch.setattr("urllib.request.urlopen", recusa)
    res = _sync(tmp_path)
    assert res.exit_code == 1
    saida = _junta(res.output)
    assert "HTTP 400" in saida
    assert "perfil de anp/precos inválido" in saida


def test_dry_run_resume_cada_perfil(tmp_path: Path) -> None:
    _dataset(tmp_path, "anp", "precos")
    res = runner.invoke(
        app, ["catalog", "sync", "--lake-dir", str(tmp_path), "--dry-run"]
    )
    assert res.exit_code == 0, res.output
    saida = _junta(res.output)
    assert "Perfis de colunas (--dry-run)" in saida
    assert "anp/precos" in saida
    assert "2019-12 a 2020-01 (2 meses)" in saida
    # preco, velha, extra: 3 colunas; eras: [velha, preco], [preco, extra].
    assert "KiB" in saida
    linha = next(x for x in res.output.splitlines() if "2 meses" in x)
    celulas = [c.strip() for c in linha.strip("│ ").split("│")]
    assert celulas[0] == "anp/precos"
    assert celulas[2:4] == ["3", "2"]
    # O JSON do payload não repete o perfil inteiro.
    assert '"perfis"' not in res.output
    assert "nulos" not in res.output


def test_core_sem_lake_profile_avisa_e_segue_sem_perfis(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _dataset(tmp_path, "anp", "precos")
    monkeypatch.setattr(catalog_mod, "_lake_profile_module", lambda: None)
    srv = ServidorFalso()
    monkeypatch.setattr("urllib.request.urlopen", srv.urlopen)

    res = _sync(tmp_path)
    assert res.exit_code == 0, res.output
    assert "não tem lake_profile" in _junta(res.output)
    (corpo,) = srv.corpos
    assert corpo["perfis"] == []
    assert [d["id"] for d in corpo["datasets"]] == ["anp/precos"]
    assert {c["nome"] for c in corpo["columns"]} >= {"preco", "velha"}


def test_coluna_de_nome_invalido_sai_do_perfil_e_o_hash_acompanha(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = _dataset(tmp_path, "anp", "precos", com_parquet=False)
    _escreve(base, "2020-01", {"preco": [1.0, 2.0], "": ["a", "b"]})
    _escreve(base, "2020-02", {"preco": [1.0], "": ["a"], " espaco ": ["z"]})

    build = catalog_mod.build_catalog(tmp_path)
    (item,) = build.payload["perfis"]
    assert set(item["perfil"]["colunas"]) == {"preco"}
    assert item["perfil_sha256"] == _portal_perfil_sha256(item["perfil"])
    srv = ServidorFalso()
    monkeypatch.setattr("urllib.request.urlopen", srv.urlopen)
    res = _sync(tmp_path)
    assert res.exit_code == 0, res.output


def test_perfil_viaja_no_mesmo_lote_do_dataset() -> None:
    def ds(i: str, fonte: str) -> dict[str, Any]:
        return {"id": f"{fonte}/{i}", "source_id": fonte}

    payload: dict[str, Any] = {
        "sources": [{"id": "a"}, {"id": "b"}],
        "datasets": [ds("x", "a"), ds("y", "b")],
        "columns": [
            {"dataset_id": "a/x", "nome": "c", "dtype_arrow": "string"},
            {"dataset_id": "b/y", "nome": "c", "dtype_arrow": "string"},
        ],
        "samples": [],
        "perfis": [
            {"dataset_id": "a/x", "perfil": {"k": "z" * 500}, "perfil_sha256": "h"},
            {"dataset_id": "b/y", "perfil": {"k": "z" * 500}, "perfil_sha256": "h"},
        ],
    }
    lotes = catalog_mod.split_catalog_payload(payload, max_bytes=900)
    assert len(lotes) == 2
    for lote in lotes:
        ids = {d["id"] for d in lote["datasets"]}
        assert {p["dataset_id"] for p in lote["perfis"]} == ids
        assert {c["dataset_id"] for c in lote["columns"]} == ids


def test_send_catalog_payload_corpo_de_erro_fora_de_json_segue_cru(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def recusa(req: Any, timeout: float | None = None) -> _Resp:
        raise urllib.error.HTTPError(
            req.full_url,
            502,
            "Bad Gateway",
            {},
            io.BytesIO(b"<html>falha</html>"),  # type: ignore[arg-type]
        )

    monkeypatch.setattr("urllib.request.urlopen", recusa)
    with pytest.raises(RuntimeError, match="HTTP 502.*<html>falha</html>"):
        catalog_mod.send_catalog_payload("https://portal.test", {"sources": []})
