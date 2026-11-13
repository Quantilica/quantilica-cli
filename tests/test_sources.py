"""Testes unitários para a gestão e instalação de fontes (quantilica-cli.sources)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from quantilica.cli.sources import (
    SOURCES_REGISTRY,
    fetch_remote_sources,
    load_state,
    save_state,
)


@pytest.fixture
def temp_config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_dir = tmp_path / "config" / "quantilica"
    monkeypatch.setenv("QUANTILICA_CONFIG_DIR", str(config_dir))
    return config_dir


def test_load_save_state(temp_config_dir: Path):
    state = load_state()
    assert state == {"installed": {}}

    state_to_save = {"installed": {"comex": "comex-fetcher", "rtn": "rtn-fetcher"}}
    save_state(state_to_save)

    loaded = load_state()
    assert loaded["installed"]["comex"] == "comex-fetcher"
    assert loaded["installed"]["rtn"] == "rtn-fetcher"


def test_fetch_remote_sources_fallback():
    with patch("urllib.request.urlopen", side_effect=Exception("Network error")):
        remote = fetch_remote_sources()
        # Sem o índice: a tabela manual mais o que estiver instalado.
        assert SOURCES_REGISTRY.items() <= remote.items()


def test_list_remote_avisa_quando_o_indice_nao_responde():
    from typer.testing import CliRunner

    from quantilica.cli.cli import app

    with patch("urllib.request.urlopen", side_effect=Exception("Network error")):
        result = CliRunner().invoke(app, ["list-sources", "--remote"])
    assert result.exit_code == 0
    assert "índice não respondeu" in " ".join(result.output.split())


def test_fetch_remote_sources_success():
    fake_json = json.dumps({"custom": "custom-fetcher"}).encode("utf-8")
    mock_response = MagicMock()
    mock_response.status = 200
    mock_response.read.return_value = fake_json
    mock_response.__enter__.return_value = mock_response

    with patch("urllib.request.urlopen", return_value=mock_response):
        remote = fetch_remote_sources()
        assert remote == {"custom": "custom-fetcher"}


def test_cmd_install_already_installed(temp_config_dir: Path):
    from quantilica.cli.cli import app

    runner = CliRunner()

    with patch(
        "quantilica.cli.sources.get_installed_entry_points", return_value={"comex"}
    ):
        result = runner.invoke(app, ["install", "comex", "--no-exec"])
        assert result.exit_code == 0
        assert "já está instalada" in result.output


def test_cmd_install_success(temp_config_dir: Path):
    from quantilica.cli.cli import app

    runner = CliRunner()

    with (
        patch("quantilica.cli.sources.get_installed_entry_points", return_value=set()),
        patch("quantilica.cli.sources.install_package") as mock_install,
    ):
        result = runner.invoke(app, ["install", "comex", "--no-exec"])
        assert result.exit_code == 0
        assert "instalada com sucesso" in result.output
        mock_install.assert_called_once_with("comex-fetcher")

        state = load_state()
        assert state["installed"].get("comex") == "comex-fetcher"


def test_cmd_uninstall_success(temp_config_dir: Path):
    from quantilica.cli.cli import app

    save_state({"installed": {"comex": "comex-fetcher"}})

    runner = CliRunner()

    with patch("quantilica.cli.sources.uninstall_package") as mock_uninstall:
        result = runner.invoke(app, ["uninstall", "comex"])
        assert result.exit_code == 0
        assert "desinstalada com sucesso" in result.output
        mock_uninstall.assert_called_once_with("comex-fetcher")

        state = load_state()
        assert "comex" not in state.get("installed", {})


def test_cmd_doctor(temp_config_dir: Path):
    from quantilica.cli.cli import app

    save_state({"installed": {"comex": "comex-fetcher", "rtn": "rtn-fetcher"}})

    runner = CliRunner()

    # comex está instalado, rtn está ausente
    with (
        patch(
            "quantilica.cli.sources.get_installed_entry_points", return_value={"comex"}
        ),
        patch("quantilica.cli.sources.install_package") as mock_install,
    ):
        result = runner.invoke(app, ["doctor"])
        assert result.exit_code == 0
        assert "rtn (rtn-fetcher)" in result.output
        mock_install.assert_called_once_with("rtn-fetcher")


def test_cmd_doctor_lists_lake_converters(temp_config_dir: Path):
    from quantilica.cli.cli import app

    save_state({"installed": {"comex": "comex-fetcher", "rtn": "rtn-fetcher"}})
    with (
        patch(
            "quantilica.cli.sources.get_installed_entry_points",
            return_value={"comex", "rtn"},
        ),
        patch(
            "quantilica.cli.sources.get_lake_entry_points",
            return_value=[("comex", "comex-fetcher")],
        ),
    ):
        result = CliRunner().invoke(app, ["doctor"])
    assert result.exit_code == 0
    assert "quantilica.lake" in result.output
    assert "comex: ✓ lake: sim" in result.output
    assert "rtn: lake: não" in result.output


def test_has_lake_converter_matches_by_name_or_dist():
    from quantilica.cli.sources import has_lake_converter

    eps = [("pdet", "pdet-fetcher")]
    assert has_lake_converter("pdet", eps)
    assert has_lake_converter("outro", eps, "pdet-fetcher")
    assert not has_lake_converter("outro", eps, "x-fetcher")
    assert not has_lake_converter("outro", [])


def test_dist_spec_monta_extras():
    from quantilica.cli.sources import _dist_spec

    assert _dist_spec("pdet-fetcher", []) == "pdet-fetcher"
    assert _dist_spec("pdet-fetcher", ["analysis"]) == "pdet-fetcher[analysis]"
    assert _dist_spec("x", ["a,b", "a", " c "]) == "x[a,b,c]"


def test_dist_spec_rejeita_extra_invalido():
    import typer

    from quantilica.cli.sources import _dist_spec

    for ruim in ["a]b", "--index", "a b", "x;rm"]:
        with pytest.raises(typer.BadParameter):
            _dist_spec("x", [ruim])


def test_cmd_install_com_extra(temp_config_dir: Path):
    from quantilica.cli.cli import app

    runner = CliRunner()

    with (
        patch("quantilica.cli.sources.get_installed_entry_points", return_value=set()),
        patch("quantilica.cli.sources.install_package") as mock_install,
    ):
        result = runner.invoke(
            app, ["install", "comex", "--extra", "analysis", "--no-exec"]
        )
        assert result.exit_code == 0, result.output
        mock_install.assert_called_once_with("comex-fetcher[analysis]")
        assert "comex-fetcher[analysis]" in " ".join(result.output.split())
        # o estado registra só a distribuição, sem extras
        assert load_state()["installed"].get("comex") == "comex-fetcher"


def test_cmd_install_extra_em_fonte_ja_instalada_instala_o_extra(temp_config_dir: Path):
    """`install pdet --extra analysis` depois de `install pdet` deve instalar o extra."""
    from quantilica.cli.cli import app

    runner = CliRunner()

    with (
        patch(
            "quantilica.cli.sources.get_installed_entry_points", return_value={"comex"}
        ),
        patch("quantilica.cli.sources.install_package") as mock_install,
    ):
        result = runner.invoke(app, ["install", "comex", "-e", "analysis", "--no-exec"])
        assert result.exit_code == 0, result.output
        mock_install.assert_called_once_with("comex-fetcher[analysis]")


def test_cmd_install_extra_invalido_sai_com_codigo_2(temp_config_dir: Path):
    from quantilica.cli.cli import app

    runner = CliRunner()

    with patch("quantilica.cli.sources.install_package") as mock_install:
        result = runner.invoke(app, ["install", "comex", "--extra", "a]b", "--no-exec"])
        assert result.exit_code == 2
        mock_install.assert_not_called()


def test_cmd_install_reexec_nao_repete_extra(temp_config_dir: Path):
    """O re-exec não pode carregar --extra, senão reinstala e reexecuta em laço."""
    from quantilica.cli.cli import app

    runner = CliRunner()

    with (
        patch("quantilica.cli.sources.get_installed_entry_points", return_value=set()),
        patch("quantilica.cli.sources.install_package"),
        patch("quantilica.cli.sources.reexec_cli") as mock_reexec,
    ):
        result = runner.invoke(app, ["install", "comex", "--extra", "analysis"])
        assert result.exit_code == 0, result.output
        mock_reexec.assert_called_once_with(["install", "comex"])


def test_install_package_imprime_extra_sem_engolir_colchete(capsys):
    """O Rich trata ``[analysis]`` como marcação; a mensagem deve mostrá-lo."""
    from quantilica.cli import sources

    cp = MagicMock(returncode=0)
    with patch("quantilica.cli.sources.subprocess.run", return_value=cp):
        sources.install_package("pdet-fetcher[analysis]", index_url="https://i/simple/")
    assert "pdet-fetcher[analysis]" in " ".join(capsys.readouterr().out.split())


def test_install_package_uv_prioriza_indice_da_quantilica():
    from quantilica.cli import sources

    cp = MagicMock(returncode=0)
    with (
        patch("quantilica.cli.sources.shutil.which", return_value="/usr/bin/uv"),
        patch("quantilica.cli.sources.subprocess.run", return_value=cp) as run,
    ):
        sources.install_package("anp-fetcher", index_url="https://q/simple/")
    cmd = run.call_args.args[0]
    assert cmd[cmd.index("--index") + 1] == "https://q/simple/"
    assert cmd[cmd.index("--default-index") + 1] == sources.PYPI_INDEX_URL
    assert cmd[cmd.index("--index-strategy") + 1] == "first-index"
    assert "--extra-index-url" not in cmd and "--index-url" not in cmd


def test_install_package_erro_cita_nome_digitado_e_expandido():
    from quantilica.cli import sources

    cp = MagicMock(returncode=1)
    with patch("quantilica.cli.sources.subprocess.run", return_value=cp):
        with pytest.raises(RuntimeError) as exc:
            sources.install_package(
                "foo-fetcher", index_url="https://q/simple/", requested="foo"
            )
    assert "'foo' foi lido como 'foo-fetcher'" in str(exc.value)


@pytest.mark.indice_real
def test_index_has_normaliza_o_nome_e_falha_fechado():
    from quantilica.cli import sources

    vistos = []

    class _Resp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def _abre(req, timeout=0):
        vistos.append(req.full_url)
        return _Resp()

    with patch("urllib.request.urlopen", side_effect=_abre):
        assert sources.index_has(
            "Foo_Fetcher[analysis]>=1.0", "https://i.example/simple/"
        )
    assert vistos == ["https://i.example/simple/foo-fetcher/"]

    with patch("urllib.request.urlopen", side_effect=Exception("404")):
        assert not sources.index_has("foo-fetcher", "https://i.example/simple/")


def test_install_recusa_o_que_o_indice_nao_publica(monkeypatch):
    from quantilica.cli import sources

    monkeypatch.setattr(sources, "index_has", lambda dist, idx: False)
    monkeypatch.setattr(sources, "_index_lookup", lambda dist, idx: False)
    with patch("subprocess.run") as run, pytest.raises(RuntimeError) as exc:
        sources.install_package("foo-fetcher", requested="foo")
    run.assert_not_called()
    assert "não foi encontrado no índice" in str(exc.value)
    assert "'foo' foi lido como 'foo-fetcher'" in str(exc.value)


@pytest.mark.indice_real
def test_install_distingue_indice_fora_do_ar():
    from quantilica.cli import sources

    with (
        patch("urllib.request.urlopen", side_effect=OSError("sem rede")),
        patch("subprocess.run") as run,
        pytest.raises(RuntimeError) as exc,
    ):
        sources.install_package("anp-fetcher")
    run.assert_not_called()
    assert "não respondeu" in str(exc.value)


# --- Laço de instalação e distribuições renomeadas -------------------------


class _AmbienteFalso:
    """Ambiente simulado: dist instalada -> entry point e nomes declarados."""

    def __init__(self, dists: dict[str, tuple[set[str], set[str]]] | None = None):
        # dist -> (nomes de entry point, ids/apelidos declarados via FetcherInfo)
        self.dists: dict[str, tuple[set[str], set[str]]] = dict(dists or {})
        self.installs: list[str] = []
        self.uninstalls: list[str] = []

    def entry_points(self) -> set[str]:
        return {ep for eps, _ in self.dists.values() for ep in eps}

    def declared(self) -> dict[str, str]:
        return {n: d for d, (_, names) in self.dists.items() for n in names}

    def install(self, spec: str) -> None:
        dist = spec.split("[")[0]
        self.installs.append(dist)
        self.dists[dist] = NOVAS[dist]

    def uninstall(self, dist: str) -> None:
        self.uninstalls.append(dist)
        self.dists.pop(dist, None)


# O que cada distribuição passa a oferecer depois de instalada.
NOVAS: dict[str, tuple[set[str], set[str]]] = {
    "ibge-sidra-fetcher": ({"ibge-sidra"}, {"ibge-sidra", "sidra", "ibge"}),
    "bcb-sgs-fetcher": ({"bcb-sgs"}, {"bcb-sgs", "bcb"}),
    "rfb-cnpj-fetcher": ({"rfb-cnpj"}, {"rfb-cnpj", "rfb"}),
    "tddata-fetcher": ({"tddata"}, set()),  # sem FetcherInfo declarado
}


@pytest.fixture
def ambiente(monkeypatch: pytest.MonkeyPatch, temp_config_dir: Path):
    """Troca a descoberta e a instalação por um ambiente simulado."""
    from quantilica.cli import sources

    amb = _AmbienteFalso()
    monkeypatch.setattr(sources, "get_installed_entry_points", amb.entry_points)
    monkeypatch.setattr(sources, "installed_declared_names", amb.declared)
    monkeypatch.setattr(sources, "is_dist_installed", lambda d: d in amb.dists)
    monkeypatch.setattr(sources, "install_package", amb.install)
    monkeypatch.setattr(sources, "uninstall_package", amb.uninstall)
    monkeypatch.setattr(sources, "fetch_remote_sources", lambda: {})
    monkeypatch.setattr(sources.os, "access", lambda *a, **k: True)  # ambiente gravável
    return amb


def _instalar_com_reexec(source: str, limite: int = 5) -> int:
    """Roda ``install`` e re-executa a CLI como o processo real faria.

    Devolve quantas execuções houve; estoura se passar do ``limite`` (laço).
    """
    from quantilica.cli import sources
    from quantilica.cli.cli import app

    chamadas: list[list[str]] = []

    def reexec(args: list[str]) -> None:
        chamadas.append(args)
        assert len(chamadas) <= limite, "laço de reinstalação sem fim"
        CliRunner().invoke(app, args)

    with patch.object(sources, "reexec_cli", reexec):
        result = CliRunner().invoke(app, ["install", source])
    assert result.exit_code == 0, result.output
    return 1 + len(chamadas)


@pytest.mark.parametrize(
    ("source", "dist"),
    [
        ("sidra", "ibge-sidra-fetcher"),
        ("ibge", "ibge-sidra-fetcher"),
        ("ibge-sidra", "ibge-sidra-fetcher"),
        ("bcb", "bcb-sgs-fetcher"),
        ("rfb", "rfb-cnpj-fetcher"),
        ("tddata", "tddata-fetcher"),
    ],
)
def test_install_com_apelido_termina_sem_laco(ambiente, source: str, dist: str):
    """O apelido não é o entry point (que tem o id): a 2ª passada não reinstala."""
    execucoes = _instalar_com_reexec(source)

    assert ambiente.installs == [dist]
    assert execucoes == 2  # instala + uma passada que vê "já instalada"
    assert load_state()["installed"][source] == dist


def test_install_dist_instalada_sem_apelido_declarado_termina(ambiente):
    """Fetcher sem FetcherInfo e com id diferente do nome: a dist instalada basta."""
    ambiente.dists["bcb-sgs-fetcher"] = ({"bcb-sgs"}, set())
    result = CliRunner().invoke(_app(), ["install", "bcb", "--no-exec"])
    assert result.exit_code == 0
    assert "já está instalada" in result.output
    assert ambiente.installs == []


def _app():
    from quantilica.cli.cli import app

    return app


def test_distribuicoes_renomeadas_declaradas_num_so_lugar():
    from quantilica.cli.sources import DISTRIBUICOES_RENOMEADAS

    assert DISTRIBUICOES_RENOMEADAS == {"sidra-fetcher": "ibge-sidra-fetcher"}


def test_install_migra_sidra_fetcher_antigo(ambiente):
    """sidra-fetcher 0.11.0 (entry point `sidra`, sem info) cede lugar à nova."""
    ambiente.dists["sidra-fetcher"] = ({"sidra"}, set())
    save_state({"installed": {"sidra": "sidra-fetcher"}})

    execucoes = _instalar_com_reexec("sidra")

    assert ambiente.installs == ["ibge-sidra-fetcher"]
    assert ambiente.uninstalls == ["sidra-fetcher"]
    assert "sidra-fetcher" not in ambiente.dists
    assert "ibge-sidra-fetcher" in ambiente.dists
    assert load_state()["installed"] == {"sidra": "ibge-sidra-fetcher"}
    assert execucoes == 2


def test_install_antiga_instalada_nao_diz_ja_instalada(ambiente):
    ambiente.dists["sidra-fetcher"] = ({"sidra"}, set())
    result = CliRunner().invoke(_app(), ["install", "sidra", "--no-exec"])
    assert result.exit_code == 0
    assert "já está instalada" not in result.output
    assert ambiente.installs == ["ibge-sidra-fetcher"]


def test_install_falha_na_nova_mantem_a_antiga(ambiente, monkeypatch):
    from quantilica.cli import sources

    ambiente.dists["sidra-fetcher"] = ({"sidra"}, set())

    def falha(spec: str) -> None:
        raise RuntimeError("índice fora do ar")

    monkeypatch.setattr(sources, "install_package", falha)
    result = CliRunner().invoke(_app(), ["install", "sidra", "--no-exec"])
    assert result.exit_code == 1
    assert "sidra-fetcher" in ambiente.dists
    assert ambiente.uninstalls == []


def test_uninstall_remove_a_antiga_se_for_ela_a_instalada(ambiente):
    ambiente.dists["sidra-fetcher"] = ({"sidra"}, set())
    save_state({"installed": {"sidra": "sidra-fetcher"}})

    result = CliRunner().invoke(_app(), ["uninstall", "sidra"])

    assert result.exit_code == 0
    assert ambiente.uninstalls == ["sidra-fetcher"]
    assert load_state()["installed"] == {}


def test_uninstall_remove_a_nova_quando_e_ela_a_instalada(ambiente):
    ambiente.dists["ibge-sidra-fetcher"] = NOVAS["ibge-sidra-fetcher"]
    save_state({"installed": {"ibge": "ibge-sidra-fetcher"}})

    result = CliRunner().invoke(_app(), ["uninstall", "ibge"])

    assert result.exit_code == 0
    assert ambiente.uninstalls == ["ibge-sidra-fetcher"]
    assert load_state()["installed"] == {}


def test_doctor_traduz_estado_antigo_em_vez_de_reinstalar_a_antiga(ambiente):
    """`sidra = "sidra-fetcher"` no estado e nada instalado: vai a nova."""
    save_state({"installed": {"sidra": "sidra-fetcher"}})

    result = CliRunner().invoke(_app(), ["doctor"])

    assert result.exit_code == 0
    assert ambiente.installs == ["ibge-sidra-fetcher"]
    assert "sidra-fetcher" not in ambiente.dists
    assert load_state()["installed"] == {"sidra": "ibge-sidra-fetcher"}


def test_doctor_migra_a_antiga_ainda_instalada(ambiente):
    ambiente.dists["sidra-fetcher"] = ({"sidra"}, set())
    save_state({"installed": {"sidra": "sidra-fetcher"}})

    result = CliRunner().invoke(_app(), ["doctor"])

    assert result.exit_code == 0
    assert ambiente.installs == ["ibge-sidra-fetcher"]
    assert ambiente.uninstalls == ["sidra-fetcher"]
    assert load_state()["installed"] == {"sidra": "ibge-sidra-fetcher"}


def test_doctor_apelido_instalado_nao_reinstala(ambiente):
    """`sidra` no estado e o fetcher novo instalado (entry point `ibge-sidra`)."""
    ambiente.dists["ibge-sidra-fetcher"] = NOVAS["ibge-sidra-fetcher"]
    save_state({"installed": {"sidra": "ibge-sidra-fetcher"}})

    result = CliRunner().invoke(_app(), ["doctor"])

    assert result.exit_code == 0
    assert ambiente.installs == []
    assert "instaladas e ativas" in result.output
