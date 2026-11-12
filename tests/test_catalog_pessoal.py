"""D3: amostras do catalog sync saem com dados pessoais mascarados."""

from __future__ import annotations

import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from quantilica.core.registry import FetcherInfo
from typer.testing import CliRunner

from quantilica.cli._personal import MASK, is_personal_column, mask_personal
from quantilica.cli.catalog import _sigla, _source_object, extract_catalog_payload
from quantilica.cli.cli import app

runner = CliRunner()

# Colunas pessoais vistas no dry-run do lake.
PESSOAIS = [
    "nome_socio",
    "cnpj_cpf_socio",
    "nm_candidato",
    "nr_cpf_candidato",
    "ds_email",
    "nr_cpf_vice_candidato",
    "nr_cpf_cnpj_fornecedor",
    "codigo_do_investidor",
    "CPF_CNPJ",
    "cpfcnpjtransportador",
    "cpf_cnpj",
    "cpf_prof",
    "cpfunico",
    "ap_cnpjcpf",
    "nome_devedor",
    "e_mail",
    "email",
    "E-mail",
    "telefone",
    "ddd_telefone_1",
    "ddd_telefone1",
    "fone",
    "celular",
    "fax",
    "nome",
    "Nome do Servidor",
    "NOME_SERVIDOR",
    "no_pessoa",
    "cpf",
    "CPF do titular",
]

NEUTRAS = [
    "nome_municipio",
    "nm_municipio",
    "nm_ue",
    "no_municipio",
    "nome_uf",
    "nome_estado",
    "nome_pais",
    "nome_regiao",
    "nome_cidade",
    "nome_bairro",
    "razao_social",
    "nome_fantasia",
    "nomefantasia",
    "nome_empresarial_empresa",
    "nome_orgao",
    "nm_partido",
    "nome_coligacao",
    "nm_federacao",
    "nome_curso",
    "nome_programa",
    "nome_ies",
    "nome_instituicao",
    "nome_escola",
    "nome_entidade",
    "nome_produto",
    "nome_procedimento",
    "nome_aeroporto",
    "nome_aerodromo",
    "nome_aeronave",
    "nome_fabricante",
    "nome_modelo",
    "nome_operador",
    "nome_unidade",
    "nome_estabelecimento",
    "nome_fundo",
    "nome_banco",
    "municipio_destino",
    "valor",
    "remuneracao",
    "nomenclatura",
    "uf",
    "cnpj",
    "ano",
]


@pytest.mark.parametrize("col", PESSOAIS)
def test_coluna_pessoal_e_mascarada(col: str) -> None:
    assert is_personal_column(col)
    rows, cols = mask_personal([{col: "qualquer coisa", "ano": 2024}])
    assert rows == [{col: MASK, "ano": 2024}]
    assert cols == [col]


@pytest.mark.parametrize("col", NEUTRAS)
def test_coluna_neutra_nao_e_mascarada(col: str) -> None:
    assert not is_personal_column(col)
    rows, cols = mask_personal([{col: "Valor neutro"}])
    assert rows == [{col: "Valor neutro"}]
    assert cols == []


def test_none_continua_none_e_coluna_pessoal_vazia_conta() -> None:
    rows, cols = mask_personal([{"nome_socio": None, "x": None}])
    assert rows == [{"nome_socio": None, "x": None}]
    assert cols == ["nome_socio"]


def test_pessoa_vence_sujeito_neutro() -> None:
    assert is_personal_column("nome_municipio_candidato")
    assert is_personal_column("nm_empresa_socio")


@pytest.mark.parametrize(
    "valor",
    [
        "123.456.789-09",
        "12345678909",
        "***.456.789-**",
        "123.456.***-**",
        "CPF 123.456.789-09 do titular",
        "fulano@exemplo.com.br",
        "contato: a.b+c@dominio.org",
        "(11) 98765-4321",
        "11987654321",
        "1133334444",
        "+55 11 98765-4321",
        "5511987654321",
        "(21) 3333-4444",
    ],
)
def test_valor_pessoal_em_coluna_neutra(valor: str) -> None:
    rows, cols = mask_personal([{"observacao": valor, "descricao": "ok"}])
    assert rows == [{"observacao": MASK, "descricao": "ok"}]
    assert cols == ["observacao"]


@pytest.mark.parametrize(
    "valor",
    [
        "Brasília",
        "2024-01-15",
        "2024-01-15T10:30:00",
        "12.345.678/0001-95",  # CNPJ formatado não é CPF
        "12345678000195",  # CNPJ puro (14 dígitos)
        "1234567890,50",  # valor monetário como texto
        "1700000000000",  # timestamp em ms
        "12345678",  # 8 dígitos
        "",
    ],
)
def test_falsos_positivos_obvios(valor: str) -> None:
    rows, cols = mask_personal([{"campo": valor}])
    assert rows == [{"campo": valor}]
    assert cols == []


def test_numero_inteiro_nao_e_texto() -> None:
    rows, cols = mask_personal([{"qtd": 12345678909, "tel": 11987654321, "f": 1.5}])
    assert rows == [{"qtd": 12345678909, "tel": 11987654321, "f": 1.5}]
    assert cols == []


def test_valor_aninhado_e_checado() -> None:
    rows, cols = mask_personal(
        [
            {
                "detalhe": {"contato": "x@y.com"},
                "lista": ["a", "11987654321"],
                "ok": ["a"],
            }
        ]
    )
    assert rows == [{"detalhe": MASK, "lista": MASK, "ok": ["a"]}]
    assert cols == ["detalhe", "lista"]


def test_colunas_mascaradas_ordenadas_e_unidas_entre_linhas() -> None:
    entrada = [
        {"obs": "ok", "nome": "A", "z_obs": "a@b.co"},
        {"obs": "123.456.789-09", "nome": "B", "z_obs": "ok"},
    ]
    rows, cols = mask_personal(entrada)
    assert cols == ["nome", "obs", "z_obs"]
    assert rows[0] == {"obs": "ok", "nome": MASK, "z_obs": MASK}
    assert rows[1] == {"obs": MASK, "nome": MASK, "z_obs": "ok"}
    assert entrada[0]["nome"] == "A"  # entrada intacta


# --- integração com o catalog sync -----------------------------------------

CVM = FetcherInfo(id="cvm", nome="CVM", orgao="Comissão de Valores Mobiliários (CVM)")


def _lake(tmp_path: Path) -> Path:
    d = tmp_path / "cvm" / "cadastro"
    d.mkdir(parents=True)
    (d / "_dataset.json").write_text(
        json.dumps({"source_id": "cvm", "dataset_id": "cadastro", "alias": "cadastro"}),
        encoding="utf-8",
    )
    pq.write_table(
        pa.table(
            {
                "nome": ["Fulano de Tal"],
                "nome_municipio": ["Curitiba"],
                "contato": ["fulano@x.com"],
                "valor": [10],
            }
        ),
        d / "part.parquet",
    )
    return tmp_path


def test_payload_leva_amostra_mascarada(tmp_path: Path) -> None:
    payload = extract_catalog_payload(_lake(tmp_path), registry={"cvm": CVM})
    sample = payload["samples"][0]
    assert sample["linhas_amostra"] == [
        {"nome": MASK, "nome_municipio": "Curitiba", "contato": MASK, "valor": 10}
    ]
    assert sample["colunas_mascaradas"] == ["contato", "nome"]
    assert "Fulano" not in json.dumps(payload, ensure_ascii=False)


def test_dry_run_resume_mascarados(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "quantilica.cli.catalog.load_registry", lambda *a, **k: {"cvm": CVM}
    )
    res = runner.invoke(
        app, ["catalog", "sync", "--lake-dir", str(_lake(tmp_path)), "--dry-run"]
    )
    assert res.exit_code == 0, res.output
    out = " ".join(res.output.split())
    assert "Datasets com valores mascarados" in out
    assert "cvm/cadastro: contato, nome" in out
    assert "Fulano" not in res.output
    assert "fulano@x.com" not in res.output


def test_sample_size_padrao_e_20() -> None:
    res = runner.invoke(app, ["catalog", "sync", "--help"])
    out = " ".join(res.output.split())
    assert "mascarados" in out
    import inspect

    from quantilica.cli.catalog import cmd_catalog_sync

    assert inspect.signature(cmd_catalog_sync).parameters["sample_size"].default == 20


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


@pytest.mark.parametrize(
    "coluna", ["rg", "nr_rg", "matricula", "endereco", "logradouro", "ds_endereco"]
)
def test_documento_e_endereco_mascarados(coluna: str) -> None:
    assert is_personal_column(coluna)
