# Changelog

Todas as mudanças notáveis deste projeto serão documentadas neste arquivo.

O formato segue [Keep a Changelog](https://keepachangelog.com/pt-BR/1.1.0/),
e este projeto adere ao [Semantic Versioning](https://semver.org/lang/pt-BR/).

## [0.13.0] - 2026-10-27

### Adicionado

- `convert`: novas opções `--lake` (raiz do lake de destino; padrão `lake_root()` de `quantilica.core.lake`, ou `default_output` se o core não tiver o módulo), `--dataset` (repetível) e `--force`. `-o/--output` continua aceito como alias de `--lake`. O conversor é chamado como `func(input, lake)` e recebe `dataset=` e `force=` por keyword só quando a assinatura os aceita (ou tem `**kwargs`); pedir `--dataset`/`--force` a um conversor que não os aceita sai com código 2.
- `convert` e `pipeline`: se o conversor devolver um objeto (ou dicionário) com `converted`, `skipped` e `errors`, o sumário é exibido, e `errors > 0` sai com código 1.
- `pipeline --lake` (com `--parquet-dir` mantido como alias), repassado ao conversor.
- `quantilica doctor`: informa, por fonte, se há conversor do lake registrado no entry point `quantilica.lake`.

### Alterado

- `pipeline`: o destino da conversão passa a ser `lake_root()` por padrão, em vez de igual a `--output`.

## [0.12.0] - 2026-10-19

### Adicionado

- `FetcherApp(source_id=...)`: `source_id` explícito, gravado nos manifests de download. Sem ele, continua derivado de `name` sem o sufixo `-fetcher`, e o `default_output` padrão passa a ser `/data/{source_id}`.

### Corrigido

- `check`: o probe `GET Range` tenta de novo (com backoff) em 429/5xx e em erros de transporte (`DEFAULT_RETRY_EXCEPTIONS`: timeout, reset de conexão) antes de desistir. Antes, uma falha transitória virava 404 e a URL saía como indisponível. Quando o probe desiste, a causa é logada em `WARNING` (`[check] probe Range falhou para <url> após N tentativa(s): ...`), e não mais em `DEBUG`.
- `check`: uma exceção inesperada em uma entrada (por exemplo, `OSError` ao montar o caminho local) não aborta mais o `check` inteiro com código 1. A entrada é verificada de novo uma vez e, se falhar outra vez, vira `download` com `reason=check-error: ...`, e o plano segue cobrindo as demais.

## [0.11.0] - 2026-10-14

### Adicionado

- `sync --force` (e `force` keyword-only em `download_datasets`, `download_entry` e `_download_file`, repassado ao cliente só quando `True`): baixa mesmo com o local atualizado.
- `check --out PATH`: grava o plano JSON de forma atômica e imprime só um resumo; `OSError` sai com código 1.
- `check`: `HEAD` com 403 ou 405 passa a um probe `GET` com `Range: bytes=0-0` e `Accept-Encoding: identity` (tamanho via `Content-Range`, corpo nunca lido); URL que dá 404 no `HEAD` e no probe vira a ação `skip-unavailable`, aceita por `CheckPlan.from_dict`.
- `FetcherApp.entry_from_plan`: gancho para fetchers que serializam objetos ricos na entrada do plano.
- `default_client` lê `QUANTILICA_HTTP_MAX_RESUMES` (opcional), repassado como `max_resumes` ao `HttpClient`.
- `quantilica install <fonte> --extra <nome>` (`-e`, repetível, aceita lista separada por vírgula): instala `<dist>[<extra>]` no ambiente da CLI, por exemplo `quantilica install pdet --extra analysis` para habilitar `convert` e `pipeline`. Funciona também em fonte já instalada. O estado continua registrando só a distribuição, e o re-exec pós-instalação não repete `--extra`, para não reinstalar em laço. Nomes de extra inválidos saem com código 2.

### Alterado

- Piso de `quantilica-core` elevado para `>=0.11.0` (símbolo usado: `max_resumes` em `HttpClient`).

### Removido

- `quantilica.cli.progress` (`file_progress`, `batch_progress`): módulo morto que nunca importou. Fazia `from .http import ProgressCallback` (não existe em `quantilica.cli`; o tipo vive em `quantilica.core.http`) e usava `tqdm`, que não é dependência do pacote. Nenhum fetcher o importava; o progresso real é Rich (`quantilica.cli.ui.make_download_progress`, `make_batch_progress`, `ProgressPool`).

### Corrigido

- `install_package` imprimia `Instalando pdet-fetcher...` sem o `[analysis]`: o Rich tratava o colchete como marcação. Agora o nome é escapado.
- `sdk`: a dica de extra ausente em `convert`, `archive` e `pipeline` mandava `pip install <fetcher>[analysis]`, que não funciona para fetchers do Fluxo B (fora do PyPI). Agora imprime `quantilica install <chave> --extra analysis`, com a chave de registro resolvida pelo entry point (por exemplo `td` para `tesouro-direto-fetcher`).
- `sdk`: `quantilica <fonte> list` (e o rótulo de partição do `check`/`sync`) terminava com `ValueError: Unknown format code 'd' for object of type 'str'` quando o catálogo traz `month` como string, caso do PDET (grupo capturado de regex). O mês e o semestre agora passam por `int()` antes do `:02d`, em `_partition_label`, `default_path_builder` e `list`; o `list` reutiliza `_partition_label` em vez de duplicar a lógica.

## [0.10.1] - 2026-10-08

### Corrigido

- `sdk`: `check_entry` considera `local.exists() or sidecar.exists()`, preservando
  o status de `manifest-fresh` em ambientes sem retenção local dos arquivos sem forçar
  re-download redundante quando o dado bruto já foi espelhado.

## [0.10.0] - 2026-10-07

### Adicionado

- `FetcherApp`: comando `check` — verificação remoto × local (HEAD por
  entrada) com plano de freshness, sem baixar; `--json` emite plano
  consumível por `sync --from-plan`.
- `FetcherApp.sync`: opção `--from-plan <arquivo.json>` — baixa somente as
  entradas com ação `download` do plano.
- SDK: `CheckPlan`/`CheckPlanItem` (tabela Rich + round-trip JSON) e
  `default_client()` com overrides via ambiente (`QUANTILICA_CA_BUNDLE`,
  `QUANTILICA_SSL_VERIFY`, `QUANTILICA_HTTP_ATTEMPTS/TIMEOUT/RETRY_DELAY`).

### Alterado

- Piso de `quantilica-core` elevado para `>=0.10.0` (símbolos usados:
  `is_remote_more_recent`, `resolve_verify_from_env`).
- Workers internos do `download_datasets` propagam o `transport` customizado
  do client canônico.

## [0.9.1] - 2026-10-04

Correção de escape inválido introduzido na v0.8.0 (extensões do SDK).

### Corrigido
- `SyntaxWarning: invalid escape sequence '\['` nas mensagens de `convert`,
  `archive` e `pipeline` do `FetcherApp` (`quantilica/cli/sdk.py`): as três
  f-strings passaram a raw (`rf"..."`), preservando o texto enviado ao Rich
  (`\[analysis]` escapado). O warning aparecia na saída de **todo**
  `quantilica <fonte> --help` e hoje também é `SyntaxError` em Python ≥ 3.14.
- Novo teste de higiene (`tests/test_package_hygiene.py`) que compila todos os
  módulos do pacote com `SyntaxWarning` convertido em erro, fechando a porta
  para regressões da mesma classe (o ruff não cobre escapes inválidos com o
  conjunto de regras `E,F,I,UP,B`).

## [0.9.0] - 2026-10-04

Registra o `cvm-fetcher` como fonte instalável sob demanda, após a v0.1.0 do
fetcher (wheel no GitHub Releases, entrada no índice PEP 503 do ecossistema).

### Adicionado
- Fonte `cvm` em `SOURCES_REGISTRY` (`quantilica install cvm`), apontando
  para a distribuição `cvm-fetcher`.

## [0.8.0] - 2026-10-03

Padronização do SDK e consolidação dos fetchers —
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
- `quantilica health`: subcomando de diagnóstico que faz sondas HTTP leves
  (HEAD com fallback para GET, timeout padrão 5s) contra as fontes canônicas
  do ecossistema — BCB SGS, SIDRA, Tesouro Direto, Comex e INMET — em
  paralelo, com saída em tabela Rich (`Fonte | Estado | Latência | HTTP`) ou
  em JSON estruturado via `--json`. Usa `quantilica.core.http` para o probe.

## [0.7.0] - 2026-10-02

Extensões do SDK (`quantilica.cli.sdk`) para eliminação de
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
- `DEFAULT_INDEX_URL` apontado para `https://index.quantilica.com/simple/` — a URL anterior (`quantilica.com/quantilica-index/`) parou de ser servida quando o domínio passou ao portal, quebrando o `quantilica install` para fetchers fora do PyPI legado (detalhes na decisão de distribuição de 2026-08-22).
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
