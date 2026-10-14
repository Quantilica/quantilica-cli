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
        assert remote == SOURCES_REGISTRY


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
