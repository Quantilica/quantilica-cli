# Changelog

Todas as mudanças notáveis deste projeto serão documentadas neste arquivo.

O formato segue [Keep a Changelog](https://keepachangelog.com/pt-BR/1.1.0/),
e este projeto adere ao [Semantic Versioning](https://semver.org/lang/pt-BR/).

## [Não lançado]

### Removido

- **Quebra:** comandos de uso interno da equipe saíram do pacote. Deixam de existir `quantilica catalog sync` (o grupo `catalog` inteiro), as opções `--somente-leitura`, `--estado`, `--limpar-marcador`, `--exceptions-from-ledger` e `--exceptions-max-age-hours` do `quantilica lake audit`, a opção `--estado` e a variável `QUANTILICA_LAKE_ESTADO` do `quantilica docs schema`, e os módulos `quantilica.cli.catalog`, `quantilica.cli.lake_estado` e `quantilica.cli.ledger_exceptions`. Quem passar uma dessas opções recebe erro de uso (código 2). O restante de `lake select`, `lake build` e `lake audit` (inclusive `--ledger` e `--exceptions`) e de `docs` continua igual.
- Entradas antigas deste arquivo que descreviam esses comandos foram resumidas.

### Alterado

- Comentários e textos do pacote falam em "agendador" e não em rotinas específicas de uma equipe; os códigos de saída do `sync` não mudam.
- O fallback do diretório do lake (`_default_lake_dir`) deixa de usar `~/data/lake` e passa a usar `$XDG_DATA_HOME/quantilica/lake` (ou `~/.local/share/quantilica/lake`), igual ao `quantilica.core.lake.lake_root()`. `QUANTILICA_LAKE_DIR` e `QUANTILICA_DATA_DIR` continuam tendo precedência.

## [0.27.0] - 2026-11-19

### Adicionado
- `sync --summary-json ARQUIVO` (e `pipeline --summary-json`, que o repassa ao passo de download): grava, ao terminar, um resumo JSON com o que antes só saía na linha de texto `Concluído: X/Y arquivo(s) baixado(s)`. Esquema versão 1: `schema_version`, `tool`/`tool_version`, `fetcher`, `command`, `status` (`success`, `partial`, `interrupted` ou `failed`), `exit_code`, `dry_run`, `started_at`/`finished_at`/`duration_seconds` (UTC), `total`, `succeeded` (o numerador de `Concluído`, inclui o que já estava em dia), `failed`, `downloaded` (transferências de fato), `skipped` (em dia), `bytes_downloaded`, `errors` (até 100 `{id, message}`, com `errors_truncated`) e `error` (motivo de falha que não é de arquivo: plano inválido, grupo desconhecido, exceção inesperada). `downloaded`, `skipped` e `bytes_downloaded` vêm do callback de progresso do cliente e são `null` quando o fetcher substitui `download_datasets`. A gravação é atômica, cria a pasta pai e nunca muda o código de saída: se falhar, avisa em `stderr`. Também grava na falha (grupo ou plano inválido, interrupção, exceção). Novo módulo `quantilica.cli.summary` (`SummaryRecorder`, `DownloadStats`, `write_summary_json`) para os `sync` próprios dos fetchers adotarem o mesmo contrato; `FetcherApp.last_download_stats` expõe os contadores do último `download_datasets`.
- `--summary-json` também grava no timeout: com a opção pedida, o `sync` instala um handler de `SIGTERM` que levanta `SystemExit(143)` (restaura o anterior ao sair), então o arquivo sai com `status` `interrupted` e `exit_code` 143. O `graceful_executor` passou a cancelar as entradas pendentes também nesse caso. Ctrl-C fora do download vira `Exit(130)` explícito, igual ao `exit_code` do arquivo em qualquer versão do typer.
- `pipeline --summary-json` só repassa a opção se o `sync` registrado aceita o parâmetro `summary_json`; senão avisa no `stderr` e segue sem o resumo, e a opção fica oculta na ajuda do `pipeline` (quem sonda a ajuda não tem falso positivo; caso de fetchers com `build_default_commands=False`).
- Ajustes em comandos de uso interno da equipe, removidos do pacote na 0.28.0.

## [0.26.0] - 2026-11-16

### Alterado
- O extra `lake` exige `quantilica-analytics>=0.13.0`.

### Adicionado
- Ajustes em comandos de uso interno da equipe, removidos do pacote na 0.28.0.

### Alterado
- Verificação de tipos com `pyright` (`[tool.pyright]`, modo `basic`) zerada em `src`.

## [0.25.0] - 2026-11-16
### Alterado

- Piso de `quantilica-core` sobe para `>=0.25.0` (`HttpClient.share_host_state` e `max_concurrency` em `stream()`, de que o `download_datasets` depende para compartilhar a política por host entre as threads).

### Corrigido

- `FetcherApp.download_datasets` respeita a política por host do client do app (`HttpClient.host_policies`): os clients das threads passam a compartilhar as políticas e o estado do limitador (`min_interval`, pausa de `429` com `Retry-After`, `max_concurrency`) com o client do app, com `HttpClient.share_host_state`. Antes cada thread criava um client sem política e com limitador próprio, e o host recebia N vezes o ritmo configurado com N `--workers`; o fetcher precisava sobrescrever `download_datasets` para contornar. Sem política por host nada muda (clients independentes, mesmo paralelismo). Com a política, o teto de simultâneas vale também no `GET` do arquivo (`stream` do core), então quem declara só `min_interval` fica, pelo padrão `max_concurrency=4`, com no máximo 4 downloads simultâneos por host.
- Ao fim de `download_datasets` o `self.client` volta sempre ao client do app. Antes, com threads concorrentes, podia ficar um client de thread (já fechado) no lugar dele.


## [0.24.0] - 2026-11-15
### Alterado
- Piso de `quantilica-core` sobe para `>=0.24.0` (granularidades semestral, trimestral e diária do perfil de colunas).
- Ajustes em comandos de uso interno da equipe, removidos do pacote na 0.28.0.

## [0.23.0] - 2026-11-15
### Adicionado
- Ajustes em comandos de uso interno da equipe, removidos do pacote na 0.28.0.

### Alterado
- Piso de `quantilica-core` sobe para `>=0.23.0` (o perfil de colunas usa `quantilica.core.lake_profile`).
- Ajustes em comandos de uso interno da equipe, removidos do pacote na 0.28.0.

### Corrigido
- Ajustes em comandos de uso interno da equipe, removidos do pacote na 0.28.0.
- CI: o pyproject declara o PyPI antes do índice Quantilica (`[[tool.uv.index]]`), e o workflow perde `--index` e `--index-strategy`. Com a estratégia padrão `first-index`, o `uv sync` só consulta o índice para o que não está no PyPI (`quantilica-analytics`) e deixa de receber o 503 intermitente que o GitHub Pages devolve a caminhos inexistentes.

## [0.22.0] - 2026-11-14

### Adicionado

- `quantilica registry export` exporta o registro v2: `module` (do entry point),
  `repo` (de `[project.urls] Repository`/`Source`; ausente, assume
  `https://github.com/Quantilica/<distribuição>` e avisa no stderr), `version`
  e `lake_entry_point`, mais o `sha256` do conteúdo. Os campos da v1 seguem com
  o mesmo nome e formato.
- `registry export --ecossistema registro.toml` junta a camada dos fatos do
  ecossistema (`docs_pasta`, `vitrine`, `ci_integracao`, `json_leve`, `mascote`,
  `health_url`, `historico`), validada por
  `quantilica.core.registry.carrega_registro_ecossistema`.
- `registry export --strict` também falha com distribuição fora da regra
  `<id>-fetcher` que o `historico` não explica e, com `--ecossistema`, com
  fetcher sem seção no arquivo.
- `registro_publico()`: baixa `index.quantilica.com/registry.json`, confere o
  `sha256` e guarda em `~/.cache/quantilica/registry.json` (validade de 24 h;
  vencido e sem rede, vale o cache). `--refresh` em `install`, `uninstall` e
  `doctor` ignora o cache. A escrita é atômica (arquivo temporário único e
  `os.replace`). Depois de uma falha de rede, um marcador `registry.fail` ao
  lado do cache evita nova tentativa por 10 min, para que cada processo (em
  especial o `quantilica health`) não espere o timeout de 5 s; `--refresh`
  ignora o marcador.
- O registro público só é aceito com `version` 1 ou 2; qualquer outra conta como
  "sem registro" (vale o cache e, depois, a reserva offline).

### Alterado

- Piso de dependência `quantilica-core>=0.21.0` (e no extra `docs`): o cli importa
  `IdentidadePacote` e o registro v2 do core 0.21.0.
- `resolve_dist` consulta, nesta ordem, o fetcher instalado, o registro público
  (id, apelidos e ids do `historico`) e a reserva offline. `SOURCES_REGISTRY`
  virou `_RESERVA_OFFLINE` (o nome antigo segue como alias), congelada: não
  recebe entradas novas.
- `resolve_dist` de um nome antigo de distribuição renomeada (`sidra-fetcher`)
  agora resolve para a nova (`ibge-sidra-fetcher`), com aviso no stderr. Antes
  passava como veio, e `install` e `doctor` discordavam (ambos migram para a
  nova). As wheels congeladas continuam no índice para reprodução.
- `DISTRIBUICOES_RENOMEADAS` passa a ser a reserva; os renomes vêm do
  `historico` do registro público (`distribuicoes_renomeadas()`).
- `quantilica health` sonda os `health_url` do registro público (nome da sonda =
  id do fetcher); `HEALTH_SOURCES` é a reserva, usada sem registro ou quando ele
  não declara nenhuma sonda.

## [0.21.4] - 2026-11-14

### Alterado

- Ajustes em comandos de uso interno da equipe, removidos do pacote na 0.28.0.

## [0.21.3] - 2026-11-13

### Corrigido

- Ajustes em comandos de uso interno da equipe, removidos do pacote na 0.28.0.

## [0.21.2] - 2026-11-13

### Corrigido

- Ajustes em comandos de uso interno da equipe, removidos do pacote na 0.28.0.

## [0.21.1] - 2026-11-13

### Corrigido

- Ajustes em comandos de uso interno da equipe, removidos do pacote na 0.28.0.

## [0.21.0] - 2026-11-13

### Alterado

- O fetcher do SIDRA passa a se chamar `ibge-sidra-fetcher` (id `ibge-sidra`): `quantilica sources install sidra` e `ibge` resolvem para a distribuição nova (apelidos na tabela manual, para valer mesmo sem o fetcher instalado); `ibge-sidra` segue a regra `<id>-fetcher`. O nome antigo `sidra-fetcher` continua aceito como distribuição, apontando para as wheels congeladas do índice.

### Corrigido

- `quantilica sources install` não re-executa mais em laço sem fim para fonte pedida por apelido: a fonte vale como instalada se o nome responde por entry point, por id/apelido declarado no `FetcherInfo` ou se a distribuição está instalada. Valia para `sidra`, `ibge`, `bcb` e `rfb`, cujo entry point tem o id (`ibge-sidra`, `bcb-sgs`, `rfb-cnpj`). `doctor` usa a mesma regra e deixou de reinstalar fonte presente só por apelido.
- Migração de distribuição renomeada (`DISTRIBUICOES_RENOMEADAS`, `sidra-fetcher` -> `ibge-sidra-fetcher`): `install sidra` com o `sidra-fetcher` antigo instalado instala o novo, remove o antigo e grava o novo no `state.toml`; `doctor` traduz entradas antigas do estado em vez de reinstalar o antigo; `uninstall` remove o antigo se for ele o instalado.

## [0.20.0] - 2026-11-12

### Alterado

- Ajustes em comandos de uso interno da equipe, removidos do pacote na 0.28.0.
- `quantilica sources install` consulta o índice da Quantilica antes do PyPI (uv: `--default-index` PyPI + `--index` Quantilica + `first-index`), e recusa instalar o que esse índice não publica, fechando o risco de dependency confusion; o erro de instalação informa quando o nome digitado foi lido como `<nome>-fetcher`.
- `quantilica sources install/uninstall` deixou de depender de uma lista de
  fetchers escrita à mão: o nome curto vira `<id>-fetcher` (regra do registro),
  então uma fonte nova é aceita sem editar o código. A tabela `SOURCES_REGISTRY`
  guarda só o que foge da regra (`sql` e apelidos antigos como `bcb`, `rfb`,
  `td`). Os nomes aceitos antes continuam levando à mesma distribuição.

### Corrigido

- Ajustes em comandos de uso interno da equipe, removidos do pacote na 0.28.0.

## [0.19.0] - 2026-11-11

### Adicionado

- `--workers` do `sync` e do `pipeline` aceita a variável de ambiente
  `QUANTILICA_SYNC_WORKERS` (`SYNC_WORKERS_ENVVAR`). A flag explícita vence a
  variável, que vence o padrão do comando (4, ou o que o fetcher definir). Serve
  a quem agenda a coleta e não pode acrescentar argumentos à linha de comando.
  O valor mínimo é 1: `0` ou negativo (pela flag ou pela variável) dá erro de uso
  (código 2) em vez de traceback, e o `--help` mostra o padrão do fetcher e a variável.

### Corrigido

- Ajustes em comandos de uso interno da equipe, removidos do pacote na 0.28.0.
- `sync` de todo fetcher construído com `FetcherApp` não sai mais com código 0
  quando algum arquivo falha. Se ao menos um arquivo foi baixado e outros
  falharam, sai com o código `3` (`EXIT_SYNC_PARTIAL`, "download parcial": o que
  baixou está íntegro em disco); se nada foi baixado, sai com `1`. Antes saía
  com `0` listando os erros, e quem automatiza a coleta (agendador, integração contínua) considerava a
  execução bem-sucedida. **Mudança de contrato:** scripts que tratam
  qualquer código diferente de 0 como falha total devem tratar o `3` como
  parcial. O `pipeline` converte o que baixou mesmo com download parcial e
  devolve `3` ao final; com falha total, não converte e sai com `1`.

## [0.18.0] - 2026-11-10

### Adicionado

- `quantilica registry export` inclui `redistribuicao` e `restricao_nota` de
  cada fetcher (e a coluna `Redistrib.` na tabela). `--strict` não falha por
  `redistribuicao` ausente: `None` significa "não avaliada". Requer
  `quantilica-core` com o campo (`>=0.20.0`).

### Alterado

- O subcomando visível de cada fetcher que declara `info` passa a ser o id
  canônico (`quantilica tesouro-direto`). O nome do entry point (`td`) e os
  apelidos de `info.aliases` continuam funcionando como subcomandos ocultos
  para o mesmo app, então `quantilica td sync` produz os mesmos caminhos,
  `source_id` e telemetria. Apelido que colide com comando embutido, com id ou
  entry point de outro plugin, ou reivindicado por dois plugins, não é montado
  e sai um aviso em stderr. Plugin sem `info` segue com o nome do entry point.

## [0.17.0] - 2026-11-10

### Adicionado

- `FetcherApp(info=FetcherInfo(...))`: o fetcher declara os fatos da fonte
  (id canônico, apelidos, órgão, licença, página, cadência). Sem `info`, um
  `FetcherInfo` mínimo é derivado de `name` e `info_declared` fica `False`;
  `source_id` segue como antes.
- `quantilica registry export [--json] [--strict]`: descobre os entry points
  `quantilica.fetchers` e `quantilica.lake` e monta o registro (info,
  `freshness_policy`, `default_output`, grupos, `lake` e `lake_source_id`).
  `--strict` falha se algum fetcher não declarar `info`, não carregar ou houver
  id/apelido duplicado.

### Alterado

- `quantilica install` e afins: para fetchers instalados que declaram `info`,
  id e apelidos vêm do `FetcherInfo`; `SOURCES_REGISTRY` fica como reserva
  para instalar fontes ausentes. Teste garante que os dois não se contradizem.

## [0.16.0] - 2026-11-08

### Adicionado

- `quantilica docs`: ferramentas da norma de documentação dos dados dos fetchers. `quantilica docs check <repo> [--source]` verifica a pasta `docs/` (a fonte e os datasets vêm do conversor `quantilica.lake` instalado ou, sem ele, das páginas em `docs/datasets/`) e sai com 1 se houver erro; `quantilica docs schema <fonte> --lake <dir> --out <docs/datasets> [--dataset ...]` grava os esquemas de referência lendo o lake; `quantilica docs update <repo>` reescreve a ficha técnica das páginas.
- Os comandos `quantilica docs` exigem o `quantilica-core` com o extra `docs` (`pip install "quantilica-core[docs]"`); sem ele, falham com a instrução de instalação.

## [0.15.1] - 2026-11-06

### Alterado

- `quantilica lake build --profile`: a ajuda passa a citar os perfis `batch` e `monolithic`; `96gb-batch` e `96gb-monolithic` continuam aceitos (depreciados no `quantilica-analytics`). O extra `lake` passa a exigir `quantilica-analytics>=0.11.0`.

## [0.15.0] - 2026-11-03

### Adicionado

- Ajustes em comandos de uso interno da equipe, removidos do pacote na 0.28.0.
- `quantilica lake build --profile` e `--dataset`: `--profile` aplica um perfil de memória (`96gb-batch` ou `96gb-monolithic`) e `--dataset` limita a conversão aos datasets pedidos. Exigem o extra `lake` com `quantilica-analytics>=0.9.0`.
- SDK dos coletores: `FetcherApp.command_backfill()` registra um subcomando `backfill` de carga histórica com ponto de retomada em `<saída>/.backfill/<nome>.json` (`--resume`/`--restart`, e as opções `--rateio` e `--regional` repassadas à fonte que as declara).
- SDK dos coletores: `FetcherApp(freshness_policy=...)` aplica a política de frescura (`quantilica.core.sync.FreshnessPolicy`). Com `remote_only` não há sonda `HEAD` e o download ativa a deduplicação por SHA-256 do `quantilica-core`.
- SDK dos coletores: o download infere o formato esperado pela extensão (`expected_format`, também aceito em `download_entry`) e repassa a conferência de assinatura do conteúdo ao `quantilica-core` quando o cliente de download a suporta.
- Fontes novas no mapa de comandos: `antt`, `bcb-microdados`, `capes`, `ipeadata`, `loterias`, `ons`, `pgfn`, `infosiga`, `sigtap` (também `datasus-sigtap`) e `transparencia-sp`.

### Alterado

- Exige `quantilica-core>=0.16.0`; o extra `lake` exige `quantilica-analytics>=0.9.0`.
- O caminho de destino dos arquivos baixados e o `dataset_id` gravado no manifesto passam a vir do `quantilica-core` (`path_for_entry`), os mesmos usados pelos coletores.
- `quantilica lake build`: a ajuda descreve a retomada como ela funciona a partir do `quantilica-analytics` 0.9.0 (só a execução interrompida é retomada; `--retry-errors` refaz as tarefas que falharam na execução mais recente).

### Corrigido

- `make_resolve_groups` aceita alias com valor `str` (por exemplo `all` para `exp`). Antes o valor era expandido caractere a caractere e a sincronização padrão ou completa falhava com "Grupo desconhecido".
- Com `freshness_policy=remote_only`, a deduplicação por SHA-256 só é repassada ao cliente de download que a aceita; antes o `FtpClient` recebia o argumento e a sincronização falhava com `TypeError`.
- Ajustes em comandos de uso interno da equipe, removidos do pacote na 0.28.0.
- Os testes de `--help` do `lake` não dependem mais de cor forçada no CI.

## [0.14.0] - 2026-10-30

### Adicionado

- `quantilica lake select|build|audit`: monta e audita o lake a partir de um bruto local. `select` cria a visão com a versão vigente de cada arquivo (`--mode hardlink|symlink|copy`, `--exclude ARQ` com `sha256` ou caminhos); `build` converte a visão em Parquet por fonte e dataset, em processos separados e com retomada (`--sources`, `--workers`, `--memory`, `--threads`, `--scratch`, `--force`, `--retry-errors`, `--no-disk-check`, `--allow-sleep`), e sai com código 1 se houver erro e 130 se for interrompido; `audit` confere rodapés, proveniência, cobertura, estrutura e leitores (`--input`, `--ledger`, `--sources`, `--no-readers`, `--no-integrity`, `--exceptions`, `--rehash`, `--workers`, `--json`, `--json-file`) e sai com código 1 se houver achados. O ledger é opcional.
- Extra `lake` (`pip install "quantilica-cli[lake]"`): traz o `quantilica-analytics`, que `build` e `audit` carregam só quando rodam; sem ele, avisam o que instalar e saem com código 1. `select` só precisa do `quantilica-core` (a partir da versão que traz `materialize_view`).
- CI: o job de testes também roda no Windows (Python 3.12).

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
- Ajustes em comandos de uso interno da equipe, removidos do pacote na 0.28.0.
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
