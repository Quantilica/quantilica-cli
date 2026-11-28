# quantilica-cli

CLI unificada para o ecossistema Quantilica de dados abertos brasileiros.
For full documentation, please visit [https://docs.quantilica.com](https://docs.quantilica.com).

## Instalação

O `quantilica-cli` é publicado no PyPI:

```bash
uv add quantilica-cli
# ou, como ferramenta global:
uv tool install quantilica-cli
```

Os fetchers não vêm com a CLI nem estão no PyPI: ficam no índice próprio da Quantilica e
são instalados sob demanda, pela própria CLI:

```bash
quantilica list-sources --remote      # fontes conhecidas no registro público
quantilica install comex                    # instala o comex-fetcher
quantilica install comex --extra analysis   # com o extra que habilita convert e pipeline
```

`quantilica doctor` (e `quantilica sources doctor`) confere se o estado registrado bate com os
plugins instalados, e `quantilica uninstall <fonte>` remove uma fonte.

## Uso

```bash
quantilica --help
quantilica list-sources
quantilica comex sync trade-exp
quantilica inmet sync bdmep
quantilica td sync prices
```

`sync` recebe grupos (ou apelidos de grupo) de cada fetcher; sem argumento baixa todos, e
`--dry-run` só lista o que baixaria. Outros comandos da CLI: `health` (sondas HTTP leves nas
fontes), `manifests list|show|status` (inspeção local dos `*.manifest.json`),
`registry export` e `docs check|schema|update` (documentação dos dados dos fetchers).

### Resumo da coleta em JSON

`sync --summary-json ARQUIVO` grava, ao terminar, um JSON com os contadores que o
comando também imprime na linha `Concluído: X/Y arquivo(s) baixado(s)`, para quem
precisa deles sem ler texto (um agendador, um painel, um teste):

```bash
quantilica comex sync trade-exp --summary-json resumo.json
```

```json
{
  "schema_version": 1,
  "fetcher": "comex-fetcher",
  "command": "sync",
  "status": "partial",
  "exit_code": 3,
  "total": 12, "succeeded": 11, "failed": 1,
  "downloaded": 4, "skipped": 7, "bytes_downloaded": 52428800,
  "errors": [{"id": "exp-2024", "message": "HTTP 503"}]
}
```

`succeeded` é o numerador de `Concluído` (inclui o que já estava em dia);
`downloaded`, `skipped` e `bytes_downloaded` separam o que foi transferido de fato,
e são `null` quando o fetcher baixa por código próprio. O arquivo é gravado de forma
atômica, também quando o comando falha (com o motivo em `error`), e a opção nunca muda
o código de saída. `pipeline --summary-json` repassa a opção ao passo de download.
O contrato completo está no docstring de `quantilica.cli.summary`.

### Regras do `sync` para autores de fetcher

O `sync` (e o `check`) do `FetcherApp` não aceita em silêncio um catálogo que esvaziou:

- **Catálogo vazio é erro.** Se `list_datasets` não devolve nenhuma entrada para os grupos
  pedidos, o `sync` sai com 1 e diz `catálogo vazio para <grupos>` (também em `error` no
  `--summary-json`). Se só alguns grupos vêm vazios e os outros têm entradas, é aviso em
  WARNING e o campo `empty_groups` do resumo. Um grupo que é vazio **por desenho** (por exemplo,
  um grupo só de catálogo, sem arquivo para baixar) se declara no construtor:
  `FetcherApp(..., expected_empty_groups=["ouvidoria"])`; esses grupos ficam fora do aviso e da
  regra (o `check` grava `expected_empty` no plano para o `sync --from-plan` aceitá-lo vazio). Não declare um grupo que esvazia por falha da fonte: ele precisa continuar falhando. Com catálogo vazio o `download_datasets` do plugin ainda roda, com a lista vazia, para que os erros dele (falha de descoberta, por exemplo) cheguem ao resumo junto com a mensagem.
- **Entrada ainda não publicada.** Um catálogo que gera a entrada do período corrente antes de
  a fonte publicá-lo (virada de ano ou de mês) marca a entrada com
  `entry["pending_publication"] = True`. Um 404 nessa entrada (em todas as URLs dela) não é erro:
  o `sync` a conta como pulada (`skipped`, com `{"id", "reason": "pending-publication"}` em
  `skipped_items` do resumo e log em INFO) e o código de saída não muda; o `check` a devolve
  como `skip-unavailable` com motivo `pending-publication`. Sem a marca, 404 segue sendo erro do
  arquivo, e qualquer outro erro (403, 5xx, rede) também, com ou sem a marca.
- **Falha de descoberta no `check`.** Quando o fetcher não consegue listar uma parte do
  catálogo (um pacote que não responde, um diretório recusado), o `check` não deve abortar
  nem esconder a falha: o plugin devolve, no lugar das entradas que faltaram, uma entrada
  sentinela e o `check_entry` dela devolve um `CheckPlanItem` com `action="skip-unavailable"`
  e `reason="listing-error"` (`LISTING_ERROR_REASON`), com a causa em `entry["error"]` e,
  se houver, a classe em `entry["error_class"]`. O `check` grava o plano dos demais itens e
  sai 0. O `sync --from-plan` conta cada um desses itens como erro (`id` do item, mensagem
  `<error_class>: <error>`; `errors` do `--summary-json`) e sai 3 se algo baixou ou 1 se
  nada baixou; o `--dry-run` do plano com a falha sai 1. O reclassificador do plano mantém
  o item e o motivo. Um `sync` próprio que já lê esses itens (como o do `datasus`) não passa
  pelo `sync` do host e não conta duas vezes. Sem plano (`sync` direto), a falha de
  descoberta é do plugin: ele soma o erro ao resultado do próprio `download_datasets`.
- **Orçamento do HEAD de frescor.** Antes de cada arquivo, o `sync` faz um HEAD para carimbar o
  nome com o `Last-Modified`, e o `check` faz o dele para decidir se há o que baixar; ambos usam o
  orçamento do cliente do app (5 tentativas de até 180 s por padrão). Com a fonte fora do ar isso
  são ~15 minutos por URL antes de qualquer erro. O
  construtor aceita um orçamento próprio e curto para esse HEAD (e só para ele: o download não
  muda): `FetcherApp(..., probe_timeout=30.0, probe_attempts=2, probe_retry_delay=1.0,
  probe_fail_fast_on_connect=True)`. Os três primeiros são tetos (vale o menor entre o parâmetro,
  o do cliente e o da variável de ambiente `QUANTILICA_HTTP_TIMEOUT`, `QUANTILICA_HTTP_ATTEMPTS`
  ou `QUANTILICA_HTTP_RETRY_DELAY`, se definida). Com `probe_fail_fast_on_connect=True`, um HEAD
  que não consegue CONECTAR (`ConnectTimeout`/`ConnectError` depois das tentativas) quer dizer
  fonte fora do ar: no `sync` o arquivo falha na hora, com `fonte inacessível (...)` em `errors`
  do resumo e sem tentar o download; no `check` o item sai `download` com motivo
  `metadata-unavailable: fonte inacessível (...)` (o plano continua completo e o
  `sync --from-plan` seguinte acusa o erro, em vez de o `check` o esconder como `skip-unavailable`).
  No `check`, o host só é dado como fora do ar com confirmação: uma segunda sonda, que começou
  depois de registrada a primeira falha, também não conecta (sondas simultâneas falham juntas num
  mesmo soluço e não confirmam). Dali em diante, e só naquela execução, as demais entradas do host
  falham na hora, sem gastar o orçamento; qualquer resposta do host limpa a suspeita. O `sync` não
  guarda memória de host: cada arquivo falha por si. Falha de HEAD que não é de conexão (status de
  erro, resposta lenta, servidor sem HEAD) continua sem impedir o download, e um `Retry-After` do
  servidor não estica a espera da sonda além de `probe_retry_delay`. Sem nenhum `probe_*`, nada
  muda. Quem sobrescrevia `_safe_head_date` para isso pode apagar a sobrescrita. Para o
  `sync --from-plan` acusar a fonte fora do ar, o plano precisa chegar a ele com o item
  `download`: um reclassificador de plano que troca `download` por `skip-up-to-date` pela idade da
  cópia local deve manter o item quando o motivo começa com `metadata-unavailable`.
- **`pipeline`** chama o `sync` que o `typer` resolve pelo nome (o último registrado), só com os
  argumentos que ele declara: `groups` (ou o único argumento variádico do `sync`), `-o/--output`,
  `--dry-run`, `--workers`, `--verbose` e `--summary-json`. Um `sync` próprio que não aceita
  `-o/--output`, que não aceita `--dry-run` quando ele foi pedido, que não aceita grupos pedidos ou
  que tem opção obrigatória sem padrão faz o `pipeline` sair com 2 e uma mensagem clara.
- **`convert`/`pipeline`** saem com 1 se o diretório de entrada do conversor não existe, sem
  chamar o conversor.
- **`backfill`**: as falhas guardadas no checkpoint só contam para unidades da lista atual; as
  que sumiram da lista (por exemplo, uma unidade de erro de uma listagem que voltou a funcionar)
  são descartadas com um INFO.

## Lake

`quantilica lake` monta e audita o lake de Parquet a partir de um bruto local, sem
depender de banco de dados nem do ledger do acervo (ele é opcional). O fluxo é
bruto local → `select` → `build` → `audit`:

```bash
# 1. visão só com a versão vigente de cada arquivo (hardlink; cai em symlink e em cópia)
quantilica lake select --raw /dados/bruto --out /dados/vigente --exclude excluir.txt

# 2. conversão em Parquet, por fonte e dataset, em processos separados; retoma uma execução interrompida
quantilica lake build --input /dados/vigente --lake /dados/lake --workers 4 --memory 12GB

# 3. auditoria: rodapés, proveniência (sha256), cobertura, estrutura e leitores
quantilica lake audit --lake /dados/lake --input /dados/vigente --json-file auditoria.json
```

- `select` só precisa do `quantilica-core`. `build` e `audit` usam o
  `quantilica-analytics`, que entra com o extra `lake` (`uv add "quantilica-cli[lake]" --index https://index.quantilica.com/simple/`); sem ele
  esses dois comandos avisam o que instalar e saem com código 1.
- `--exclude` e `--exceptions` aceitam um arquivo texto com um item por linha (linhas
  vazias e iniciadas por `#` são ignoradas): `sha256` ou caminho relativo no `select`,
  caminho relativo ou padrão `fnmatch` no `audit`.
- `build` sai com código 1 se alguma tarefa falhar, e com 130 se for interrompido
  (Ctrl+C); rodar de novo retoma e `--retry-errors` refaz só o que falhou. O andamento
  sai em stderr.
- `audit` sai com 0 se o lake for aprovado e com 1 se houver achados. `--json` imprime
  o relatório em JSON no stdout (o andamento fica em stderr); `--json-file ARQ` o grava.
- `--ledger ARQ` (opcional, só em `audit`) confere a proveniência também contra uma
  exportação do ledger; sem ele, a conferência usa o `sha256` dos manifestos ou do
  próprio arquivo.
