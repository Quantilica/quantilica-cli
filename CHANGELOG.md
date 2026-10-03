# Changelog

Todas as mudanças notáveis deste projeto serão documentadas neste arquivo.

O formato segue [Keep a Changelog](https://keepachangelog.com/pt-BR/1.1.0/),
e este projeto adere ao [Semantic Versioning](https://semver.org/lang/pt-BR/).

## [0.8.0] - 2026-10-03

Onda A.2 do plano `2026-10-03-padronizacao-core-e-consolidacao-fetchers` —
decorators de ciclo de vida no SDK (`quantilica.cli.sdk`) e abstração de
pré-visualização tabular de sincronização.

### Adicionado
- `FetcherApp.command_convert(func)`: decorator que registra o subcomando
  `convert` com flags canônicas (`-i/--input`, `-o/--output`, `--verbose`,
  padrões derivados de `default_output`). Configura logging Rich, trata
  `ImportError` graciosamente (sugerindo `pip install {nome}[analysis]`,
  saída com código 1) e exibe confirmação com check verde.
- `FetcherApp.command_pipeline(func)`: decorator que registra o subcomando
  `pipeline` encadeando sincronização (passo 1/2, via `sync`) e conversão
  analítica (passo 2/2, via `func`). Opções: grupos, `--output`,
  `--parquet-dir`, `--workers`, `--dry-run` (interrompe antes do passo 2) e
  `--verbose`.
- `FetcherApp.command_archive(func)`: decorator que registra o subcomando
  `archive` para arquivamento histórico, com o mesmo padrão de flags e
  tratamento gracioso de `ImportError`.
- `SyncPlanItem`/`SyncPlan`: dataclasses (imutáveis) para planejamento de
  sincronização, com `SyncPlan.render_table(console=None)` que renderiza uma
  tabela Rich (`Dataset | Partição | Arquivo | URL`) e o sumário `Total: X
  arquivos planejados. Y ignorados fora de cobertura.` Importados do
  `quantilica.cli.sdk`.

## [0.7.0] - 2026-10-02

Onda 2 — extensões do SDK (`quantilica.cli.sdk`) para eliminação de
boilerplate nos fetchers (decisão
`2026-10-02-padronizacao-e-deduplicacao-fetchers`).

### Adicionado
- `DataRepository` canônico no SDK (baseado em `StampedDataRepository` do core)
  com `path_for_entry(entry, last_modified=...)`, estabelecendo a convenção
  única de layout: `{dataset_id}/{slug}[@{partition}]@{YYYYMMDD}.{ext}`.
- `default_path_builder(output_dir, entry, last_modified)`: path builder
  canônico para fetchers que não fornecem um próprio.
- `FetcherApp.attach_command(cmd_func, name=None, **kwargs)`: registro limpo de
  subcomandos customizados (`convert`, `pipeline`, `archive`) sem subclassificar
  e sobrescrever `_build_commands`.
- `FetcherApp(build_default_commands=False)`: instancie o app sem os comandos
  padrão `sync`/`list` (fim do padrão `def _build_commands(): pass`).
- `make_resolve_groups(groups_dict, aliases_dict)`: helper que constrói
  resolver de grupos/aliases para comandos customizados (dedup, ordem
  declarada, erro em grupos desconhecidos); o comando `sync` padrão agora o
  usa.
- Commit `feat(sdk)`: `default_client()` já com `emulate_browser` e pooling
  keep-alive por worker em `download_datasets` (anteriomente não documentado).

### Alterado
- `path_builder` no `FetcherApp` é agora opcional (default:
  `default_path_builder`).

## [0.3.2] - 2026-08-22

### Corrigido
- **Crítico:** wheels publicados desde a 0.3.0 vinham **sem os módulos do pacote** — o diretório `quantilica/cli/` não era incluído no build por configuração incorreta do hatchling (`sources` na seção global e `packages` apontando para o subpacote). Instalações via pip/uv reportavam sucesso, mas `import quantilica.cli` falhava em qualquer ambiente não-editable. Configuração realinhada ao padrão dos pacotes irmãos (`packages = ["src/quantilica"]` dentro de `[tool.hatch.build.targets.wheel]`).

## [0.3.1] - 2026-08-22

### Corrigido
- `DEFAULT_INDEX_URL` apontado para `https://index.quantilica.com/simple/` — a URL anterior (`quantilica.com/quantilica-index/`) parou de ser servida quando o domínio passou ao portal, quebrando o `quantilica install` para fetchers fora do PyPI legado (detalhes no ADR de distribuição de 2026-08-22).
- `install`/`uninstall` agora mesclam o registro remoto (`sources.json`) com o registro local, resolvendo também nomes canônicos do índice (ex.: `tesouro-direto`, além de `td`).

## [0.3.0] - 2026-08-10

### Adicionado
- Extensão da `FetcherApp` (`sdk.py`) com suporte opcional para `FtpClient`.
- Exposição do método `download_datasets` na `FetcherApp` para uso por comandos Typer customizados nos plugins.
- Aceitação e passagem do parâmetro `aliases_dict` para personalização total dos subcomandos por fetcher.

### Alterado
- Padrão de metadados do pacote portado integralmente para a PEP 639 (licença) e PEP 561 (tipagem estática).

## [0.2.2] - 2026-07-28
*(Release retroativo não documentado)*

## [0.2.0] - 2026-07-15
*(Release retroativo não documentado)*

## [0.1.0] - 2026-06-04

Primeira entrada em formato Keep a Changelog; documenta o estado do pacote nesta
versão.

### Adicionado

- CLI unificada `quantilica` que descobre e monta os fetchers instalados via
  entry points `quantilica.fetchers`, sem depender diretamente dos pacotes de
  fetcher.
- Comando `list-sources` e montagem automática dos sub-apps Typer de cada fetcher
  instalado (`quantilica <fonte> ...`).
