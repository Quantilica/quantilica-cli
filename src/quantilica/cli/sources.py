"""Gerenciamento e instalação sob demanda de fontes de dados (fetchers)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import urllib.request
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any

import typer
from rich.console import Console

app = typer.Typer(
    help="Gerenciar e instalar fontes de dados (fetchers) sob demanda.",
    no_args_is_help=True,
)
console = Console()

FETCHER_GROUP = "quantilica.fetchers"
DEFAULT_INDEX_URL = "https://index.quantilica.com/simple/"

# Mapeamento estático padrão de comandos CLI para nome das distribuições
SOURCES_REGISTRY: dict[str, str] = {
    "anac": "anac-fetcher",
    "anp": "anp-fetcher",
    "bcb-sgs": "bcb-sgs-fetcher",
    "comex": "comex-fetcher",
    "datasus": "datasus-fetcher",
    "inmet": "inmet-fetcher",
    "pdet": "pdet-fetcher",
    "rtn": "rtn-fetcher",
    "sidra": "sidra-fetcher",
    "td": "tesouro-direto-fetcher",
}


def get_config_dir() -> Path:
    """Retorna o diretório de configuração do quantilica-cli.

    Returns:
        O caminho do diretório de configuração.
    """
    custom = os.environ.get("QUANTILICA_CONFIG_DIR")
    if custom:
        return Path(custom)
    return Path.home() / ".config" / "quantilica"


def get_state_file() -> Path:
    """Retorna o caminho para o arquivo de estado local.

    Returns:
        O caminho para o arquivo de estado `state.toml`.
    """
    return get_config_dir() / "state.toml"


def load_state() -> dict[str, Any]:
    """Lê o arquivo de estado em ~/.config/quantilica/state.toml.

    Returns:
        Um dicionário contendo o estado carregado.
    """
    state_file = get_state_file()
    if not state_file.exists():
        return {"installed": {}}

    try:
        import tomllib

        with open(state_file, "rb") as f:
            return tomllib.load(f)
    except Exception as exc:
        console.print(
            f"[yellow]Aviso:[/yellow] Não foi possível ler {state_file}: {exc}"
        )
        return {"installed": {}}


def save_state(state: dict[str, Any]) -> None:
    """Salva o arquivo de estado em ~/.config/quantilica/state.toml.

    Args:
        state: O dicionário de estado a ser salvo.
    """
    config_dir = get_config_dir()
    config_dir.mkdir(parents=True, exist_ok=True)
    state_file = get_state_file()

    installed = state.get("installed", {})
    lines = ["[installed]"]
    for name, dist in sorted(installed.items()):
        lines.append(f'{name} = "{dist}"')

    state_file.write_text("\n".join(lines) + "\n", encoding="utf-8")


def get_installed_entry_points() -> set[str]:
    """Retorna o conjunto de nomes de entry points de fetchers atualmente instalados.

    Returns:
        Um conjunto de nomes de entry points.
    """
    eps = entry_points(group=FETCHER_GROUP)
    return {ep.name for ep in eps}


def get_index_url() -> str:
    """Retorna a URL do índice pip customizado ou default.

    Returns:
        A URL do índice pip.
    """
    return os.environ.get("QUANTILICA_INDEX_URL", DEFAULT_INDEX_URL)


def install_package(dist_name: str, index_url: str | None = None) -> None:
    """Instala um pacote Python usando uv pip install se disponível, ou pip.

    Args:
        dist_name: Nome do pacote a instalar.
        index_url: URL do índice pip. Padrão para a URL do índice global se não especificada.

    Raises:
        RuntimeError: Se a instalação falhar.
    """
    idx = index_url or get_index_url()

    # Prepara o ambiente garantindo que VIRTUAL_ENV aponte para o ambiente atual
    env = os.environ.copy()
    venv_dir = str(Path(sys.executable).parent.parent)
    env["VIRTUAL_ENV"] = venv_dir

    if shutil.which("uv"):
        cmd = [
            "uv",
            "pip",
            "install",
            "--python",
            sys.executable,
            "--extra-index-url",
            idx,
            dist_name,
        ]
    else:
        cmd = [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--extra-index-url",
            idx,
            dist_name,
        ]

    console.print(f"[cyan]Instalando {dist_name}...[/cyan]")
    res = subprocess.run(cmd, env=env)
    if res.returncode != 0:
        raise RuntimeError(
            f"Falha ao instalar {dist_name} (código de saída: {res.returncode})"
        )


def uninstall_package(dist_name: str) -> None:
    """Desinstala um pacote Python usando uv pip uninstall se disponível, ou pip.

    Args:
        dist_name: Nome do pacote a desinstalar.

    Raises:
        RuntimeError: Se a desinstalação falhar.
    """
    env = os.environ.copy()
    venv_dir = str(Path(sys.executable).parent.parent)
    env["VIRTUAL_ENV"] = venv_dir

    if shutil.which("uv"):
        cmd = ["uv", "pip", "uninstall", "--python", sys.executable, dist_name]
    else:
        cmd = [sys.executable, "-m", "pip", "uninstall", "-y", dist_name]

    console.print(f"[cyan]Desinstalando {dist_name}...[/cyan]")
    res = subprocess.run(cmd, env=env)
    if res.returncode != 0:
        raise RuntimeError(
            f"Falha ao desinstalar {dist_name} (código de saída: {res.returncode})"
        )


def fetch_remote_sources() -> dict[str, str]:
    """Obtém a lista remota de fontes disponíveis do arquivo sources.json do índice.

    Returns:
        Um dicionário mapeando os nomes das fontes para os nomes das distribuições.
    """
    index_url = get_index_url()
    # Converte simple/ index url para URL base de sources.json
    base_url = index_url.rstrip("/")
    if base_url.endswith("/simple"):
        base_url = base_url[:-7]
    sources_json_url = f"{base_url.rstrip('/')}/sources.json"

    try:
        req = urllib.request.Request(
            sources_json_url, headers={"User-Agent": "quantilica-cli"}
        )
        with urllib.request.urlopen(req, timeout=5) as response:
            if response.status == 200:
                data = json.loads(response.read().decode("utf-8"))
                if isinstance(data, dict):
                    return data
    except Exception:
        pass

    return SOURCES_REGISTRY.copy()


def reexec_cli(args: list[str]) -> None:
    """Recarrega o processo CLI com os novos entry points instalados.

    Args:
        args: Argumentos a serem passados para a nova execução da CLI.
    """
    if sys.platform == "win32":
        code = subprocess.call([sys.executable, "-m", "quantilica.cli.cli", *args])
        sys.exit(code)
    else:
        os.execv(sys.executable, [sys.executable, "-m", "quantilica.cli.cli", *args])


def _merged_registry() -> dict[str, str]:
    """Combina o registro remoto (sources.json) com o local; o local tem precedência."""
    return {**fetch_remote_sources(), **SOURCES_REGISTRY}


@app.command("install")
def cmd_install(
    source: str = typer.Argument(
        ..., help="Nome da fonte/fetcher a instalar (ex: comex, rtn)"
    ),
    no_exec: bool = typer.Option(
        False, "--no-exec", help="Não re-executa a CLI automaticamente após instalar"
    ),
) -> None:
    """Instala uma fonte de dados (fetcher) sob demanda."""
    registry = _merged_registry()
    dist_name = registry.get(source, source)

    installed_eps = get_installed_entry_points()
    if source in installed_eps:
        console.print(f"[yellow]A fonte '{source}' já está instalada.[/yellow]")
        return

    # Verificar permissão de escrita no ambiente
    env_dir = Path(sys.executable).parent
    if not os.access(env_dir, os.W_OK):
        console.print(
            f"[red]Erro:[/red] Sem permissão de escrita no ambiente Python ({env_dir}).\n"
            "Por favor, use um ambiente virtual (venv) ou 'uv tool install quantilica-cli'."
        )
        raise typer.Exit(1)

    try:
        install_package(dist_name)
    except Exception as exc:
        console.print(f"[red]Erro na instalação:[/red] {exc}")
        raise typer.Exit(1) from exc

    # Gravar no arquivo de estado
    state = load_state()
    installed = state.setdefault("installed", {})
    installed[source] = dist_name
    save_state(state)

    console.print(
        f"[green]✓ Fonte '{source}' ({dist_name}) instalada com sucesso![/green]"
    )

    if not no_exec:
        # Re-executar a CLI para recarregar entry points
        args = [arg for arg in sys.argv[1:] if arg != "--no-exec"]
        reexec_cli(args)


@app.command("uninstall")
def cmd_uninstall(
    source: str = typer.Argument(
        ..., help="Nome da fonte/fetcher a desinstalar (ex: comex)"
    ),
) -> None:
    """Desinstala uma fonte de dados (fetcher)."""
    registry = _merged_registry()
    dist_name = registry.get(source, source)

    try:
        uninstall_package(dist_name)
    except Exception as exc:
        console.print(f"[red]Erro na desinstalação:[/red] {exc}")
        raise typer.Exit(1) from exc

    state = load_state()
    installed = state.get("installed", {})
    if source in installed:
        del installed[source]
        save_state(state)

    console.print(f"[green]✓ Fonte '{source}' desinstalada com sucesso.[/green]")


@app.command("doctor")
def cmd_doctor() -> None:
    """Verifica inconsistências entre o estado registrado e os plugins instalados."""
    state = load_state()
    registered = state.get("installed", {})
    installed_eps = get_installed_entry_points()

    missing: dict[str, str] = {}
    for name, dist in registered.items():
        if name not in installed_eps:
            missing[name] = dist

    if not missing:
        console.print(
            "[green]✓ Todas as fontes registradas estão instaladas e ativas.[/green]"
        )
        return

    console.print(
        f"[yellow]Encontradas {len(missing)} fonte(s) registrada(s) mas ausente(s) no ambiente:[/yellow]"
    )
    for name, dist in missing.items():
        console.print(f" - {name} ({dist})")

    for name, dist in missing.items():
        console.print(f"[cyan]Reinstalando {name} ({dist})...[/cyan]")
        try:
            install_package(dist)
            console.print(f"[green]✓ {name} recuperado.[/green]")
        except Exception as exc:
            console.print(f"[red]Falha ao recuperar {name}:[/red] {exc}")
