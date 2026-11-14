"""Gerenciamento e instalação sob demanda de fontes de dados (fetchers)."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from importlib.metadata import PackageNotFoundError, distribution, entry_points
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.markup import escape

app = typer.Typer(
    help="Gerenciar e instalar fontes de dados (fetchers) sob demanda.",
    no_args_is_help=True,
)
console = Console()

FETCHER_GROUP = "quantilica.fetchers"
LAKE_GROUP = "quantilica.lake"
DEFAULT_INDEX_URL = "https://index.quantilica.com/simple/"
PYPI_INDEX_URL = "https://pypi.org/simple"

# Reserva offline: a tabela manual congelada. Só vale quando o fetcher não está
# instalado e o registro público (``registro_publico``) não responde nem tem
# cache. NÃO recebe entradas novas: id, apelidos e renomes de fetcher novo vivem
# no registro público (``quantilica-index/registro.toml``) e nos ``FetcherInfo``
# dos pacotes. Os fetchers em si não são listados aqui: os instalados vêm dos
# entry points e os ausentes saem da regra em ``resolve_dist``.
_RESERVA_OFFLINE: dict[str, str] = {
    # Pacote que não é fetcher (a distribuição não termina em ``-fetcher``).
    "sql": "quantilica-sql",
    # Apelidos de nomes antigos que levam a outro id; ficam para o caso de o
    # fetcher ainda não estar instalado (sem entry point para consultar).
    "bcb": "bcb-sgs-fetcher",
    "bcb_microdados": "bcb-microdados-fetcher",
    "datasus-sigtap": "sigtap-fetcher",
    "ibge": "ibge-sidra-fetcher",
    "rfb": "rfb-cnpj-fetcher",
    "sidra": "ibge-sidra-fetcher",
    "td": "tesouro-direto-fetcher",
    "tesouro_direto": "tesouro-direto-fetcher",
    "transparencia_sp": "transparencia-sp-fetcher",
}
#: Nome antigo da reserva, mantido como alias.
SOURCES_REGISTRY = _RESERVA_OFFLINE

FETCHER_SUFFIX = "-fetcher"

# Reserva offline dos renomes de distribuição: antiga -> nova. A fonte é o
# ``historico`` do registro público (``distribuicoes_renomeadas``); esta
# constante só cobre o que já existia quando o registro passou a mandar. Quem
# tem a antiga instalada (ou registrada no ``state.toml``) migra para a nova em
# ``install``, ``doctor`` e ``uninstall``. A antiga segue nas wheels congeladas
# do índice, só para reprodução.
DISTRIBUICOES_RENOMEADAS: dict[str, str] = {
    "sidra-fetcher": "ibge-sidra-fetcher",
}

#: Validade do cache local do registro público.
REGISTRY_CACHE_TTL = 24 * 3600

# Marca que a rede já falhou neste processo: evita repetir a espera a cada
# resolução (``--refresh`` ignora a marca).
_REDE_FALHOU = False


def registry_cache_path() -> Path:
    """Cache local do registro público: ``~/.cache/quantilica/registry.json``."""
    return Path.home() / ".cache" / "quantilica" / "registry.json"


def registry_url() -> str:
    """URL do ``registry.json`` público, ao lado do ``simple/`` do índice."""
    base = get_index_url().rstrip("/")
    if base.endswith("/simple"):
        base = base[:-7]
    return f"{base.rstrip('/')}/registry.json"


def _registro_valido(doc: object) -> dict[str, Any] | None:
    """O documento, se tem a forma de um registro e o ``sha256`` (quando há) bate."""
    if not isinstance(doc, dict):
        return None
    fetchers = doc.get("fetchers")
    if not isinstance(fetchers, list) or not all(
        isinstance(f, dict) and isinstance(f.get("id"), str) for f in fetchers
    ):
        return None
    digest = doc.get("sha256")
    if digest is not None:
        from quantilica.core.registry import registry_sha256

        try:
            if registry_sha256(doc) != digest:
                return None
        except (KeyError, TypeError):
            return None
    return doc


def _le_cache() -> dict[str, Any] | None:
    try:
        return _registro_valido(
            json.loads(registry_cache_path().read_text(encoding="utf-8"))
        )
    except (OSError, ValueError):
        return None


def _baixa_registro() -> dict[str, Any] | None:
    """Baixa o registro público; ``None`` quando o índice não responde ou o
    documento é inválido."""
    try:
        req = urllib.request.Request(
            registry_url(), headers={"User-Agent": "quantilica-cli"}
        )
        with urllib.request.urlopen(req, timeout=5) as response:
            if response.status == 200:
                return _registro_valido(json.loads(response.read().decode("utf-8")))
    except Exception:
        pass
    return None


def registro_publico(refresh: bool = False) -> dict[str, Any] | None:
    """O ``registry.json`` público, com cache local de 24 h.

    Cache fresco (menos de 24 h) é usado sem rede. Vencido, ou com ``refresh``,
    baixa de novo; sem resposta do índice, vale o cache vencido. ``None`` quando
    não há rede nem cache (a reserva offline cobre).
    """
    global _REDE_FALHOU
    path = registry_cache_path()
    if not refresh:
        try:
            fresco = time.time() - path.stat().st_mtime < REGISTRY_CACHE_TTL
        except OSError:
            fresco = False
        cached = _le_cache()
        if cached is not None and (fresco or _REDE_FALHOU):
            return cached
        if _REDE_FALHOU:
            return None
    doc = _baixa_registro()
    if doc is None:
        _REDE_FALHOU = True
        return _le_cache()
    _REDE_FALHOU = False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass  # sem cache: o documento baixado vale para esta execução
    return doc


def nomes_do_registro(doc: dict[str, Any]) -> dict[str, str]:
    """Mapa ``id/apelido/id antigo -> distribuição`` de um registro.

    Registro v1 não traz ``distribution``: vale a regra ``<id>-fetcher``.
    """
    out: dict[str, str] = {}
    for f in doc.get("fetchers", []):
        dist = f.get("distribution") or f"{f['id']}{FETCHER_SUFFIX}"
        for name in (f["id"], *(f.get("aliases") or ())):
            out[name] = dist
        for r in f.get("historico") or ():
            if isinstance(r, dict) and isinstance(r.get("id"), str):
                out.setdefault(r["id"], dist)
    return out


def renomeadas_do_registro(doc: dict[str, Any]) -> dict[str, str]:
    """Mapa ``distribuição antiga -> nova`` a partir do ``historico``."""
    out: dict[str, str] = {}
    for f in doc.get("fetchers", []):
        dist = f.get("distribution") or f"{f['id']}{FETCHER_SUFFIX}"
        for r in f.get("historico") or ():
            old = r.get("distribution") if isinstance(r, dict) else None
            if isinstance(old, str) and old != dist:
                out[old] = dist
    return out


def distribuicoes_renomeadas() -> dict[str, str]:
    """Renomes de distribuição: o ``historico`` do registro público sobre a reserva."""
    out = dict(DISTRIBUICOES_RENOMEADAS)
    doc = registro_publico()
    if doc is not None:
        out.update(renomeadas_do_registro(doc))
    return out


def resolve_dist(source: str, registry: dict[str, str] | None = None) -> str:
    """Resolve o nome curto de uma fonte para a distribuição pip.

    Com ``registry`` (tabela já montada, p.ex. por ``_merged_registry``), ele
    vale primeiro. Sem ele, a ordem é: fetcher instalado (id e apelidos do
    ``FetcherInfo``), registro público, reserva offline (``_RESERVA_OFFLINE``).
    Em seguida, nome que já é uma distribuição (termina em ``-fetcher`` ou começa
    com ``quantilica-``) passa como veio e, por fim, a regra ``<nome>-fetcher``.
    """
    if registry is not None:
        if source in registry:
            return registry[source]
    else:
        instalado = installed_declared_names()
        if source in instalado:
            return instalado[source]
        publico = registro_publico()
        if publico is not None:
            nomes = nomes_do_registro(publico)
            if source in nomes:
                return nomes[source]
        if source in _RESERVA_OFFLINE:
            return _RESERVA_OFFLINE[source]
    if source.endswith(FETCHER_SUFFIX) or source.startswith("quantilica-"):
        return source
    return f"{source}{FETCHER_SUFFIX}"


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


def is_dist_installed(dist_name: str) -> bool:
    """Diz se a distribuição ``dist_name`` está instalada no ambiente atual."""
    try:
        distribution(dist_name)
    except PackageNotFoundError:
        return False
    return True


def installed_source_names() -> set[str]:
    """Nomes sob os quais as fontes instaladas respondem.

    União dos nomes de entry point com os ids e apelidos que os pacotes
    declaram (``FetcherInfo``). Só o entry point não basta: ``sidra``, ``ibge``,
    ``bcb`` e ``rfb`` são apelidos, e o entry point tem o id (``ibge-sidra``).
    """
    return get_installed_entry_points() | set(installed_declared_names())


def renamed_old_dists(dist_name: str) -> list[str]:
    """Distribuições antigas, hoje renomeadas para ``dist_name``, que estão instaladas."""
    return [
        old
        for old, new in distribuicoes_renomeadas().items()
        if new == dist_name and is_dist_installed(old)
    ]


def _remove_old_dists(old_dists: list[str]) -> None:
    """Desinstala as distribuições antigas, já substituídas pela nova."""
    for old in old_dists:
        uninstall_package(old)


def get_lake_entry_points() -> list[tuple[str, str | None]]:
    """Lista os conversores do lake (entry points ``quantilica.lake``) instalados.

    Não carrega os entry points: importar o conversor exige os extras de
    análise, e o ``doctor`` só precisa saber se ele está registrado.

    Returns:
        Pares ``(nome do entry point, nome da distribuição ou None)``.
    """
    return [
        (ep.name, ep.dist.name if ep.dist is not None else None)
        for ep in entry_points(group=LAKE_GROUP)
    ]


def has_lake_converter(
    source: str, lake_eps: list[tuple[str, str | None]], dist: str | None = None
) -> bool:
    """Indica se a fonte tem conversor do lake registrado.

    Casa pelo nome do entry point (``source_id``) ou pela distribuição da fonte.
    """
    dists = {d for _, d in lake_eps if d}
    return any(name == source for name, _ in lake_eps) or (
        dist is not None and dist in dists
    )


def get_index_url() -> str:
    """Retorna a URL do índice pip customizado ou default.

    Returns:
        A URL do índice pip.
    """
    return os.environ.get("QUANTILICA_INDEX_URL", DEFAULT_INDEX_URL)


def uv_index_args(idx: str) -> list[str]:
    """Argumentos do uv que fazem o índice da Quantilica ser consultado primeiro.

    No uv, ``--index`` tem prioridade sobre ``--default-index``; com
    ``first-index`` um pacote achado no primeiro índice não é buscado no
    segundo. Assim um homônimo no PyPI não substitui o da Quantilica, e
    dependências comuns (``requests``...) ainda vêm do PyPI.
    """
    return [
        "--default-index",
        PYPI_INDEX_URL,
        "--index",
        idx,
        "--index-strategy",
        "first-index",
    ]


def _index_lookup(dist_spec: str, index_url: str) -> bool | None:
    """``True`` se o índice publica, ``False`` se não (404), ``None`` sem resposta."""
    name = re.split(r"[\[<>=!~; ]", dist_spec, maxsplit=1)[0]
    name = re.sub(r"[-_.]+", "-", name).lower()
    url = f"{index_url.rstrip('/')}/{name}/"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "quantilica-cli"})
        with urllib.request.urlopen(req, timeout=10) as response:
            return response.status == 200
    except urllib.error.HTTPError as exc:
        return False if exc.code == 404 else None
    except Exception:
        return None


def index_has(dist_spec: str, index_url: str) -> bool:
    """Diz se o índice da Quantilica publica a distribuição (PEP 503).

    Sem resposta ou com erro devolve ``False``: na dúvida, não instala.
    """
    return _index_lookup(dist_spec, index_url) is True


def install_package(
    dist_name: str, index_url: str | None = None, requested: str | None = None
) -> None:
    """Instala um pacote Python usando uv pip install se disponível, ou pip.

    Args:
        dist_name: Nome do pacote a instalar.
        index_url: URL do índice pip. Padrão para a URL do índice global se não especificada.
        requested: Nome digitado pelo usuário, citado no erro se difere de ``dist_name``.

    Raises:
        RuntimeError: Se a instalação falhar.
    """
    idx = index_url or get_index_url()

    # Só instala o que o índice da Quantilica publica. Sem isso, um nome
    # digitado errado (ou um fetcher ainda não publicado) cairia no PyPI, onde
    # qualquer pessoa pode registrar um pacote com esse nome.
    if not index_has(dist_name, idx):
        if _index_lookup(dist_name, idx) is None:
            raise RuntimeError(
                f"O índice da Quantilica ({idx}) não respondeu; "
                f"{dist_name} não foi instalado. Tente de novo mais tarde"
            )
        msg = f"{dist_name} não foi encontrado no índice da Quantilica ({idx})"
        if requested:
            msg += f". '{requested}' foi lido como '{dist_name}'"
        raise RuntimeError(msg)

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
            *uv_index_args(idx),
            dist_name,
        ]
    else:
        # pip não tem prioridade entre índices: aqui a proteção contra
        # dependency confusion é só parcial (o PyPI continua sendo consultado
        # em pé de igualdade). O caminho protegido é o do uv.
        cmd = [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--extra-index-url",
            idx,
            dist_name,
        ]

    console.print(f"[cyan]Instalando {escape(dist_name)}...[/cyan]")
    res = subprocess.run(cmd, env=env)
    if res.returncode != 0:
        msg = f"Falha ao instalar {dist_name} (código de saída: {res.returncode})"
        if requested:
            msg += f". '{requested}' foi lido como '{dist_name}'"
        raise RuntimeError(msg)


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


def fetch_remote_sources_or_none() -> dict[str, str] | None:
    """Lê o ``sources.json`` do índice; ``None`` quando o índice não responde."""
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

    return None


def fetch_remote_sources() -> dict[str, str]:
    """Obtém a lista remota de fontes disponíveis do arquivo sources.json do índice.

    Sem resposta do índice, devolve só o que se conhece localmente: a tabela
    manual e os fetchers instalados. A lista completa vive no índice.

    Returns:
        Um dicionário mapeando os nomes das fontes para os nomes das distribuições.
    """
    remote = fetch_remote_sources_or_none()
    if remote is not None:
        return remote
    return local_sources()


def local_sources() -> dict[str, str]:
    """Fontes conhecidas sem o índice: registro público em cache, reserva e instalados."""
    publico = registro_publico()
    return {
        **_RESERVA_OFFLINE,
        **(nomes_do_registro(publico) if publico is not None else {}),
        **installed_declared_names(),
    }


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


def installed_declared_names() -> dict[str, str]:
    """Ids e apelidos dos fetchers instalados que declaram ``FetcherInfo``.

    Mapeia ``id/apelido -> distribuição``. Falhas na descoberta não impedem o
    uso da tabela manual.
    """
    try:
        from quantilica.cli.registry import declared_source_names

        return declared_source_names()
    except Exception:  # pragma: no cover - a reserva manual cobre
        return {}


def _merged_registry(refresh: bool = False) -> dict[str, str]:
    """Combina reserva offline, ``sources.json``, registro público e instalados.

    Da menor para a maior precedência: a reserva (``_RESERVA_OFFLINE``, que cobre
    o que a regra ``<nome>-fetcher`` não alcança), o ``sources.json`` do índice,
    o registro público (``registro_publico``; ``refresh`` força baixar de novo)
    e, para o que está instalado, id e apelidos do ``FetcherInfo``.
    """
    publico = registro_publico(refresh=refresh)
    return {
        **_RESERVA_OFFLINE,
        **fetch_remote_sources(),
        **(nomes_do_registro(publico) if publico is not None else {}),
        **installed_declared_names(),
    }


_EXTRA_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _dist_spec(dist_name: str, extras: list[str]) -> str:
    """Monta a especificação ``dist[extra1,extra2]`` a partir do nome e dos extras.

    Args:
        dist_name: Nome da distribuição (ex: ``pdet-fetcher``).
        extras: Nomes dos extras opcionais (ex: ``["analysis"]``).

    Returns:
        A especificação aceita por ``uv pip install``/``pip install``.

    Raises:
        typer.BadParameter: Se algum nome de extra tiver caracteres inválidos.
    """
    names = [e.strip() for item in extras for e in item.split(",") if e.strip()]
    for name in names:
        if not _EXTRA_NAME.match(name):
            raise typer.BadParameter(
                f"nome de extra inválido: {name!r} (use letras, números, '.', '_' ou '-')",
                param_hint="--extra",
            )
    unique = list(dict.fromkeys(names))
    return f"{dist_name}[{','.join(unique)}]" if unique else dist_name


def _translate_state(registered: dict[str, str]) -> bool:
    """Troca, no estado, distribuições renomeadas pela nova. Devolve se mudou algo."""
    changed = False
    renomeadas = distribuicoes_renomeadas()
    for name, dist in registered.items():
        new = renomeadas.get(dist)
        if new:
            registered[name] = new
            changed = True
    return changed


@app.command("install")
def cmd_install(
    source: str = typer.Argument(
        ..., help="Nome da fonte/fetcher a instalar (ex: comex, rtn)"
    ),
    no_exec: bool = typer.Option(
        False, "--no-exec", help="Não re-executa a CLI automaticamente após instalar"
    ),
    extra: Annotated[
        list[str] | None,
        typer.Option(
            "--extra",
            "-e",
            help=(
                "Extra opcional do pacote a instalar junto (ex: analysis, para os "
                "comandos convert e pipeline). Repetível; aceita lista separada "
                "por vírgula."
            ),
        ),
    ] = None,
    refresh: Annotated[
        bool,
        typer.Option(
            "--refresh",
            help="Baixa de novo o registro público, ignorando o cache de 24 h.",
        ),
    ] = False,
) -> None:
    """Instala uma fonte de dados (fetcher) sob demanda."""
    registry = _merged_registry(refresh)
    dist_name = resolve_dist(source, registry)
    spec = _dist_spec(dist_name, extra or [])

    # Distribuição antiga renomeada (ex.: sidra-fetcher): precisa migrar, mesmo
    # que o nome pedido já responda pelo entry point dela.
    old_dists = renamed_old_dists(dist_name)
    installed = source in installed_source_names() or is_dist_installed(dist_name)
    if installed and spec == dist_name and not old_dists:
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
        install_package(spec)
    except Exception as exc:
        console.print(f"[red]Erro na instalação:[/red] {exc}")
        if source != dist_name:
            console.print(
                f"[yellow]'{escape(source)}' foi lido como "
                f"'{escape(dist_name)}'.[/yellow]"
            )
        raise typer.Exit(1) from exc

    # A nova entra antes de a antiga sair: se a instalação falha, não se perde a fonte.
    if old_dists:
        try:
            _remove_old_dists(old_dists)
        except Exception as exc:
            console.print(
                f"[red]Erro ao remover a distribuição antiga:[/red] {exc}\n"
                f"[yellow]Remova com: uv pip uninstall {' '.join(old_dists)}[/yellow]"
            )
            raise typer.Exit(1) from exc
        console.print(
            f"[cyan]Distribuição renomeada: {', '.join(old_dists)} -> "
            f"{dist_name}.[/cyan]"
        )

    # Gravar no arquivo de estado (sem extras: o estado registra só a distribuição)
    state = load_state()
    registered = state.setdefault("installed", {})
    _translate_state(registered)
    registered[source] = dist_name
    save_state(state)

    escaped = spec.replace("[", r"\[")
    console.print(
        f"[green]✓ Fonte '{source}' ({escaped}) instalada com sucesso![/green]"
    )

    if not no_exec:
        # Re-executar a CLI para recarregar entry points. Os argumentos são
        # refeitos sem --extra: com ele, a segunda passada reinstalaria o pacote
        # e reexecutaria de novo, em laço.
        reexec_cli(["install", source])


@app.command("uninstall")
def cmd_uninstall(
    source: str = typer.Argument(
        ..., help="Nome da fonte/fetcher a desinstalar (ex: comex)"
    ),
    refresh: Annotated[
        bool,
        typer.Option(
            "--refresh",
            help="Baixa de novo o registro público, ignorando o cache de 24 h.",
        ),
    ] = False,
) -> None:
    """Desinstala uma fonte de dados (fetcher)."""
    registry = _merged_registry(refresh)
    dist_name = resolve_dist(source, registry)

    # Se a instalada é a distribuição antiga (renomeada), é ela que sai.
    old_dists = renamed_old_dists(dist_name)
    targets = [dist_name] if is_dist_installed(dist_name) or not old_dists else []
    targets += old_dists

    try:
        for target in targets:
            uninstall_package(target)
    except Exception as exc:
        console.print(f"[red]Erro na desinstalação:[/red] {exc}")
        raise typer.Exit(1) from exc

    state = load_state()
    registered = state.get("installed", {})
    gone = [n for n, d in registered.items() if n == source or d in targets]
    for name in gone:
        del registered[name]
    if gone:
        save_state(state)

    console.print(f"[green]✓ Fonte '{source}' desinstalada com sucesso.[/green]")


@app.command("doctor")
def cmd_doctor(
    refresh: Annotated[
        bool,
        typer.Option(
            "--refresh",
            help="Baixa de novo o registro público, ignorando o cache de 24 h.",
        ),
    ] = False,
) -> None:
    """Verifica inconsistências entre o estado registrado e os plugins instalados.

    Também informa, por fonte, se há conversor do lake (``quantilica.lake``).
    """
    if refresh:
        registro_publico(refresh=True)
    state = load_state()
    registered = state.get("installed", {})
    # Entradas com o nome antigo da distribuição passam para a nova.
    if _translate_state(registered):
        save_state(state)
    installed_eps = installed_source_names()

    missing: dict[str, str] = {}
    for name, dist in registered.items():
        # Ainda com a distribuição antiga instalada: falta migrar.
        if name not in installed_eps or renamed_old_dists(dist):
            missing[name] = dist

    if not missing:
        console.print(
            "[green]✓ Todas as fontes registradas estão instaladas e ativas.[/green]"
        )
    else:
        console.print(
            f"[yellow]Encontradas {len(missing)} fonte(s) registrada(s) mas ausente(s) no ambiente:[/yellow]"
        )
        for name, dist in missing.items():
            console.print(f" - {name} ({dist})")

        recovered: set[str] = set()  # ids e apelidos da mesma distribuição
        for name, dist in missing.items():
            if dist in recovered:
                continue
            console.print(f"[cyan]Reinstalando {name} ({dist})...[/cyan]")
            try:
                old_dists = renamed_old_dists(dist)
                install_package(dist)
                _remove_old_dists(old_dists)
                recovered.add(dist)
                console.print(f"[green]✓ {name} recuperado.[/green]")
            except Exception as exc:
                console.print(f"[red]Falha ao recuperar {name}:[/red] {exc}")

    _print_lake_status(installed_eps | set(registered), registered)


def _print_lake_status(sources: set[str], registered: dict[str, str]) -> None:
    """Imprime, por fonte instalada/registrada, se há conversor do lake."""
    if not sources:
        return
    lake_eps = get_lake_entry_points()
    console.print("\n[bold]Lake (conversor quantilica.lake):[/bold]")
    for name in sorted(sources):
        dist = registered.get(name) or resolve_dist(name)
        if has_lake_converter(name, lake_eps, dist):
            console.print(f" - {name}: [green]✓[/green] lake: sim")
        else:
            console.print(f" - {name}: [dim]lake: não[/dim]")
